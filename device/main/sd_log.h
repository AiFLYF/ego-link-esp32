/*
 * SPDX-License-Identifier: MIT
 *
 * 遥测摘要 / 事件的 SD 卡本地留档。需要先 sd_card_init() 成功挂上卡。
 *
 * 一行样本：
 *     T,<开机毫秒>,<活动词>,<x>,<y>,<z>,<|a|>
 *   （**没有步数列**：步数是服务端按窗口算出来的，板端并不掌握 —— 不写自己不知道的数据。）
 * 一行事件：
 *     E,<开机毫秒>,<类型>,"<文本>"
 * 文件按天：`/sdcard/260923.LOG`（**8.3 短名**，长文件名是关的）；
 * 时钟没同步（SNTP 失败）时退化成 `/sdcard/RW1.LOG` 单文件。
 *
 * 所有函数都**容忍没卡**：没挂载时静默返回，不报错、不拦主流程。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

/** @brief 打开（或换到）当天的日志文件。没卡返回 ESP_ERR_INVALID_STATE。 */
esp_err_t sd_log_init(void);

/** @brief 关闭当前文件。**格式化卡之前必须先调**，否则句柄指向失效的 FAT 表。 */
void sd_log_close(void);

/** @brief 记一行遥测摘要。 */
void sd_log_sample(uint32_t uptime_ms, const char *activity,
                   float x, float y, float z, float mag);

/** @brief 记一行事件（跌落/摇晃/指令结果/AI 回复…）。 */
void sd_log_event(const char *kind, const char *text);

/** @brief 当前是否在正常记录（已打开且空间够）。 */
bool sd_log_active(void);

/** @brief 累计写过的行数（给界面/日志看）。 */
unsigned sd_log_lines(void);

/** @brief 当前日志文件路径（空串表示没在记）。 */
const char *sd_log_path(void);
