/*
 * SPDX-License-Identifier: MIT
 *
 * See transport.h. One task does:
 *   sample IMU at CONFIG_RW1_SAMPLE_PERIOD_MS -> buffer ->
 *   every CONFIG_RW1_TELEMETRY_PERIOD_MS: POST the whole batch -> parse reply.
 *
 * Sampling fast and uploading in batches is what makes step counting and
 * free-fall detection real: at the old 2 Hz upload rate both were physically
 * impossible (walking is 1.5-2.5 Hz, a fall lasts < 0.5 s), yet the UI still
 * showed numbers. Now the server gets a genuine 100 Hz waveform while the HTTP
 * request rate stays at 2 Hz.
 */
#include "transport.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "accel_input.h"
#include "led_feedback.h"
#include "wifi_link.h"

static const char *TAG = "transport";

#define TX_PATH "/api/telemetry"

/* Generous on purpose: a slow LAN should not be mistaken for a dead server.
 * The AI reply itself no longer needs a long timeout — the server answers the
 * ask frame immediately ("正在思考…") and delivers the real text on a later
 * frame, so there is nothing here to wait 8 s for. */
#define TX_HTTP_TIMEOUT_MS 5000

/* Give up on a queued question after this many failed uploads (≈ this many
 * telemetry periods) instead of retrying forever. */
#define TX_ASK_MAX_ATTEMPTS 3

/* Samples held for one upload. 128 @ 10 ms = 1.28 s, comfortably more than the
 * default 500 ms period; the batch is flushed early if it fills up. */
#define TX_BATCH_MAX 128

/* Response buffer. 1200 was too small: a long AI reply overran it, the JSON got
 * truncated, cJSON failed to parse it, and a perfectly successful POST was
 * counted as a link failure. */
#define TX_RESP_BUF 4096

/* Remote command (week 2): how many samples one "capture_once" averages.
 * Reuses the ordinary 10 ms sampling tick, so a capture takes 200 ms and never
 * blocks the loop. If the samples cannot be gathered in time we report a failure
 * rather than leaving the web page spinning. */
#define CAPTURE_N 20
#define CAPTURE_TIMEOUT_MS 2000

/* Remote-command state. Only touched from the transport task, so no lock. */
typedef struct {
    /* command being executed right now */
    char       id[TRANSPORT_CMD_ID_LEN];
    bool       running;
    uint8_t    state;          /* transport_cmd_state_t，给 UI 看（不随 ready 复位） */
    TickType_t started;
    int        n;
    float      sum[3];         /* Σx, Σy, Σz (screen frame) */
    float      mag_sum;        /* Σ|a|   —— 用来算 |a| 的标准差 */
    float      mag_sq_sum;     /* Σ|a|² */

    /* result waiting to be uploaded on the next frame */
    bool       ready;
    bool       ok;
    bool       has_measurement; /* capture_once 才有 x/y/z/std；LED 指令只回 ok/ms */
    char       rid[TRANSPORT_CMD_ID_LEN];
    float      ms;
    int        rn;
    float      xyz[3];
    float      std;
    char       err[48];
} cmd_ctx_t;

static transport_status_t s_st;
static SemaphoreHandle_t s_lock;
static volatile bool s_ask_pending;
static int s_ask_attempts;
static char s_ask_text[64] = "我现在的运动状态怎么样？";
static uint32_t s_post_count;
static int s_btn_pending;      /* 自上次成功上报以来 BOOT 被按了几次（第 3 周） */
static char s_last_reply[TRANSPORT_REPLY_LEN];   /* 上一次看到过的服务器回复 */

/* [(x,y,z)] in screen frame, filled by the sampling loop. */
static float s_batch[TX_BATCH_MAX][3];

static cmd_ctx_t s_cmd;

/* Like strlcpy but never cuts a multi-byte UTF-8 character in half (a torn
 * trailing sequence renders as garbage at the end of LVGL labels). */
static void utf8_strlcpy(char *dst, const char *src, size_t cap)
{
    if (cap == 0) {
        return;
    }
    size_t n = strlen(src);
    if (n > cap - 1) {
        n = cap - 1;
        while (n > 0 && ((unsigned char)src[n] & 0xC0) == 0x80) {
            n--;   /* back off continuation bytes; src[n] is now a lead byte */
        }
    }
    memcpy(dst, src, n);
    dst[n] = '\0';
}

