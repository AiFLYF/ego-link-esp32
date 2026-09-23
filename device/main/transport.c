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
#include <dirent.h>
#include <sys/stat.h>

#include "bsp/esp-bsp.h"      /* BSP_SD_MOUNT_POINT */
#include "esp_vfs_fat.h"     /* esp_vfs_fat_info */

#include "sd_card.h"
#include "sd_log.h"
#include "net_config.h"
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
/* SD 留档的节奏：每多少个样本记一行（100 Hz 采样 → 50 个 ≈ 500 ms 一行）。
 * 单独一个常量，不复用 TX_BATCH_MAX —— 那是「一批最多多少」，不是节奏。 */
#define SD_LOG_EVERY_SAMPLES 50

/* Response buffer. 1200 was too small: a long AI reply overran it, the JSON got
 * truncated, cJSON failed to parse it, and a perfectly successful POST was
 * counted as a link failure. */
#define TX_RESP_BUF 4096

/* Remote command (week 2): how many samples one "capture_once" averages.
 * Reuses the ordinary 10 ms sampling tick, so a capture takes 200 ms and never
 * blocks the loop. If the samples cannot be gathered in time we report a failure
 * rather than leaving the web page spinning. */
/* 指令回传里的**自由文本**字段长度。目前用于 sd_ls 的文件列表 / sd_rm 的结果说明。
 * 不复用 err —— err 的语义是「失败原因」，拿它传正常数据会让服务端/网页的判断变味。 */
#define CMD_NOTE_LEN 384

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
    char       note[CMD_NOTE_LEN];   /* 自由文本：sd_ls 的文件列表、sd_rm 的结果说明 */
} cmd_ctx_t;

static transport_status_t s_st;
static SemaphoreHandle_t s_lock;
static volatile bool s_ask_pending;
static int s_ask_attempts;
static char s_ask_text[64] = "我现在的运动状态怎么样？";
static uint32_t s_post_count;
static int s_btn_pending;      /* 自上次成功上报以来 BOOT 被按了几次（第 3 周） */
static uint32_t s_bad_samples; /* 累计丢弃的不可用样本数（P1-4，只统计不上报） */
static char s_url[NET_URL_MAX];/* 上报地址：来自 net_config（运行期可变，不再编译期写死） */
/* 设备 id：服务端按它分片（多板场景下仪表盘区分板子的唯一依据）。
 * 由 net_config_device_id() 解析：配网页填了就用填的，留空则按 MAC 生成 rw1-XXXX。 */
static char s_device[NET_DEV_MAX];
static uint32_t s_poll_fail;   /* 连续 accel_input_poll 失败次数（真实源读失败） */
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
    CMD_SET_ORIENT,     /* set_orient   —— 远程设置方向档位（与长按 BOOT 等价） */
    CMD_SET_CONFIG,     /* set_config   —— 远程改 WiFi / 服务器地址 / 上报周期 */
    CMD_SD_FORMAT,      /* sd_format    —— 格式化 SD 卡（卡里长文件名的旧文件板子删不掉，只能整卡格式化） */
    CMD_SD_LS,          /* sd_ls        —— 列出 SD 卡文件 + 容量（网页存储管理用） */
    CMD_SD_RM,          /* sd_rm        —— 删除 SD 卡上的一个文件（参数 name） */
} cmd_kind_t;

/* set_config 的参数（含**字符串**）。用文件级静态而不是扩 run_command 的参数列表：
 * 那个列表已经有 6 个参数了，再塞 3 个字符串和 1 个 int 只会更难读。
 * 只有 transport 任务会碰它们，不存在竞态。 */
/* sd_rm 要删的文件名（字符串参数，和 set_config 一样先用静态存下来） */
static char s_rm_name[64];

static char s_cfg_ssid[NET_SSID_MAX];
static char s_cfg_pass[NET_PASS_MAX];
static char s_cfg_url[NET_URL_MAX];
static int  s_cfg_period;

/* 上报周期（毫秒）—— 做成变量而不是 const，好让 set_config 在运行时改掉它。
 * 「灵敏度」本质上就是这个：周期越短，网页小球跟得越紧。 */
static TickType_t s_post_ticks;

/* led_blink 的可选 pattern 参数：让服务端能表达"这是告警/确认/错误"的语义，
 * 而不是只丢一个"闪 N 次"过来。板端映射到对应的预置图案——
 * 现场一眼能分清是远端告警还是普通闪灯。 */
enum {
    CMD_PATTERN_NONE = 0,
    CMD_PATTERN_ALERT,
    CMD_PATTERN_ACK,
    CMD_PATTERN_ERROR,
};

