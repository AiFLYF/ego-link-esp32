/*
 * SPDX-License-Identifier: MIT
 * See led_feedback.h.
 */
#include "led_feedback.h"

#include <string.h>

#include "bsp/esp-bsp.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "led_indicator.h"

static const char *TAG = "led_fb";

/* 一段：亮/灭 + 持续多久。图案的最后一段必须是「灭」，否则播完会卡在亮。 */
typedef struct {
    bool     on;
    uint16_t ms;
} led_seg_t;

static const led_seg_t P_ACK[]   = {{true,  60}, {false,   0}};
static const led_seg_t P_REPLY[] = {{true,  60}, {false,  90}, {true, 60}, {false, 0}};
static const led_seg_t P_CMD[]   = {{true, 120}, {false, 120}, {true, 120}, {false, 0}};
static const led_seg_t P_ALERT[] = {{true,  80}, {false,  80}, {true, 80},
                                    {false, 80}, {true,  80}, {false, 0}};
static const led_seg_t P_ERROR[] = {{true, 1000}, {false, 0}};

static const led_seg_t *const PATTERNS[LED_FB_PATTERN_MAX] = {
    [LED_FB_ACK]   = P_ACK,
    [LED_FB_REPLY] = P_REPLY,
    [LED_FB_CMD]   = P_CMD,
    [LED_FB_ALERT] = P_ALERT,
    [LED_FB_ERROR] = P_ERROR,
};

static const int PATTERN_LEN[LED_FB_PATTERN_MAX] = {
    [LED_FB_ACK]   = sizeof(P_ACK) / sizeof(P_ACK[0]),
    [LED_FB_REPLY] = sizeof(P_REPLY) / sizeof(P_REPLY[0]),
    [LED_FB_CMD]   = sizeof(P_CMD) / sizeof(P_CMD[0]),
    [LED_FB_ALERT] = sizeof(P_ALERT) / sizeof(P_ALERT[0]),
    [LED_FB_ERROR] = sizeof(P_ERROR) / sizeof(P_ERROR[0]),
};

static led_indicator_handle_t s_led;
static esp_timer_handle_t s_timer;

/* 播放状态：由定时器回调和调用方共同读写，量很小，用临界区保护即可。 */
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static const led_seg_t *s_segs;      /* 正在播的图案（NULL = 没在播） */
static int s_n;
static int s_cur;

/* 自定义闪烁的临时图案缓冲（远端 led_blink 用） */
static led_seg_t s_dyn[LED_FB_MAX_BLINKS * 2 + 1];

static void led_apply(bool on)
{
    if (s_led != NULL) {
        bsp_led_set(s_led, on);
    }
}

/* 停掉当前图案。先把指针清空，这样万一回调正好在跑也只会空转一下。 */
static void stop_pattern(void)
{
    portENTER_CRITICAL(&s_mux);
    s_segs = NULL;
    s_n = 0;
    s_cur = 0;
    portEXIT_CRITICAL(&s_mux);
    if (s_timer != NULL) {
        esp_timer_stop(s_timer);     /* 没在跑时会返回 INVALID_STATE，无所谓 */
    }
}

static void play_segs(const led_seg_t *segs, int n)
{
    if (s_timer == NULL || segs == NULL || n <= 0) {
        return;
    }
    stop_pattern();

    portENTER_CRITICAL(&s_mux);
    s_segs = segs;
    s_n = n;
    s_cur = 1;                       /* 第 0 段立刻生效 */
    portEXIT_CRITICAL(&s_mux);

    led_apply(segs[0].on);
    if (n > 1 && segs[0].ms > 0) {
        esp_timer_start_once(s_timer, (uint64_t)segs[0].ms * 1000);
    }
}

static void led_timer_cb(void *arg)
{
    (void)arg;

    const led_seg_t *segs;
    int cur, n;

    portENTER_CRITICAL(&s_mux);
    segs = s_segs;
    n = s_n;
    cur = s_cur;
    if (cur < n) {
        s_cur = cur + 1;
    }
    portEXIT_CRITICAL(&s_mux);

    if (segs == NULL || cur >= n) {
        return;
    }

    /* bsp_led_set 在临界区之外调用：它内部会走 led_indicator 的锁，别在关中断时干这个 */
    led_apply(segs[cur].on);

    if ((cur + 1) < n && segs[cur].ms > 0) {
        esp_timer_start_once(s_timer, (uint64_t)segs[cur].ms * 1000);
    }
}

esp_err_t led_feedback_init(void)
{
    led_indicator_handle_t leds[BSP_LED_NUM] = {0};
    int cnt = 0;

    esp_err_t ret = bsp_led_indicator_create(leds, &cnt, BSP_LED_NUM);
    if (ret != ESP_OK || cnt < 1) {
        ESP_LOGE(TAG, "no LED available (%s) - feedback disabled", esp_err_to_name(ret));
        return (ret == ESP_OK) ? ESP_FAIL : ret;
    }
    s_led = leds[0];

    const esp_timer_create_args_t targs = {
        .callback = led_timer_cb,
        .name = "led_fb",
    };
    ret = esp_timer_create(&targs, &s_timer);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "timer create failed: %s", esp_err_to_name(ret));
        s_led = NULL;
        return ret;
    }

    led_apply(false);
    ESP_LOGI(TAG, "led feedback ready (GPIO%d)", BSP_LED_1_IO);
    return ESP_OK;
}

void led_feedback_play(led_fb_pattern_t p)
{
    if (p < 0 || p >= LED_FB_PATTERN_MAX || PATTERNS[p] == NULL) {
        return;
    }
    play_segs(PATTERNS[p], PATTERN_LEN[p]);
}

void led_feedback_blink(int n, uint16_t on_ms, uint16_t off_ms)
{
    if (n < 1) {
        n = 1;
    }
    if (n > LED_FB_MAX_BLINKS) {
        n = LED_FB_MAX_BLINKS;
    }
    /* 夹一下时长：远端参数是外部输入，别让一条指令把灯焊死或闪成一片模糊 */
    if (on_ms  < 20)   { on_ms  = 20; }
    if (on_ms  > 5000) { on_ms  = 5000; }
    if (off_ms < 20)   { off_ms = 20; }
    if (off_ms > 5000) { off_ms = 5000; }

    int k = 0;
    for (int i = 0; i < n; i++) {
        s_dyn[k].on = true;
        s_dyn[k].ms = on_ms;
        k++;
        s_dyn[k].on = false;
        s_dyn[k].ms = off_ms;
        k++;
    }
    s_dyn[k].on = false;             /* 收尾：确保最后是灭的 */
    s_dyn[k].ms = 0;
    k++;

    play_segs(s_dyn, k);
}

void led_feedback_steady(bool on)
{
    stop_pattern();
    led_apply(on);
}
