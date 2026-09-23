/*
 * SPDX-License-Identifier: MIT
 *
 * 把遥测摘要与事件按天追加到 SD 卡上 —— 网络断了也能查，卡在手上就有留档。
 *
 * 设计取舍（都是踩过才定的）：
 *  1) **只记摘要，不记原始样本**。100Hz 的三轴原始数据是 ~3 KB/s = 10 MB/小时，
 *     2.4 GB 的卡只能撑 10 天，而且写放大对卡的寿命很不友好。
 *     每个上报周期记**一行摘要**（活动/三轴/|a|/步数）≈ 200 B/s，
 *     同样一张卡能撑几个月。
 *  2) **文件名必须是 8.3**（`CONFIG_FATFS_LFN_NONE=y`，长文件名是关的）。
 *     所以是 `260923.LOG` 这种，不是 `2026-09-23.log`。
 *     时钟没同步（SNTP 没成功）时退回 `RW1.LOG` 单文件，绝不因为"取不到日期"就不记。
 *  3) **每写一行就 fflush**：一次 500 ms，代价可以忽略；换来的是
 *     "拔卡/断电时最后几秒的数据不丢"。不做 fsync（那才是真磨损）。
 *  4) **剩余空间低于阈值就停写并只警告一次**：卡写满了继续写会让 fopen/fwrite
 *     反复失败刷屏，而且 FAT 表损坏的风险变高。
 */
#include "sd_log.h"

#include <stdio.h>
#include <string.h>
#include <time.h>

#include "bsp/esp-bsp.h"      /* BSP_SD_MOUNT_POINT */
#include "esp_log.h"
#include "esp_vfs_fat.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sd_card.h"

static const char *TAG = "sd_log";

#define SD_LOG_DIR        BSP_SD_MOUNT_POINT
#define SD_LOG_FALLBACK   SD_LOG_DIR "/RW1.LOG"     /* 时钟没同步时用 */
#define SD_LOG_MAX_BYTES  (512 * 1024)              /* 单个文件上限，超了换下一个 */
#define SD_LOG_MIN_FREE   (8ULL * 1024 * 1024)      /* 剩余低于 8 MB 就停写 */
#define SD_LOG_SPACE_EVERY 60                       /* 每 60 行查一次剩余空间 */
#define SD_LOG_REPORT_EVERY 600                     /* 每 600 行（约 5 分钟）回报一次落盘字节数 */

static FILE *s_file = NULL;
static char s_path[64] = { 0 };
static int s_day = -1;              /* 当前文件的"日"键，-1 = 还没建 */
static unsigned s_lines = 0;
static unsigned s_lines_total = 0;
static bool s_low_space = false;

static bool sd_log_has_clock(void)
{
    /* SNTP 没同步时 time() 返回的是 1970 附近的值；2020-01-01 之前一律当作"没时钟" */
    return time(NULL) > 1577836800;
}

static int sd_log_day_key(void)
{
    if (!sd_log_has_clock()) {
        return -1;
    }
    time_t now = time(NULL);
    struct tm tm_now;
    localtime_r(&now, &tm_now);
    return (tm_now.tm_year + 1900) * 10000 + (tm_now.tm_mon + 1) * 100 + tm_now.tm_mday;
}

/* 打开（或换到）今天的文件。返回 false 表示这次写不了（没卡/没空间/开不了）。 */
static bool sd_log_open_for_today(void)
{
    if (!sd_card_mounted()) {
        return false;
    }

    int day = sd_log_day_key();
    if (s_file != NULL && day == s_day) {
        return true;
    }
    if (s_file != NULL) {
        fclose(s_file);
        s_file = NULL;
    }

    char path[64];
    if (day < 0) {
        snprintf(path, sizeof(path), "%s", SD_LOG_FALLBACK);
    } else {
        /* 8.3：YYMMDD.LOG */
        snprintf(path, sizeof(path), "%s/%02d%02d%02d.LOG",
                 SD_LOG_DIR, (day / 10000) % 100, (day / 100) % 100, day % 100);
    }

    FILE *f = fopen(path, "a");
    if (f == NULL) {
        ESP_LOGW(TAG, "打不开 %s（没插卡？卡只读？）", path);
        return false;
    }
    /* 新文件写一行表头，让文件自己说明格式 —— 半年后回来看也知道每列是什么 */
    long sz = ftell(f);
    if (sz <= 0) {
        fprintf(f, "# rw1 log v1 | T,ms,act,x,y,z,mag | E,ms,kind,text\n");
        fflush(f);
    }
    s_file = f;
    strlcpy(s_path, path, sizeof(s_path));
    s_day = day;
    s_lines = 0;
    s_low_space = false;
    ESP_LOGI(TAG, "日志文件 %s", path);
    return true;
}