static int json_pattern(const cJSON *obj)
{
    const cJSON *v = cJSON_IsObject(obj)
                     ? cJSON_GetObjectItemCaseSensitive(obj, "pattern") : NULL;
    if (!cJSON_IsString(v) || v->valuestring == NULL) {
        return CMD_PATTERN_NONE;
    }
    if (strcmp(v->valuestring, "alert") == 0) {
        return CMD_PATTERN_ALERT;
    }
    if (strcmp(v->valuestring, "ack") == 0) {
        return CMD_PATTERN_ACK;
    }
    if (strcmp(v->valuestring, "error") == 0) {
        return CMD_PATTERN_ERROR;
    }
    return CMD_PATTERN_NONE;
}

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
/* 远程改配置 —— 等价于配网页，但不用掏手机。
 *
 * 参数都可选，只改传了的那些：{"ssid":"..","pass":"..","url":"..","period_ms":200}
 *
 * **WiFi 先试连、连上了才保存**（和配网页 `wifi_link_try_sta` 一致）：
 * 凭据填错时板子不会失联 —— 这是刻意选的保守做法，代价是这条指令要等几秒。
 * 试连期间 transport 被占住（遥测会停几秒），所以返回里会带上耗时。 */
/* 把 SD 卡根目录列成一行紧凑文本给网页：`NAME|SIZE;NAME|SIZE`。
 * `|` `;` 当分隔符是安全的 —— 8.3 文件名里不可能出现它们。
 * 文件多了会撑爆 note，所以最多列 CMD_NOTE_MAX_FILES 个，末尾加 `...` 提示还有。 */
#define CMD_NOTE_MAX_FILES 12

static void run_sd_ls(const char *id)
{
    utf8_strlcpy(s_cmd.id, id, sizeof(s_cmd.id));
    s_cmd.running = true;
    s_cmd.state = TRANSPORT_CMD_RUNNING;
    s_cmd.started = xTaskGetTickCount();
    s_cmd.n = 0;
    s_cmd.note[0] = '\0';

    if (!sd_card_mounted()) {
        finish_command(false, false, "没有挂载 SD 卡");
        return;
    }

    /* 容量放最前面：网页不用再发一条指令去问 */
    uint64_t total = 0, free_b = 0;
    int off = 0;
    if (esp_vfs_fat_info(BSP_SD_MOUNT_POINT, &total, &free_b) == ESP_OK) {
        off += snprintf(s_cmd.note + off, sizeof(s_cmd.note) - (size_t)off,
                        "SPACE,%llu,%llu;", (unsigned long long)total,
                        (unsigned long long)free_b);
    }

    DIR *d = opendir(BSP_SD_MOUNT_POINT);
    if (d == NULL) {
        finish_command(false, false, "打不开 SD 卡根目录");
        return;
    }
    struct dirent *ent;
    int shown = 0, more = 0;
    while ((ent = readdir(d)) != NULL) {
        if (ent->d_name[0] == '.') {
            continue;                       /* 跳过 . / .. 和隐藏项 */
        }
        if (shown >= CMD_NOTE_MAX_FILES) {
            more = 1;
            break;
        }
        /* 缓冲要放得下 mount point + '/' + **最长可能的 d_name(255)** ——
         * 原来写 128，被 -Werror=format-truncation 挡下（T21 同一类：
         * snprintf 的目标缓冲比"最坏情况"小）。 */
        char full[320];
        snprintf(full, sizeof(full), "%s/%s", BSP_SD_MOUNT_POINT, ent->d_name);
        struct stat st;
        long sz = (stat(full, &st) == 0) ? (long)st.st_size : -1;
        int n = snprintf(s_cmd.note + off, sizeof(s_cmd.note) - (size_t)off,
                         "%s|%ld;", ent->d_name, sz);
        if (n < 0 || (size_t)(off + n) >= sizeof(s_cmd.note)) {
            break;                          /* 塞不下就停，已列出来的照样发回去 */
        }
        off += n;
        shown++;
    }
    closedir(d);
    if (more && (size_t)off + 4 < sizeof(s_cmd.note)) {
        strlcpy(s_cmd.note + off, "...;", sizeof(s_cmd.note) - (size_t)off);
    }

    status_lock();
    s_st.cmd_count++;
    status_unlock();
    ESP_LOGI(TAG, "cmd %s: sd_ls 列了 %d 个文件", s_cmd.id, shown);
    finish_command(true, false, NULL);
}

