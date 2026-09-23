/*
 * SPDX-License-Identifier: MIT
 *
 * SD 卡挂载与自检的实现。只依赖 BSP 暴露的三个函数：
 *   bsp_sdcard_mount() / bsp_sdcard_unmount() / bsp_sdcard_get_handle()
 * 把"引脚、总线宽度、挂载点、格式化策略"全部交给 BSP，本文件不做任何硬件配置。
 */
#include "sd_card.h"

#include <stdio.h>
#include <string.h>

#include "bsp/esp-bsp.h"
#include "esp_log.h"
#include "esp_vfs_fat.h"

static const char *TAG = "sd_card";

/* 挂载点来自 BSP Kconfig，默认 "/sdcard" */
#define SD_MOUNT_POINT  BSP_SD_MOUNT_POINT
/* 文件名必须是**合法 8.3**：当前 sdkconfig 是 CONFIG_FATFS_LFN_NONE=y，
 * 长文件名被关掉了 —— 主干超过 8 个字符的 `rw1_probe.txt` 在真卡上 fopen 会**直接失败**，
 * 自检就永远是"打开失败"。（头文件里写了这个坑，但第一版代码自己踩了。
 * 想用长名/中文名要先在 menuconfig 开 FATFS_LFN_* + CODEPAGE_936。） */
#define SD_PROBE_PATH   SD_MOUNT_POINT "/RW1PROBE.TXT"
#define SD_PROBE_TEXT   "rw1 sd probe ok"

static bool s_mounted = false;

esp_err_t sd_card_init(void)
{
    if (s_mounted) {
        return ESP_OK;
    }

    esp_err_t ret = bsp_sdcard_mount();
    if (ret != ESP_OK) {
        /* 把最常见的两种成因直接写进日志，省得再去翻手册：
         *   没插卡 / 接触不良      -> ESP_ERR_TIMEOUT、ESP_ERR_NOT_FOUND
         *   卡不是 FAT32（exFAT）  -> ESP_FAIL
         * 想让它首次挂载失败自动格成 FAT32：menuconfig ->
         *   Board Support Package -> Storage -> SD card ->
         *   勾 "Format SD card if mounting fails"。 */
        ESP_LOGE(TAG, "SD 挂载失败: %s（多为没插卡，或卡不是 FAT32）",
                 esp_err_to_name(ret));
        return ret;
    }

    s_mounted = true;

    sdmmc_card_t *card = bsp_sdcard_get_handle();
    if (card != NULL) {
        /* cid.name 只有 8 字节、不保证以 '\0' 结尾 → 用 %.8s 限长打印 */
        ESP_LOGI(TAG, "SD 挂载成功: name=%.8s bus=%d-bit %d kHz",
                 card->cid.name, 1 << card->log_bus_width,
                 (int)card->real_freq_khz);
    }
    sd_card_log_info();
    return ESP_OK;
}

bool sd_card_mounted(void)
{
    return s_mounted;
}

esp_err_t sd_card_deinit(void)
{
    if (!s_mounted) {
        return ESP_OK;
    }
    esp_err_t ret = bsp_sdcard_unmount();
    s_mounted = false;
    return ret;
}

void sd_card_log_info(void)
{
    if (!s_mounted) {
        return;
    }
    uint64_t total = 0;
    uint64_t free_bytes = 0;
    if (esp_vfs_fat_info(SD_MOUNT_POINT, &total, &free_bytes) == ESP_OK) {
        ESP_LOGI(TAG, "容量 %.2f GB / 剩余 %.2f GB",
                 (double)total / (1024.0 * 1024.0 * 1024.0),
                 (double)free_bytes / (1024.0 * 1024.0 * 1024.0));
    } else {
        ESP_LOGW(TAG, "读取容量失败（esp_vfs_fat_info）");
    }
}

esp_err_t sd_card_format(void)
{
    if (!s_mounted) {
        return ESP_ERR_INVALID_STATE;
    }
    /* 句柄必须在**卸载之前**取：bsp_sdcard_unmount() 会把内部的 bsp_sdcard 置 NULL。
     * 而 IDF 自己的测试（fatfs/test_apps/sdcard）就是在**挂载状态下**调
     * esp_vfs_fat_sdcard_format() 的，所以不需要先卸载。 */
    sdmmc_card_t *card = bsp_sdcard_get_handle();
    if (card == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    ESP_LOGW(TAG, "开始格式化 %s —— 卡里原有内容会全部丢失", SD_MOUNT_POINT);
    esp_err_t ret = esp_vfs_fat_sdcard_format(SD_MOUNT_POINT, card);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "格式化失败: %s", esp_err_to_name(ret));
        return ret;
    }
    ESP_LOGI(TAG, "格式化完成");
    sd_card_log_info();          /* 打完立刻把新容量报出来，方便核对 */
    return ESP_OK;
}

esp_err_t sd_card_selftest(void)
{
    if (!s_mounted) {
        return ESP_ERR_INVALID_STATE;
    }

    /* ---- 写 ---- */
    FILE *f = fopen(SD_PROBE_PATH, "w");
    if (f == NULL) {
        ESP_LOGE(TAG, "自检：打开 %s 失败（写）", SD_PROBE_PATH);
        return ESP_FAIL;
    }
    fprintf(f, "%s\n", SD_PROBE_TEXT);
    fclose(f);

    /* ---- 读回 ---- */
    char buf[32] = { 0 };
    FILE *rf = fopen(SD_PROBE_PATH, "r");
    if (rf == NULL) {
        ESP_LOGE(TAG, "自检：打开 %s 失败（读）", SD_PROBE_PATH);
        return ESP_FAIL;
    }
    char *got = fgets(buf, sizeof(buf), rf);
    fclose(rf);
    if (got == NULL) {
        ESP_LOGE(TAG, "自检：读回为空");
        return ESP_FAIL;
    }

    /* 去掉行尾 CR/LF 再比对 */
    for (size_t i = 0; i < sizeof(buf); i++) {
        if (buf[i] == '\r' || buf[i] == '\n') {
            buf[i] = '\0';
            break;
        }
    }
    if (strcmp(buf, SD_PROBE_TEXT) != 0) {
        ESP_LOGE(TAG, "自检：内容不符（写 '%s' / 读 '%s'）", SD_PROBE_TEXT, buf);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "自检 PASS：可读可写（%s）", SD_PROBE_PATH);
    return ESP_OK;
}
