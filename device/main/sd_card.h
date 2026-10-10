/*
 * SPDX-License-Identifier: MIT
 *
 * SD 卡（microSD）挂载与自检。
 *
 * 硬件：ESP32-S3-EYE 板载 microSD 卡座，走 **1 位 SDMMC** 模式
 *       CLK=GPIO39  CMD=GPIO38  D0=GPIO40（D1~D3 未接；无卡检测脚 DET）。
 *       这 3 个脚在本项目里没有被摄像头/LCD/按键占用，可放心使用。
 *
 * 软件：BSP 组件已声明 `fatfs vfs esp_driver_sdmmc` 依赖，sdkconfig 里 FATFS
 *       也已开启（CONFIG_FATFS_VOLUME_COUNT=2），挂载点固定 `/sdcard`
 *       （Kconfig `BSP_SD_MOUNT_POINT`）——所以本模块不含任何额外配置。
 *
 * 用法：
 *     if (sd_card_init() == ESP_OK) {
 *         FILE *f = fopen("/sdcard/xxx.txt", "w");   // 之后就是标准 C 文件 API
 *     }
 *
 * 注意（两个真实会踩的坑）：
 *   1) 卡必须是 **FAT32**。ESP-IDF 的 FATFS 只认 FAT12/16/32，**不认 exFAT/NTFS**，
 *      而 >32GB 的卡出厂多半是 exFAT → 挂载会失败。
 *   2) 当前 sdkconfig 是 `CONFIG_FATFS_LFN_NONE=y`，**长文件名/中文名不可用**，
 *      只能用 8.3 短名（如 `TEST123.TXT`）。要中文名须 menuconfig 开 LFN + codepage 936。
 */
#pragma once

#include <stdbool.h>
#include "esp_err.h"

/**
 * @brief 挂载 /sdcard。重复调用安全（已挂载则直接返回 ESP_OK）。
 *
 * @return ESP_OK 成功；否则为 SDMMC/FATFS 的错误码。调用方应容忍失败——
 *         SD 卡是可选外设，不该因为没插卡就拦下开机。
 */
esp_err_t sd_card_init(void);

/** @brief 是否已挂载成功。 */
bool sd_card_mounted(void);

/** @brief 卸载并释放资源（正常业务用不到，留给"换卡"场景）。 */
esp_err_t sd_card_deinit(void);

/**
 * @brief **格式化**成 FAT32（卡里原来的东西全部消失）。
 *
 * 用途：卡里若是长文件名/中文名的旧文件，在 LFN 关闭的配置下**板子既看不见也删不掉**
 * （VFS 访问不到），只能整卡格式化才拿得回空间。
 *
 * 调用方必须先 `sd_log_close()` —— 否则日志的文件句柄会指向失效的 FAT 表。
 * @return ESP_OK 成功；未挂载返回 ESP_ERR_INVALID_STATE。
 */
esp_err_t sd_card_format(void);

/** @brief 把卡的容量/剩余空间打进日志。未挂载时静默返回。 */
void sd_card_log_info(void);

/**
 * @brief 读写自检：往 /sdcard/rw1_probe.txt 写一行再读回逐字比对。
 * @return ESP_OK 表示"真能读能写"；未挂载返回 ESP_ERR_INVALID_STATE。
 */
esp_err_t sd_card_selftest(void);