static void run_sd_rm(const char *id, const char *name)
{
    utf8_strlcpy(s_cmd.id, id, sizeof(s_cmd.id));
    s_cmd.running = true;
    s_cmd.state = TRANSPORT_CMD_RUNNING;
    s_cmd.started = xTaskGetTickCount();
    s_cmd.n = 0;
    s_cmd.note[0] = '\0';

    if (!sd_card_mounted()) {
        finish_command(false, false, "没有挂载 SD 卡");
        return;
    }
    /* 只允许删根目录下的普通文件：名字里出现 '/' 或 '..' 一律拒绝，
     * 免得网页（或伪造的请求）能顺着路径删到别的挂载点上去。 */
    if (name == NULL || name[0] == '\0' || strchr(name, '/') != NULL ||
        strchr(name, '\\') != NULL || strstr(name, "..") != NULL) {
        finish_command(false, false, "文件名不合法（只允许 SD 根目录下的文件名）");
        return;
    }

    char full[128];
    snprintf(full, sizeof(full), "%s/%s", BSP_SD_MOUNT_POINT, name);
    if (remove(full) != 0) {
        finish_command(false, false, "删除失败（文件不存在 / 只读卡？）");
        return;
    }
    snprintf(s_cmd.note, sizeof(s_cmd.note), "已删除 %s", name);

    status_lock();
    s_st.cmd_count++;
    status_unlock();
    ESP_LOGI(TAG, "cmd %s: sd_rm 删除 %s", s_cmd.id, name);
    finish_command(true, false, NULL);
}

static void run_set_config(const char *id)
{
    net_config_t cfg;
    net_config_load(&cfg);

    utf8_strlcpy(s_cmd.id, id, sizeof(s_cmd.id));
    s_cmd.running = true;
    s_cmd.state = TRANSPORT_CMD_RUNNING;
    s_cmd.started = xTaskGetTickCount();
    s_cmd.n = 0;

    const bool wifi_change = (s_cfg_ssid[0] != '\0' && strcmp(s_cfg_ssid, cfg.ssid) != 0);
    if (wifi_change) {
        char ip[20] = "";
        ESP_LOGI(TAG, "set_config: 试连 '%s' ...", s_cfg_ssid);
        if (!wifi_link_try_sta(s_cfg_ssid, s_cfg_pass, 8000, ip, sizeof(ip))) {
            finish_command(false, false, "WiFi 连不上（密码错或找不到该网络）—— 配置未保存");
            return;
        }
        utf8_strlcpy(cfg.ssid, s_cfg_ssid, sizeof(cfg.ssid));
        utf8_strlcpy(cfg.pass, s_cfg_pass, sizeof(cfg.pass));
        ESP_LOGI(TAG, "set_config: 试连成功 (%s)", ip);
    }
    if (s_cfg_url[0] != '\0') {
        utf8_strlcpy(cfg.url, s_cfg_url, sizeof(cfg.url));
    }
    /* 下限从 100ms 放宽到 50ms —— 50ms = 20Hz。用户要 20Hz 的实时感，
     * 100ms 只能到 10Hz。注意 50ms 已接近 WiFi 单次往返的量级，
     * 实际能跑多快取决于现场网络，跑不到也别指望更低了。 */
    if (s_cfg_period >= 50 && s_cfg_period <= 2000) {
        cfg.period_ms = (uint16_t)s_cfg_period;
    }
    if (!net_config_save(&cfg)) {
        finish_command(false, false, "写 NVS 失败");
        return;
    }
    transport_reload_config();
    ESP_LOGI(TAG, "set_config ok: ssid='%s' url='%s' period=%d",
             cfg.ssid, cfg.url, (int)cfg.period_ms);
    finish_command(true, false, NULL);
}