static void status_lock(void)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
}

static void status_unlock(void)
{
    xSemaphoreGive(s_lock);
}

/* ---------------- remote commands (week 2 起) ----------------------------- */

/* 支持哪些指令。板端也做一层白名单：服务端万一发来没实现的名字，
 * 静默忽略即可，绝不因为一条陌生指令把采样循环搞乱。 */
typedef enum {
    CMD_NONE = 0,
    CMD_CAPTURE,        /* capture_once —— 累积样本做一次测量 */
    CMD_LED_BLINK,      /* led_blink    —— 闪 n 次（远端物理反馈） */
    CMD_LED_SET,        /* led_set      —— 常亮/熄灭 */
} cmd_kind_t;

static int json_int(const cJSON *obj, const char *key, int dflt)
{
    const cJSON *v = cJSON_IsObject(obj) ? cJSON_GetObjectItemCaseSensitive(obj, key) : NULL;
    return cJSON_IsNumber(v) ? (int)v->valuedouble : dflt;
}

static bool json_bool(const cJSON *obj, const char *key, bool dflt)
{
    const cJSON *v = cJSON_IsObject(obj) ? cJSON_GetObjectItemCaseSensitive(obj, key) : NULL;
    if (cJSON_IsBool(v)) {
        return cJSON_IsTrue(v);
    }
    if (cJSON_IsNumber(v)) {
        return v->valuedouble != 0;
    }
    return dflt;
}

/* 结束当前指令并准备好结果。with_measurement=false 时只回 ok/ms
 * （led_blink / led_set 这类没有测量值）。 */
static void finish_command(bool ok, bool with_measurement, const char *err)
{
    s_cmd.ready = true;
    s_cmd.ok = ok;
    strlcpy(s_cmd.rid, s_cmd.id, sizeof(s_cmd.rid));
    s_cmd.rn = s_cmd.n;
    s_cmd.ms = (float)pdTICKS_TO_MS(xTaskGetTickCount() - s_cmd.started);
    s_cmd.has_measurement = (with_measurement && ok && s_cmd.n > 0);

    if (s_cmd.has_measurement) {
        const float inv = 1.0f / (float)s_cmd.n;
        s_cmd.xyz[0] = s_cmd.sum[0] * inv;
        s_cmd.xyz[1] = s_cmd.sum[1] * inv;
        s_cmd.xyz[2] = s_cmd.sum[2] * inv;
        const float mean_mag = s_cmd.mag_sum * inv;
        const float var = s_cmd.mag_sq_sum * inv - mean_mag * mean_mag;   /* E[|a|²]-E[|a|]² */
        s_cmd.std = (var > 0.0f) ? sqrtf(var) : 0.0f;
        s_cmd.err[0] = '\0';
    } else {
        s_cmd.xyz[0] = s_cmd.xyz[1] = s_cmd.xyz[2] = 0.0f;
        s_cmd.std = 0.0f;
        strlcpy(s_cmd.err, ok ? "" : (err ? err : "command failed"), sizeof(s_cmd.err));
    }

    s_cmd.running = false;
    s_cmd.state = ok ? TRANSPORT_CMD_DONE : TRANSPORT_CMD_FAILED;
    if (!ok) {
        led_feedback_play(LED_FB_ERROR);      /* 失败给一个能看见的物理信号 */
    }
    ESP_LOGI(TAG, "cmd %s: %s (%d samples, %.0f ms, std %.4f g)%s",
             s_cmd.rid, ok ? "done" : "failed", s_cmd.rn, s_cmd.ms, s_cmd.std,
             ok ? "" : s_cmd.err);
}

static void start_capture(const char *id)
{
    if (s_cmd.running) {
        /* The server only has one command in flight at a time, so this can only
         * happen if the previous frame was lost. Ignore the duplicate. */
        ESP_LOGW(TAG, "cmd %s ignored: %s still running", id, s_cmd.id);
        return;
    }
    utf8_strlcpy(s_cmd.id, id, sizeof(s_cmd.id));
    s_cmd.running = true;
    s_cmd.state = TRANSPORT_CMD_RUNNING;
    s_cmd.started = xTaskGetTickCount();
    s_cmd.n = 0;
    s_cmd.sum[0] = s_cmd.sum[1] = s_cmd.sum[2] = 0.0f;
    s_cmd.mag_sum = 0.0f;
    s_cmd.mag_sq_sum = 0.0f;
    status_lock();
    s_st.cmd_count++;
    status_unlock();
    ESP_LOGI(TAG, "cmd %s: capture_once started (%d samples)", s_cmd.id, CAPTURE_N);
}

