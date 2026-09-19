/*
 * SPDX-License-Identifier: MIT
 *
 * LED 物理反馈 —— 第 3 周「按键触发 + 本地/远端物理反馈闭环」的执行端。
 *
 * 板载硬件：ESP32-S3-EYE 在 GPIO3 上有一颗普通 LED（BSP_CAPS_LED=1），
 * **没有喇叭**（BSP_CAPS_AUDIO_SPEAKER=0，只有麦克风），所以"物理反馈"就用这颗灯。
 *
 * BSP 只暴露 bsp_led_set(on/off) 和 4 个内置效果（on/off/快闪/慢闪，且都是**无限循环**），
 * 做不了"闪 3 次然后停"这类事件驱动的图案，所以这里自己写了个很小的图案播放器：
 * 每个图案是一串「亮/灭 + 持续多久」的段，由 esp_timer 逐段推进，**不阻塞任何任务**。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 预置图案。每个都自带收尾的"灭"，不会卡在亮的状态。 */
typedef enum {
    LED_FB_ACK = 0,     /*!< 1 短闪（60ms）—— 按键被识别（纯本地反馈，最即时） */
    LED_FB_REPLY,       /*!< 2 短闪 —— 服务器/AI 回复到达 */
    LED_FB_CMD,         /*!< 2 中闪（120ms）—— 收到并开始执行远程指令 */
    LED_FB_ALERT,       /*!< 3 快闪（80ms）—— 远端告警（如服务端判定跌落） */
    LED_FB_ERROR,       /*!< 长亮 1 秒 —— 链路或指令失败 */
    LED_FB_PATTERN_MAX,
} led_fb_pattern_t;

/** 单次自定义闪烁最多几次（远端 led_blink 指令的上限）。 */
#define LED_FB_MAX_BLINKS 12

/** 初始化 LED 与定时器。没有 LED 时返回错误，调用方可以忽略（不致命）。 */
esp_err_t led_feedback_init(void);

/** 播放一个预置图案（会打断正在播的那个）。 */
void led_feedback_play(led_fb_pattern_t p);

/** 播放自定义闪烁：亮 on_ms、灭 off_ms，重复 n 次（远端 led_blink 用）。 */
void led_feedback_blink(int n, uint16_t on_ms, uint16_t off_ms);

/** 常亮 / 熄灭（会打断正在播的图案）。远端 led_set 用。 */
void led_feedback_steady(bool on);

#ifdef __cplusplus
}
#endif
