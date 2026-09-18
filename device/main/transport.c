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

static transport_status_t s_st;
static SemaphoreHandle_t s_lock;
static volatile bool s_ask_pending;
static int s_ask_attempts;
static char s_ask_text[64] = "我现在的运动状态怎么样？";
static uint32_t s_post_count;

/* [(x,y,z)] in screen frame, filled by the sampling loop. */
static float s_batch[TX_BATCH_MAX][3];

static void status_lock(void)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
}

static void status_unlock(void)
{
    xSemaphoreGive(s_lock);
}

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
    size_t cap = 256 + (size_t)n * 28 + strlen(s_ask_text) * 2 + 64;
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

static void post_batch(int n, bool ask)
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
        return;
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
        return;
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
    } else {
        apply_response(NULL, 0);
        ESP_LOGW(TAG, "POST failed ret=%s status=%d body=%d",
                 esp_err_to_name(ret), status, acc.len);
    }
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
            if (n < TX_BATCH_MAX) {
                /* Upload screen-frame axes so the server's tilt labels match
                 * what the LCD shows (see accel_input_map_to_screen). */
                accel_input_map_to_screen(sample.x_g, sample.y_g,
                                          &s_batch[n][0], &s_batch[n][1]);
                s_batch[n][0] = sanitize_g(s_batch[n][0]);
                s_batch[n][1] = sanitize_g(s_batch[n][1]);
                s_batch[n][2] = sanitize_g(sample.z_g);
                n++;
            }
            status_lock();
            s_st.x_g = sample.x_g;
            s_st.y_g = sample.y_g;
            s_st.z_g = sample.z_g;
            strlcpy(s_st.source, sample.source_name ? sample.source_name : "?", sizeof(s_st.source));
            status_unlock();
        }

        const TickType_t now = xTaskGetTickCount();
        if (n > 0 && ((now - last_post) >= post_ticks || n >= TX_BATCH_MAX)) {
            bool ask = s_ask_pending;
            post_batch(n, ask);

            status_lock();
            bool ok = s_st.server_ok;
            s_st.batch_last = (uint16_t)n;
            s_st.orient = (uint8_t)accel_input_get_orientation();
            char act[TRANSPORT_ACTIVITY_LEN];
            strlcpy(act, s_st.activity, sizeof(act));
            status_unlock();

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