/* 执行一条刚收到的指令。LED 类指令立刻完成，采集类交给采样循环慢慢累积。 */
static void run_command(cmd_kind_t kind, const char *id,
                        int n, int on_ms, int off_ms, bool on)
{
    if (kind == CMD_NONE) {
        return;
    }
    if (s_cmd.running) {
        ESP_LOGW(TAG, "cmd %s ignored: %s still running", id, s_cmd.id);
        return;
    }

    led_feedback_play(LED_FB_CMD);            /* 收到指令的物理反馈 */

    switch (kind) {
    case CMD_CAPTURE:
        start_capture(id);
        break;

    case CMD_LED_BLINK:
        /* 先把"当前指令"占上，好让结果带上正确的 request_id 和耗时 */
        utf8_strlcpy(s_cmd.id, id, sizeof(s_cmd.id));
        s_cmd.running = true;
        s_cmd.state = TRANSPORT_CMD_RUNNING;
        s_cmd.started = xTaskGetTickCount();
        s_cmd.n = 0;
        led_feedback_blink(n, (uint16_t)on_ms, (uint16_t)off_ms);
        status_lock();
        s_st.cmd_count++;
        status_unlock();
        ESP_LOGI(TAG, "cmd %s: led_blink n=%d on=%dms off=%dms", s_cmd.id, n, on_ms, off_ms);
        finish_command(true, false, NULL);
        break;

    case CMD_LED_SET:
        utf8_strlcpy(s_cmd.id, id, sizeof(s_cmd.id));
        s_cmd.running = true;
        s_cmd.state = TRANSPORT_CMD_RUNNING;
        s_cmd.started = xTaskGetTickCount();
        s_cmd.n = 0;
        led_feedback_steady(on);
        status_lock();
        s_st.cmd_count++;
        status_unlock();
        ESP_LOGI(TAG, "cmd %s: led_set on=%d", s_cmd.id, on ? 1 : 0);
        finish_command(true, false, NULL);
        break;

    default:
        break;
    }
}

/* Feed one freshly sampled point into the running capture. */
static void feed_capture(const float xyz[3])
{
    if (!s_cmd.running) {
        return;
    }
    s_cmd.sum[0] += xyz[0];
    s_cmd.sum[1] += xyz[1];
    s_cmd.sum[2] += xyz[2];
    const float mag = sqrtf(xyz[0] * xyz[0] + xyz[1] * xyz[1] + xyz[2] * xyz[2]);
    s_cmd.mag_sum += mag;
    s_cmd.mag_sq_sum += mag * mag;

    if (++s_cmd.n >= CAPTURE_N) {
        finish_command(true, true, NULL);
    }
}

/* Give up on a capture that is not getting its samples (e.g. IMU went quiet). */
static void check_capture_timeout(void)
{
    if (s_cmd.running &&
        pdTICKS_TO_MS(xTaskGetTickCount() - s_cmd.started) > CAPTURE_TIMEOUT_MS) {
        finish_command(false, true, "not enough samples");
    }
}

/* Append src to buf[off..cap) with JSON escaping. Returns false if it did not
 * fit. Source names and question text are internal literals, but escaping keeps
 * a future "ask" that contains a quote from producing invalid JSON. */
static bool json_escape_append(char *buf, size_t cap, int *off, const char *src)
{
    for (const char *p = src; *p != '\0'; ++p) {
        const char *esc = NULL;
        switch (*p) {
        case '"':  esc = "\\\""; break;
        case '\\': esc = "\\\\"; break;
        case '\n': esc = "\\n";  break;
        case '\r': esc = "\\r";  break;
        case '\t': esc = "\\t";  break;
        default: break;
        }
        char one[2] = {*p, '\0'};
        const char *text = esc ? esc : one;
        size_t len = strlen(text);
        if ((size_t)*off + len + 1 > cap) {
            return false;
        }
        memcpy(buf + *off, text, len);
        *off += (int)len;
    }
    buf[*off] = '\0';
    return true;
}