static bool sd_log_space_ok(void)
{
    if (s_lines % SD_LOG_SPACE_EVERY != 0) {
        return true;
    }
    uint64_t total = 0, free_b = 0;
    if (esp_vfs_fat_info(SD_LOG_DIR, &total, &free_b) != ESP_OK) {
        return true;                /* 查不到就不拦，别因为一次查询失败就停记 */
    }
    if (free_b < SD_LOG_MIN_FREE) {
        if (!s_low_space) {
            s_low_space = true;
            ESP_LOGW(TAG, "SD 剩余空间不足（%.1f MB）—— 停止记录，清卡或格式化后重启即可恢复",
                     (double)free_b / (1024.0 * 1024.0));
        }
        return false;
    }
    s_low_space = false;
    return true;
}

static void sd_log_write(const char *line)
{
    if (s_file == NULL || s_low_space) {
        return;
    }
    fputs(line, s_file);
    fflush(s_file);                 /* 见文件头第 3 条 */
    s_lines++;
    s_lines_total++;

    /* **自证**：`fopen` 成功不等于写进去了（卡写保护、FAT 满、接触不良都可能
     * 让写入静默失败）。所以头几行之后、以及之后每 SD_LOG_REPORT_EVERY 行，
     * 回报一次"实际落盘多少字节"。没有这行日志，"留档在工作"就只是猜测。 */
    if (s_lines == 10 || s_lines % SD_LOG_REPORT_EVERY == 0) {
        long sz = ftell(s_file);
        ESP_LOGI(TAG, "已写入 %u 行 / %ld 字节（%s）", s_lines_total, sz, s_path);
    }
}

esp_err_t sd_log_init(void)
{
    if (!sd_card_mounted()) {
        ESP_LOGI(TAG, "没有 SD 卡，本地留档功能关闭（其它功能不受影响）");
        return ESP_ERR_INVALID_STATE;
    }
    if (!sd_log_open_for_today()) {
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "本地留档已开启（%s）", s_path);
    return ESP_OK;
}

void sd_log_close(void)
{
    if (s_file != NULL) {
        fflush(s_file);
        fclose(s_file);
        s_file = NULL;
    }
    s_day = -1;
}

void sd_log_sample(uint32_t uptime_ms, const char *activity,
                   float x, float y, float z, float mag)
{
    if (s_file == NULL && !sd_log_open_for_today()) {
        return;
    }
    if (!sd_log_space_ok()) {
        return;
    }
    char line[128];
    snprintf(line, sizeof(line), "T,%lu,%s,%.3f,%.3f,%.3f,%.3f\n",
             (unsigned long)uptime_ms,
             (activity != NULL && activity[0] != '\0') ? activity : "-",
             (double)x, (double)y, (double)z, (double)mag);
    sd_log_write(line);
}

void sd_log_event(const char *kind, const char *text)
{
    if (s_file == NULL && !sd_log_open_for_today()) {
        return;
    }
    char line[160];
    /* text 可能带中文/逗号，用双引号包起来，方便以后直接导成 CSV */
    snprintf(line, sizeof(line), "E,%lu,%s,\"%s\"\n",
             (unsigned long)(xTaskGetTickCount() * portTICK_PERIOD_MS),
             (kind != NULL) ? kind : "?",
             (text != NULL) ? text : "");
    sd_log_write(line);
}

bool sd_log_active(void)
{
    return s_file != NULL && !s_low_space;
}

unsigned sd_log_lines(void)
{
    return s_lines_total;
}

const char *sd_log_path(void)
{
    return s_path;
}