static void run_command(cmd_kind_t kind, const char *id,
                        int n, int on_ms, int off_ms, bool on, int pattern)
{
    /* 每条指令进来先清 note：新指令（sd_ls/sd_rm）自己会填，
     * 老指令（led_blink 等）不填 —— 不清的话回传里会带上一条的残留。 */
    s_cmd.note[0] = '\0';

    if (kind == CMD_NONE) {
        return;
    }
    if (s_cmd.running) {
        ESP_LOGW(TAG, "cmd %s ignored: %s still running", id, s_cmd.id);
        return;
    }

    switch (kind) {
    case CMD_CAPTURE:
        /* 只有采集类才播"收到指令"的中闪：它要跑 200ms，这段时间有反馈才有意义。
         * LED 类指令**不能**先播 —— 紧接着的 led_feedback_blink/steady 会
         * 立刻打断它，用户根本看不见（P1-2）。LED 指令自己的闪烁就是反馈。 */
        led_feedback_play(LED_FB_CMD);
        start_capture(id);
        break;

    case CMD_LED_BLINK:
        /* 先把"当前指令"占上，好让结果带上正确的 request_id 和耗时 */
        utf8_strlcpy(s_cmd.id, id, sizeof(s_cmd.id));
        s_cmd.running = true;
        s_cmd.state = TRANSPORT_CMD_RUNNING;
        s_cmd.started = xTaskGetTickCount();
        s_cmd.n = 0;
        if (pattern == CMD_PATTERN_ALERT) {
            /* 服务端说"这是告警"，就别让它只是"闪三下"——用带语义的图案，
             * 现场一眼能分清是远端告警还是普通闪灯。 */
            led_feedback_play(LED_FB_ALERT);
        } else if (pattern == CMD_PATTERN_ACK) {
            led_feedback_play(LED_FB_ACK);
        } else if (pattern == CMD_PATTERN_ERROR) {
            led_feedback_play(LED_FB_ERROR);
        } else {
            led_feedback_blink(n, (uint16_t)on_ms, (uint16_t)off_ms);
        }
        status_lock();
        s_st.cmd_count++;
        status_unlock();
        ESP_LOGI(TAG, "cmd %s: led_blink n=%d on=%dms off=%dms pattern=%d",
                 s_cmd.id, n, on_ms, off_ms, pattern);
        finish_command(true, false, NULL);
        break;

    case CMD_SET_CONFIG:
        run_set_config(id);
        break;

    case CMD_SD_LS:
        run_sd_ls(id);
        break;

    case CMD_SD_RM:
        run_sd_rm(id, s_rm_name);
        break;

    case CMD_SD_FORMAT: {
        /* 顺序很重要：
         *   1) 先关日志句柄 —— 格式化后它指向的 FAT 表就失效了，不关会写出乱码
         *   2) 再格式化（IDF 的 esp_vfs_fat_sdcard_format 支持挂载状态下直接调）
         *   3) 成功后立刻重开日志，不丢后续数据
         * 格式化要几秒，这期间 transport 被占住（遥测会停几秒），返回里会说明。 */
        utf8_strlcpy(s_cmd.id, id, sizeof(s_cmd.id));
        s_cmd.running = true;
        s_cmd.state = TRANSPORT_CMD_RUNNING;
        s_cmd.started = xTaskGetTickCount();
        s_cmd.n = 0;

        sd_log_close();
        if (sd_card_format() == ESP_OK) {
            sd_log_init();
            status_lock();
            s_st.cmd_count++;
            status_unlock();
            ESP_LOGI(TAG, "cmd %s: sd_format ok", s_cmd.id);
            finish_command(true, false, NULL);
        } else {
            sd_log_init();          /* 失败也把日志开回来，别因为一次格式化失败就不记了 */
            finish_command(false, false, "格式化失败（没插卡 / 卡写保护 / 卡已损坏？）");
        }
        break;
    }

    case CMD_SET_ORIENT: {
        /* 与长按 BOOT 完全等价：改方向档位并写 NVS。
         * 网页上直接点选档位，比"长按盲按 N 次"靠谱得多 ——
         * 2026-09-23 真机标定时用户就是靠长按一次次试出来的。 */
        int o = n;
        if (o < 0) {
            o = 0;
        }
        if (o > 15) {
            o = 15;
        }
        accel_input_set_orientation(o);
        utf8_strlcpy(s_cmd.id, id, sizeof(s_cmd.id));
        s_cmd.running = true;
        s_cmd.state = TRANSPORT_CMD_RUNNING;
        s_cmd.started = xTaskGetTickCount();
        s_cmd.n = 0;
        led_feedback_play(LED_FB_ACK);
        status_lock();
        s_st.cmd_count++;
        status_unlock();
        ESP_LOGI(TAG, "cmd %s: set_orient -> %d", s_cmd.id, o);
        finish_command(true, false, NULL);
        break;
    }

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

/* A sample is only usable when all three axes are plausible (a mis-detected chip
 * format or a garbled I2C read can produce anything, including NaN).
 *
 * 注意：**不能把异常值填 0**。零加速度恰好就是失重的特征（|a| = 0 < FREEFALL_G），
 * 所以一次读错就会被服务端判成「疑似跌落」，而第 3 周起那还会**自动下发 LED 告警**
 * ——一次 I²C 错误变成板子无故闪灯报警。正确做法是判定为不可用、整帧丢弃。
 * NaN 也走这里：它与任何数比较都为假，会自然落到 return false。 */
static bool sanitize3(float x, float y, float z, float out[3])
{
    if (!(x >= -8.0f && x <= 8.0f) ||
        !(y >= -8.0f && y <= 8.0f) ||
        !(z >= -8.0f && z <= 8.0f)) {
        return false;
    }
    out[0] = x;
    out[1] = y;
    out[2] = z;
    return true;
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
                 + (s_cmd.ready ? (320 + CMD_NOTE_LEN + 24) : 0) + 32
                 + strlen(s_device) * 6 + 24;   /* "dev" 字段：设备名是用户输入，转义后可能翻倍 */
    char *buf = malloc(cap);
    if (buf == NULL) {
        return NULL;
    }

    /* 设备 id 放最前面：服务端拿到第一件事就是按它分片，不用先解析完 50 个样本。
     * 字段名用 **"device"**（不是缩写 "dev"）：要和查询参数 ?device=、
     * 以及 net_config 里的字段名一致 —— 三处一个名字，才不会有"板端发了 dev、
     * 服务端找 device"这种对不上的静默失败（verify_server.py 的固件格式契约用例
     * 就是钉这个的）。 */
    int off = snprintf(buf, cap, "{\"device\":\"");
    if (!json_escape_append(buf, cap, &off, s_device)) {
        free(buf);
        return NULL;
    }
    if ((size_t)off + 16 > cap) {
        free(buf);
        return NULL;
    }
    /* 带上方向档位：网页要靠它显示"板子现在是哪一档"，并让"改档位"有反馈闭环。 */
    off += snprintf(buf + off, cap - (size_t)off, "\",\"o\":%d,\"batch\":[",
                    accel_input_get_orientation());
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
        if ((size_t)off + 288 + CMD_NOTE_LEN + 24 > cap) {
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
        /* 自由文本（文件列表等）。文件列表可能带空格和分隔符，
         * 所以照例走 json_escape_append，不直接拼。 */
        if (s_cmd.note[0] != '\0') {
            if ((size_t)off + CMD_NOTE_LEN + 24 > cap) {
                free(buf);
                return NULL;
            }
            off += snprintf(buf + off, cap - (size_t)off, ",\"note\":\"");
            if (!json_escape_append(buf, cap, &off, s_cmd.note)) {
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
    int p_n = 0, p_on = 0, p_off = 0, p_pattern = 0;
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
                        p_pattern = json_pattern(jparams);
                    } else if (strcmp(jname->valuestring, "led_set") == 0) {
                        kind = CMD_LED_SET;
                        p_onf = json_bool(jparams, "on", true);
                    } else if (strcmp(jname->valuestring, "sd_ls") == 0) {
                        kind = CMD_SD_LS;
                    } else if (strcmp(jname->valuestring, "sd_rm") == 0) {
                        kind = CMD_SD_RM;
                        const cJSON *v = cJSON_IsObject(jparams)
                            ? cJSON_GetObjectItemCaseSensitive(jparams, "name") : NULL;
                        if (cJSON_IsString(v)) {
                            utf8_strlcpy(s_rm_name, v->valuestring, sizeof(s_rm_name));
                        }
                    } else if (strcmp(jname->valuestring, "sd_format") == 0) {
                        kind = CMD_SD_FORMAT;
                    } else if (strcmp(jname->valuestring, "set_config") == 0) {
                        /* 字符串参数在这里取好存进文件级静态 —— cJSON 树马上就会被
                         * Delete 掉，不能留着指针到执行点再用。 */
                        const cJSON *v;
                        v = cJSON_IsObject(jparams)
                            ? cJSON_GetObjectItemCaseSensitive(jparams, "ssid") : NULL;
                        if (cJSON_IsString(v)) {
                            utf8_strlcpy(s_cfg_ssid, v->valuestring, sizeof(s_cfg_ssid));
                        }
                        v = cJSON_IsObject(jparams)
                            ? cJSON_GetObjectItemCaseSensitive(jparams, "pass") : NULL;
                        if (cJSON_IsString(v)) {
                            utf8_strlcpy(s_cfg_pass, v->valuestring, sizeof(s_cfg_pass));
                        }
                        v = cJSON_IsObject(jparams)
                            ? cJSON_GetObjectItemCaseSensitive(jparams, "url") : NULL;
                        if (cJSON_IsString(v)) {
                            utf8_strlcpy(s_cfg_url, v->valuestring, sizeof(s_cfg_url));
                        }
                        s_cfg_period = json_int(jparams, "period_ms", 0);
                        kind = CMD_SET_CONFIG;
                    } else if (strcmp(jname->valuestring, "set_orient") == 0) {
                        /* 方向档位远程设置。参数复用 p_n —— 这条指令不需要样本数。 */
                        kind = CMD_SET_ORIENT;
                        p_n = json_int(jparams, "o", 0);
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
    /* 活动词在锁内取一份，给下面的"活动变化"事件用（采样循环那边的留档另有取值） */
    char log_act[sizeof(s_st.activity)];
    strlcpy(log_act, s_st.activity, sizeof(log_act));
    status_unlock();

    /* SD 留档**不在这里**：本函数只在"上报成功"时才会被调到，
     * 而离线时整段上报是被跳过的 —— 挂在这儿等于"断网就不记"，
     * 正好把"断网也能查"这个核心承诺做反了（2026-09-23 真机上抓到）。
     * 现在挂在采样循环里（见 transport_task），与网络无关。
     * 这里只记**活动词变化**这种事件（活动词是服务端给的，只有联网时才知道）。 */
    {
        static char s_log_act[32];
        if (log_act[0] != '\0' && strcmp(log_act, s_log_act) != 0) {
            strlcpy(s_log_act, log_act, sizeof(s_log_act));
            sd_log_event("ACT", log_act);
        }
    }

    /* 在锁外执行指令：run_command 只碰 s_cmd 和 LED，不能拖住状态锁 */
    if (cmd_id[0] != '\0') {
        run_command(kind, cmd_id, p_n, p_on, p_off, p_onf, p_pattern);
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
    char url[NET_URL_MAX + 16];
    snprintf(url, sizeof(url), "%s%s", s_url, TX_PATH);

    char source[16];
    status_lock();
    strlcpy(source, s_st.source, sizeof(source));
    status_unlock();

    char *body = build_body(n, ask, source);
    if (body == NULL) {
        ESP_LOGE(TAG, "out of memory building telemetry body");
        return false;
    }

    /* P1-1：4 KB 的响应缓冲**不能放栈上** —— transport 任务栈只有 8 KB，
     * 再加上 esp_http_client_perform() 自己的栈帧就没有余量了。栈溢出在
     * ESP-IDF 下是 canary 报错 + 重启，而且只在响应体较大时偶发、极难复现。
     * post_batch 只有 transport 任务会调用（不会重入），所以静态化是安全的。 */
    static resp_acc_t s_acc;
    s_acc.len = 0;
    s_acc.truncated = false;
    s_acc.buf[0] = '\0';
    esp_http_client_config_t cfg = {
        .url = url,
        .timeout_ms = TX_HTTP_TIMEOUT_MS,
        .event_handler = http_event_handler,
        .user_data = &s_acc,
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

    if (ret == ESP_OK && status == 200 && s_acc.len > 0) {
        if (s_acc.truncated) {
            ESP_LOGW(TAG, "response truncated at %d bytes (raise TX_RESP_BUF)", s_acc.len);
        }
        apply_response(s_acc.buf, (size_t)s_acc.len);
        if (ask) {
            ESP_LOGI(TAG, "AI reply: %s", s_acc.buf);
        }
        return true;
    }

    apply_response(NULL, 0);
    ESP_LOGW(TAG, "POST failed ret=%s status=%d body=%d",
             esp_err_to_name(ret), status, s_acc.len);
    return false;
}

/* ---------------- 1 Hz IMU diagnostic line (validation aid) -----------------
 *
 * Prints one line per second with the raw sensor sample, the screen-frame
 * values and |a|. Hold the board in a known pose (flat / on each edge) and
 * compare with the expected numbers to validate the sensor->screen mapping
 * and the per-axis scale live — no debugger, no extra tooling, just the log.
 * See Kconfig.projbuild (RW1_IMU_DEBUG_LOG) for the expected values. */
#if CONFIG_RW1_IMU_DEBUG_LOG
static void imu_debug_log(const accel_input_sample_t *s, float sx, float sy, float sz)
{
    static uint32_t n = 0;
    const uint32_t period = 1000u / (uint32_t)CONFIG_RW1_SAMPLE_PERIOD_MS;
    if (period == 0 || ++n < period) {
        return;
    }
    n = 0;
    const float mag = sqrtf(s->x_g * s->x_g + s->y_g * s->y_g + s->z_g * s->z_g);
    ESP_LOGI(TAG, "imu: src=%s o=%d raw[%+.3f %+.3f %+.3f] scr[%+.3f %+.3f %+.3f] |a|=%.3f",
             s->source_name ? s->source_name : "?", accel_input_get_orientation(),
             s->x_g, s->y_g, s->z_g, sx, sy, sz, mag);
}
#endif

static void transport_task(void *arg)
{
    /* **不再等 WiFi 才开工**（2026-09-23 改）。
     *
     * 原来这里是 `while (!wifi_link_is_up()) vTaskDelay(500ms);`，后果是
     * **没网时板子连采样都不做** —— 屏幕没数据、方向词不更新、IMU 标定也做不了。
     * 但"看当前姿态""标定方向""看界面"这些恰恰都只需要本地数据，
     * 跟服务器没关系。实测就是这么卡住的：板子连不上 WiFi，整个前端像死机。
     *
     * 现在改成：**采样、刷新界面照常；只在没网时跳过上报。**
     * 代价是离线期间那几批样本不补发（缓冲区每周期照常清空）——
     * 换来的是一条硬得多的性质：**没网也能看、也能标定**。 */
    ESP_LOGI(TAG, "transport: 采样立即开始（没网时只跳过上报）");

    const TickType_t sample_ticks = pdMS_TO_TICKS(CONFIG_RW1_SAMPLE_PERIOD_MS);
    /* 一次就够：本函数同时把 URL、设备 id 和上报周期都刷一遍（见函数定义）。 */
    transport_reload_config();
    ESP_LOGI(TAG, "telemetry -> %s%s : %d samples @ %d ms, upload every %d ms",
             s_url, TX_PATH,
             (int)(CONFIG_RW1_TELEMETRY_PERIOD_MS / CONFIG_RW1_SAMPLE_PERIOD_MS),
             CONFIG_RW1_SAMPLE_PERIOD_MS, CONFIG_RW1_TELEMETRY_PERIOD_MS);

    TickType_t last_post = xTaskGetTickCount();
    int n = 0;
    char last_activity[TRANSPORT_ACTIVITY_LEN] = "";
    const char *last_source = "?";   /* IMU 型号，只在变化时才写进状态 */

    while (true) {
        accel_input_sample_t sample = {0};
        if (accel_input_poll(&sample)) {
            s_poll_fail = 0;
            last_source = sample.source_name ? sample.source_name : "?";
            /* Upload screen-frame axes so the server's tilt labels match what
             * the LCD shows (see accel_input_map_to_screen). z 也要映射 ——
             * 传感器可能是"竖着装"的（法线落在它的 y 轴上），只换 x/y 修不了。 */
            float sx, sy, sz;
            accel_input_map_to_screen(sample.x_g, sample.y_g, sample.z_g, &sx, &sy, &sz);
#if CONFIG_RW1_IMU_DEBUG_LOG
            imu_debug_log(&sample, sx, sy, sz);
#endif
            float xyz[3];
            if (sanitize3(sx, sy, sz, xyz)) {
                if (n < TX_BATCH_MAX) {
                    s_batch[n][0] = xyz[0];
                    s_batch[n][1] = xyz[1];
                    s_batch[n][2] = xyz[2];
                    n++;
                }

                /* 界面要"跟手"：`s_st` 原来**只在 500ms 的上报周期**才更新一次
                 * （2Hz），姿态球 2Hz 一跳，看着就是"迟钝"。
                 * 这里每 5 个样本（20Hz）把**给屏幕看的那几个值**刷一遍 ——
                 * 上报本身仍是 500ms 一批，判定/落盘也不受影响，纯粹是显示。
                 * 取 20Hz 而不是 100Hz：锁的临界区很短，但没必要每样本都抢一次。 */
                if ((n % 5) == 0) {
                    status_lock();
                    s_st.x_g = xyz[0];
                    s_st.y_g = xyz[1];
                    s_st.z_g = xyz[2];
                    strlcpy(s_st.source, last_source, sizeof(s_st.source));
                    status_unlock();
                }

                /* ---------- SD 卡本地留档 ----------
                 * **挂在采样循环里，不是挂在上报流程里** —— 离线时上报整段跳过，
                 * 挂那儿就变成"断网不记"，与"断网也能查"的初衷正好相反。
                 * 节奏：每 SD_LOG_EVERY_SAMPLES 个样本一行（100 Hz 下 ≈ 500 ms）。
                 * 注意这里在 `n++` **之后**，n 从 1 开始 —— 拿 TX_BATCH_MAX(128)
                 * 当模数就永远等不到：每批才 ~50 个样本就重置了（第一版就是这么错的）。 */
                if ((n % SD_LOG_EVERY_SAMPLES) == 0) {
                    char act[32];
                    status_lock();
                    strlcpy(act, s_st.activity, sizeof(act));
                    status_unlock();
                    sd_log_sample((uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS),
                                  act, xyz[0], xyz[1], xyz[2],
                                  sqrtf(xyz[0] * xyz[0] + xyz[1] * xyz[1] + xyz[2] * xyz[2]));
                }
                /* 正在执行远程指令就用同一个采样节拍累积，不额外阻塞 */
                feed_capture(xyz);
            } else {
                /* 坏样本整帧丢弃（**不填 0**，见 sanitize3 的注释）。
                 * 只计数不打日志，避免坏传感器把日志刷爆。 */
                s_bad_samples++;
            }
        } else {
            /* poll 只会在真实传感器源读失败时返回 false（demo/buttons 永远成功）。
             * 丢弃该样本并计数；限频告警，避免总线濒死时刷爆日志。
             * 注意这里**绝不能**用假数据顶上——那会把失重特征掩盖掉。 */
            if (s_poll_fail == 0 || (s_poll_fail % 1000u) == 0u) {
                ESP_LOGW(TAG, "accel read failed (streak %u, ~%u ms) — samples dropped",
                         (unsigned)(s_poll_fail + 1),
                         (unsigned)((s_poll_fail + 1) * CONFIG_RW1_SAMPLE_PERIOD_MS));
            }
            s_poll_fail++;
        }
        check_capture_timeout();

        const TickType_t now = xTaskGetTickCount();
        if (n > 0 && ((now - last_post) >= s_post_ticks || n >= TX_BATCH_MAX)) {
            bool ask = s_ask_pending;

            /* ⚠️ 必须在 post_batch **之前**把"这一帧要发出去的结果"记下来。
             *
             * 为什么：post_batch() 内部会解析服务器回复，而 LED 类指令是**同步执行**的
             * —— run_command() 直接调 finish_command()，于是 s_cmd.ready 在 post_batch
             * 返回**之前**就被设成了 true。原来的写法是 post_batch 之后无条件清 ready，
             * 结果"刚设好的结果"在同一轮里被立刻清掉，**永远发不出去**，
             * 服务器只能判超时（现象：灯确实闪了，网页上却显示超时/失败）。
             * capture_once 不中招，因为它只 start_capture()，结果要等 200ms 后由采样
             * 循环补上 —— 那时早过了清除点。所以这个 bug 只打 LED 指令。
             * （2026-09-23 真机实测：capture_once 往返 782ms 正常，led_blink / led_set
             *   连续 6 次全部 10 秒超时，就是这个原因。）
             *
             * 按 rid 精确清除而不是按布尔值：同一帧里完全可能"发走旧结果 + 收到新指令
             * 并立刻完成"，只比对布尔会把新结果一起清掉。 */
            char sent_rid[TRANSPORT_CMD_ID_LEN];
            const bool had_result = s_cmd.ready;
            sent_rid[0] = '\0';
            if (had_result) {
                strlcpy(sent_rid, s_cmd.rid, sizeof(sent_rid));
            }

            /* 没网就只跳过上报：采样和界面刷新照常（见任务开头那段注释）。
             * 缓冲区在这个块的末尾照常清零，所以离线不会把样本越攒越多。 */
            const bool wifi_up = wifi_link_is_up();
            bool ok = false;
            if (wifi_up) {
                ok = post_batch(n, ask);
            }

            /* 结果与按键计数只在成功送达后才清；失败就下一帧重发
             * （与 ask 的重试策略一致，不丢东西） */
            if (ok) {
                if (had_result && strcmp(s_cmd.rid, sent_rid) == 0) {
                    s_cmd.ready = false;
                }
                s_btn_pending = 0;
            }

            status_lock();
            s_st.batch_last = (uint16_t)n;
            s_st.orient = (uint8_t)accel_input_get_orientation();
            /* P0-2：写**屏幕坐标系**的值（即 s_batch 里已 map 过的），不是原始传感器值。
             * ui.c 的姿态球直接按 x_g/y_g 放点、不做二次翻转，所以这里必须是屏幕系——
             * 否则「板子屏幕上看到的倾斜方向」与「网页仪表盘」会相反。
             * 这正是 accel_input_map_to_screen 存在的唯一理由（见 accel_input.h）。
             * 顺带把原来的每样本更新收敛成每上报一次（P1-7）。 */
            s_st.x_g = s_batch[n - 1][0];
            s_st.y_g = s_batch[n - 1][1];
            s_st.z_g = s_batch[n - 1][2];
            strlcpy(s_st.source, last_source, sizeof(s_st.source));
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

void transport_reload_config(void)
{
    /* 地址与设备 id 都来自 net_config（NVS 优先 / 回退 Kconfig），配网改完立即生效。
     * 缓存成静态而不是每次上报都读 NVS：post_batch 和采样循环在同一个任务里，
     * 每次 2 Hz 去开关 NVS 句柄会白占锁、拖慢采样。 */
    net_config_t cfg;
    net_config_load(&cfg);
    strlcpy(s_url, cfg.url, sizeof(s_url));

    /* 上报周期也在这里刷新：`set_config` 改完周期后调本函数即可生效，
     * 不用重启。夹到 100..2000ms —— 太短会把 WiFi 压满，太长界面就不跟手了。 */
    int per = (int)cfg.period_ms;
    if (per < 50 || per > 2000) {
        per = CONFIG_RW1_TELEMETRY_PERIOD_MS;
    }
    s_post_ticks = pdMS_TO_TICKS(per);

    /* 设备 id：本函数会被 provisioning 的 httpd 任务调用，而 build_body 在 transport
     * 任务里读它。**值没变就不写** —— 开机写一次之后基本不再变动，把并发窗口压到最小
     * （万一真读到半截，服务端只会多出一个设备条目，随后自然过期淘汰，不影响数据）。 */
    char dev[NET_DEV_MAX];
    net_config_device_id(&cfg, dev, sizeof(dev));
    if (strcmp(dev, s_device) != 0) {
        status_lock();
        strlcpy(s_device, dev, sizeof(s_device));
        status_unlock();
    }

    ESP_LOGI(TAG, "server url -> '%s' device -> '%s' (source=%s)",
             s_url, s_device, net_config_source());
}

void transport_request_ask(const char *question)
{
    /* 本函数在 iot_button 的任务里跑，而 s_ask_text / s_btn_pending 由 transport
     * 任务读写 —— 必须走同一把锁。64 字节的 strlcpy 是非原子写，不加锁时
     * build_body 可能读到半新半旧的问句（服务端收到乱码），
     * s_btn_pending++ 的读-改-写也可能丢计数。
     * 注意：s_lock 由 transport_start() 创建，而按钮注册必须排在它之后
     * （见 main.c 里的调用顺序），否则这里会取到 NULL 锁。 */
    status_lock();
    if (question != NULL && question[0] != '\0') {
        strlcpy(s_ask_text, question, sizeof(s_ask_text));
    }
    s_ask_attempts = 0;
    s_ask_pending = true;
    s_btn_pending++;          /* 让服务端/网页能看见"物理按键发生了" */
    status_unlock();

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