/* Sensor values go straight into "%.3f" formatting, so an implausible reading
 * (a mis-detected chip format, a garbled I2C read) must not be able to blow up
 * the JSON or poison the server's statistics. NaN fails both comparisons and is
 * therefore also caught here. */
static float sanitize_g(float v)
{
    return (v >= -8.0f && v <= 8.0f) ? v : 0.0f;
}

/* Build the telemetry JSON by hand rather than with cJSON: cJSON prints doubles
 * with 15 significant digits, so a float 0.012f comes out as
 * "0.0120000001634057" and a 50-sample batch balloons to ~2.7 kB. "%.3f" gives
 * exactly the precision the analysis needs (1 mg) in a third of the bytes. */
static char *build_body(int n, bool ask, const char *source)
{
    if (n <= 0) {
        return NULL;
    }
    size_t cap = 256 + (size_t)n * 28 + strlen(s_ask_text) * 2 + 64
                 + (s_cmd.ready ? 320 : 0) + 32;
    char *buf = malloc(cap);
    if (buf == NULL) {
        return NULL;
    }

    int off = snprintf(buf, cap, "{\"batch\":[");
    for (int i = 0; i < n; ++i) {
        if ((size_t)off + 48 > cap) {
            free(buf);
            return NULL;
        }
        off += snprintf(buf + off, cap - (size_t)off, "%s[%.3f,%.3f,%.3f]",
                        i ? "," : "", s_batch[i][0], s_batch[i][1], s_batch[i][2]);
    }

    if ((size_t)off + 128 > cap) {
        free(buf);
        return NULL;
    }
    off += snprintf(buf + off, cap - (size_t)off,
                    "],\"x\":%.3f,\"y\":%.3f,\"z\":%.3f,\"source\":\"",
                    s_batch[n - 1][0], s_batch[n - 1][1], s_batch[n - 1][2]);

    const char *src = (source != NULL && source[0]) ? source : "?";
    if (!json_escape_append(buf, cap, &off, src) ||
        (size_t)off + 32 > cap) {
        free(buf);
        return NULL;
    }
    off += snprintf(buf + off, cap - (size_t)off, "\",\"ask\":%s", ask ? "true" : "false");

    if (ask) {
        if ((size_t)off + 16 > cap) {
            free(buf);
            return NULL;
        }
        off += snprintf(buf + off, cap - (size_t)off, ",\"q\":\"");
        if (!json_escape_append(buf, cap, &off, s_ask_text)) {
            free(buf);
            return NULL;
        }
        off += snprintf(buf + off, cap - (size_t)off, "\"");
    }

    /* 远程指令的执行结果（带同一个 request_id 回传给服务器） */
    if (s_cmd.ready) {
        if ((size_t)off + 288 > cap) {
            free(buf);
            return NULL;
        }
        off += snprintf(buf + off, cap - (size_t)off, ",\"result\":{\"id\":\"");
        if (!json_escape_append(buf, cap, &off, s_cmd.rid)) {
            free(buf);
            return NULL;
        }
        off += snprintf(buf + off, cap - (size_t)off,
                        "\",\"ok\":%s,\"ms\":%.1f,\"n\":%d",
                        s_cmd.ok ? "true" : "false", s_cmd.ms, s_cmd.rn);
        if (s_cmd.has_measurement) {
            off += snprintf(buf + off, cap - (size_t)off,
                            ",\"x\":%.4f,\"y\":%.4f,\"z\":%.4f,\"std\":%.4f",
                            s_cmd.xyz[0], s_cmd.xyz[1], s_cmd.xyz[2], s_cmd.std);
        } else if (!s_cmd.ok) {
            off += snprintf(buf + off, cap - (size_t)off, ",\"err\":\"");
            if (!json_escape_append(buf, cap, &off, s_cmd.err)) {
                free(buf);
                return NULL;
            }
            off += snprintf(buf + off, cap - (size_t)off, "\"");
        }
        off += snprintf(buf + off, cap - (size_t)off, "}");
    }

    /* 按键触发（第 3 周）：自上次上报以来 BOOT 被按了几次。
     * 让服务端/网页能看见"物理动作真的发生了"，而不只是间接看到 ask。 */
    if (s_btn_pending > 0) {
        if ((size_t)off + 24 > cap) {
            free(buf);
            return NULL;
        }
        off += snprintf(buf + off, cap - (size_t)off, ",\"btn\":%d", s_btn_pending);
    }

    /* Close with an explicit write (not snprintf) so a full buffer can never
     * silently drop the brace and hand the server truncated JSON. */
    if ((size_t)off + 2 > cap) {
        free(buf);
        return NULL;
    }
    buf[off++] = '}';
    buf[off] = '\0';
    return buf;
}

