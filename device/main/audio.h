/*
 * SPDX-License-Identifier: MIT
 *
 * 板载麦克风录音（第 4 周「按键说话」）。
 *
 * ESP32-S3-EYE 有**麦克风、没有喇叭**（BSP_CAPS_AUDIO_MIC=1 / BSP_CAPS_AUDIO_SPEAKER=0），
 * 所以这一版只做"听"：录一段 PCM 交给服务端识别，回复走屏幕显示。
 * TTS 留到以后 —— 板子得先外接喇叭，否则做了也听不见。
 *
 * 录音缓冲放 PSRAM：3 秒 @16 kHz/16 bit 单声道 = 96 KB，内部 SRAM 放不下，
 * 也没必要为它挤占采样/网络要用的空间。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define AUDIO_RATE_HZ    16000
#define AUDIO_RECORD_MS  3000
#define AUDIO_MAX_BYTES  (AUDIO_RATE_HZ * 2 * AUDIO_RECORD_MS / 1000)

/**
 * 初始化麦克风。失败**不拦开机** —— 语音只是多一路输入，
 * 采集/上报/UI 都不该因为它挂掉。
 */
esp_err_t audio_init(void);

/** 麦克风是否可用。录音前先问这个，免得白等 3 秒才失败。 */
bool audio_ready(void);

/**
 * 录一段 PCM（**阻塞**约 AUDIO_RECORD_MS）。成功返回缓冲指针并写 *out_len；
 * 失败返回 NULL。缓冲归本模块所有，调用方不要 free，下次录音会覆盖它。
 */
const uint8_t *audio_record(size_t *out_len);

#ifdef __cplusplus
}
#endif