static void apply_response(const char *body, size_t len)
{
    char activity[TRANSPORT_ACTIVITY_LEN] = "";
    char reply[TRANSPORT_REPLY_LEN] = "";
    char cmd_id[TRANSPORT_CMD_ID_LEN] = "";
    cmd_kind_t kind = CMD_NONE;
    int p_n = 0, p_on = 0, p_off = 0;
    bool p_onf = false;
    bool ok = false;
    bool pending = false;

    if (body != NULL && len > 0) {
        cJSON *root = cJSON_ParseWithLength(body, len);
        if (root != NULL) {
            const cJSON *jok = cJSON_GetObjectItemCaseSensitive(root, "ok");
            const cJSON *jact = cJSON_GetObjectItemCaseSensitive(root, "activity");
            const cJSON *jrep = cJSON_GetObjectItemCaseSensitive(root, "reply");
            const cJSON *jpen = cJSON_GetObjectItemCaseSensitive(root, "pending");
            ok = cJSON_IsTrue(jok);
            pending = cJSON_IsTrue(jpen);
            if (cJSON_IsString(jact) && jact->valuestring != NULL) {
                utf8_strlcpy(activity, jact->valuestring, sizeof(activity));
            }
            if (cJSON_IsString(jrep) && jrep->valuestring != NULL) {
                utf8_strlcpy(reply, jrep->valuestring, sizeof(reply));
            }
            /* 远程指令：只认白名单里的名字，不认识的静默忽略，别把板子搞乱 */
            const cJSON *jcmd = cJSON_GetObjectItemCaseSensitive(root, "cmd");
            if (cJSON_IsObject(jcmd)) {
                const cJSON *jid = cJSON_GetObjectItemCaseSensitive(jcmd, "id");
                const cJSON *jname = cJSON_GetObjectItemCaseSensitive(jcmd, "name");
                const cJSON *jparams = cJSON_GetObjectItemCaseSensitive(jcmd, "params");
                if (cJSON_IsString(jid) && jid->valuestring != NULL &&
                    cJSON_IsString(jname) && jname->valuestring != NULL) {
                    if (strcmp(jname->valuestring, "capture_once") == 0) {
                        kind = CMD_CAPTURE;
                    } else if (strcmp(jname->valuestring, "led_blink") == 0) {
                        kind = CMD_LED_BLINK;
                        p_n   = json_int(jparams, "n", 3);
                        p_on  = json_int(jparams, "on_ms", 80);
                        p_off = json_int(jparams, "off_ms", 80);
                    } else if (strcmp(jname->valuestring, "led_set") == 0) {
                        kind = CMD_LED_SET;
                        p_onf = json_bool(jparams, "on", true);
                    }
                    if (kind != CMD_NONE) {
                        utf8_strlcpy(cmd_id, jid->valuestring, sizeof(cmd_id));
                    } else {
                        ESP_LOGW(TAG, "unknown cmd '%s' ignored", jname->valuestring);
                    }
                }
            }
            cJSON_Delete(root);
        }
    }

    status_lock();
    s_st.posts_ok += ok ? 1 : 0;
    s_st.posts_fail += ok ? 0 : 1;
    s_st.server_ok = ok;
    s_st.fail_streak = ok ? 0 : s_st.fail_streak + 1;
    s_st.ai_pending = pending;
    if (activity[0] != '\0') {
        strlcpy(s_st.activity, activity, sizeof(s_st.activity));
    }
    if (reply[0] != '\0') {
        strlcpy(s_st.reply, reply, sizeof(s_st.reply));
    }
    status_unlock();

    /* 在锁外执行指令：run_command 只碰 s_cmd 和 LED，不能拖住状态锁 */
    if (cmd_id[0] != '\0') {
        run_command(kind, cmd_id, p_n, p_on, p_off, p_onf);
    }
}

/* Response body is collected during esp_http_client_perform() via the
 * HTTP_EVENT_ON_DATA callback — the most reliable pattern for esp_http_client
 * (post-perform esp_http_client_read can return 0 on HTTP/1.0 responses). */
typedef struct {
    char buf[TX_RESP_BUF];
    int  len;
    bool truncated;
} resp_acc_t;

static esp_err_t http_event_handler(esp_http_client_event_t *evt)
{
    if (evt->event_id == HTTP_EVENT_ON_DATA && evt->user_data != NULL) {
        resp_acc_t *acc = (resp_acc_t *)evt->user_data;
        int space = (int)sizeof(acc->buf) - acc->len - 1;
        int n = evt->data_len < space ? evt->data_len : space;
        if (n > 0) {
            memcpy(acc->buf + acc->len, evt->data, (size_t)n);
            acc->len += n;
            acc->buf[acc->len] = '\0';
        }
        if (n < evt->data_len) {
            acc->truncated = true;
        }
    }
    return ESP_OK;
}

static bool post_batch(int n, bool ask)
{
    char url[160];
    snprintf(url, sizeof(url), "%s%s", CONFIG_RW1_SERVER_URL, TX_PATH);

    char source[16];
    status_lock();
    strlcpy(source, s_st.source, sizeof(source));
    status_unlock();

    char *body = build_body(n, ask, source);
    if (body == NULL) {
        ESP_LOGE(TAG, "out of memory building telemetry body");
        return false;
    }

    resp_acc_t acc = {.len = 0, .truncated = false};
    acc.buf[0] = '\0';
    esp_http_client_config_t cfg = {
        .url = url,
        .timeout_ms = TX_HTTP_TIMEOUT_MS,
        .event_handler = http_event_handler,
        .user_data = &acc,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        free(body);
        return false;
    }

    esp_http_client_set_method(client, HTTP_METHOD_POST);
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, body, (int)strlen(body));

    esp_err_t ret = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    free(body);

    if (ret == ESP_OK && status == 200 && acc.len > 0) {
        if (acc.truncated) {
            ESP_LOGW(TAG, "response truncated at %d bytes (raise TX_RESP_BUF)", acc.len);
        }
        apply_response(acc.buf, (size_t)acc.len);
        if (ask) {
            ESP_LOGI(TAG, "AI reply: %s", acc.buf);
        }
        return true;
    }

    apply_response(NULL, 0);
    ESP_LOGW(TAG, "POST failed ret=%s status=%d body=%d",
             esp_err_to_name(ret), status, acc.len);
    return false;
}

static void transport_task(void *arg)
{
    ESP_LOGI(TAG, "waiting for WiFi...");
    while (!wifi_link_is_up()) {
        vTaskDelay(pdMS_TO_TICKS(500));
    }

    const TickType_t sample_ticks = pdMS_TO_TICKS(CONFIG_RW1_SAMPLE_PERIOD_MS);
    const TickType_t post_ticks = pdMS_TO_TICKS(CONFIG_RW1_TELEMETRY_PERIOD_MS);
    ESP_LOGI(TAG, "telemetry -> %s%s : %d samples @ %d ms, upload every %d ms",
             CONFIG_RW1_SERVER_URL, TX_PATH,
             (int)(CONFIG_RW1_TELEMETRY_PERIOD_MS / CONFIG_RW1_SAMPLE_PERIOD_MS),
             CONFIG_RW1_SAMPLE_PERIOD_MS, CONFIG_RW1_TELEMETRY_PERIOD_MS);

    TickType_t last_post = xTaskGetTickCount();
    int n = 0;
    char last_activity[TRANSPORT_ACTIVITY_LEN] = "";

    while (true) {
        accel_input_sample_t sample = {0};
        if (accel_input_poll(&sample)) {
            /* Upload screen-frame axes so the server's tilt labels match what
             * the LCD shows (see accel_input_map_to_screen). */
            float sx, sy;
            accel_input_map_to_screen(sample.x_g, sample.y_g, &sx, &sy);
            sx = sanitize_g(sx);
            sy = sanitize_g(sy);
            const float sz = sanitize_g(sample.z_g);
            if (n < TX_BATCH_MAX) {
                s_batch[n][0] = sx;
                s_batch[n][1] = sy;
                s_batch[n][2] = sz;
                n++;
            }
            /* 正在执行远程指令就用同一个采样节拍累积，不额外阻塞 */
            const float xyz[3] = {sx, sy, sz};
            feed_capture(xyz);

            status_lock();
            s_st.x_g = sample.x_g;
            s_st.y_g = sample.y_g;
            s_st.z_g = sample.z_g;
            strlcpy(s_st.source, sample.source_name ? sample.source_name : "?", sizeof(s_st.source));
            status_unlock();
        }
        check_capture_timeout();

        const TickType_t now = xTaskGetTickCount();
        if (n > 0 && ((now - last_post) >= post_ticks || n >= TX_BATCH_MAX)) {
            bool ask = s_ask_pending;
            bool ok = post_batch(n, ask);

            /* 结果与按键计数只在成功送达后才清；失败就下一帧重发
             * （与 ask 的重试策略一致，不丢东西） */
            if (ok) {
                if (s_cmd.ready) {
                    s_cmd.ready = false;
                }
                s_btn_pending = 0;
            }

            status_lock();
            s_st.batch_last = (uint16_t)n;
            s_st.orient = (uint8_t)accel_input_get_orientation();
            s_st.cmd_state = s_cmd.state;
            if (s_cmd.rid[0] != '\0') {
                strlcpy(s_st.cmd_id, s_cmd.rid, sizeof(s_st.cmd_id));
            }
            char act[TRANSPORT_ACTIVITY_LEN];
            strlcpy(act, s_st.activity, sizeof(act));
            const bool reply_changed =
                (s_st.reply[0] != '\0' && strcmp(s_st.reply, s_last_reply) != 0);
            if (reply_changed) {
                strlcpy(s_last_reply, s_st.reply, sizeof(s_last_reply));
            }
            status_unlock();

            /* 服务器/AI 的回复变了就闪两下 —— 这是"远端反馈真的回来了"的物理信号 */
            if (reply_changed) {
                led_feedback_play(LED_FB_REPLY);
            }

            /* A button press must never be silently swallowed: keep the flag and
             * retry on the next frame, but stop after a few tries. */
            if (ask) {
                if (ok) {
                    s_ask_pending = false;
                    s_ask_attempts = 0;
                } else if (++s_ask_attempts >= TX_ASK_MAX_ATTEMPTS) {
                    ESP_LOGW(TAG, "ask dropped after %d failed uploads", s_ask_attempts);
                    s_ask_pending = false;
                    s_ask_attempts = 0;
                } else {
                    ESP_LOGW(TAG, "ask upload failed, retry %d/%d",
                             s_ask_attempts, TX_ASK_MAX_ATTEMPTS);
                }
            }

            if (ok && strcmp(act, last_activity) != 0) {
                strlcpy(last_activity, act, sizeof(last_activity));
                ESP_LOGI(TAG, "activity: %s (x=%+.2f y=%+.2f z=%+.2f)",
                         act, s_batch[n - 1][0], s_batch[n - 1][1], s_batch[n - 1][2]);
            }

            /* Heartbeat every 20 uploads (10 s at the default period). This used
             * to key off "activity changed 20 times", so whenever the board sat
             * still the counter stayed 0 and it logged on every single frame. */
            if ((++s_post_count % 20) == 0) {
                status_lock();
                uint32_t okc = s_st.posts_ok, failc = s_st.posts_fail;
                status_unlock();
                ESP_LOGI(TAG, "link %s ok=%u fail=%u batch=%d", ok ? "OK" : "DOWN",
                         (unsigned)okc, (unsigned)failc, n);
            }

            n = 0;
            last_post = xTaskGetTickCount();
        }

        vTaskDelay(sample_ticks);
    }
}

void transport_start(void)
{
    if (s_lock == NULL) {
        s_lock = xSemaphoreCreateMutex();
    }
    xTaskCreatePinnedToCore(transport_task, "transport", 8192, NULL, 5, NULL, 0);
}

void transport_request_ask(const char *question)
{
    if (question != NULL && question[0] != '\0') {
        strlcpy(s_ask_text, question, sizeof(s_ask_text));
    }
    s_ask_attempts = 0;
    s_ask_pending = true;
    s_btn_pending++;          /* 让服务端/网页能看见"物理按键发生了" */
    ESP_LOGI(TAG, "ask queued: %s", s_ask_text);
}

void transport_get_status(transport_status_t *out)
{
    if (s_lock == NULL) {
        memset(out, 0, sizeof(*out));
        return;
    }
    status_lock();
    memcpy(out, &s_st, sizeof(*out));
    status_unlock();
}
