/*
 * SPDX-License-Identifier: MIT
 *
 * AI交互课 第1周 · 开发板端应用
 *
 * Flow:  on-board IMU --(HTTP POST /api/telemetry, batched)--> PC server (server.py)
 *        PC server classifies the motion ("AI") and answers --> shown here.
 *        BOOT single click  => next telemetry frame carries ask=true; the server
 *                              replies immediately with "正在思考…" and delivers
 *                              the real answer on a later frame.
 *        BOOT long press    => cycle the tilt calibration (which way is "up"),
 *                              persisted in NVS, no rebuild needed.
 *
 *   - bsp_display_start():   LCD + LVGL
 *   - accel_input_init():    SC7A20/LIS3DH/MPU6050/QMA7981 auto-detect
 *   - ui_init():             status/activity/AI-reply screen
 *   - wifi_link_start():     WiFi STA (+ Aliyun SNTP for log timestamps)
 *   - transport_start():     IMU sampling + batched telemetry to the PC server
 *
 * Reuses the battle-tested accel_input module of the parent biaopan project.
 */
#include "bsp/esp-bsp.h"
#include "esp_log.h"
#include "iot_button.h"
#include "nvs_flash.h"

#include "accel_input.h"
#include "transport.h"
#include "ui.h"
#include "wifi_link.h"

static const char *TAG = "main";

static button_handle_t s_buttons[BSP_BUTTON_NUM];

static void on_ask_click(void *btn, void *arg)
{
    (void)btn;
    (void)arg;
    transport_request_ask("我现在的运动状态怎么样？");
}

/* If the reported tilt direction runs the wrong way, long-press BOOT until the
 * screen and the dashboard agree. The choice is stored in NVS and is also
 * applied to the telemetry, so the server's labels stay consistent with it. */
static void on_orient_long_press(void *btn, void *arg)
{
    (void)btn;
    (void)arg;
    accel_input_cycle_orientation();
    ESP_LOGI(TAG, "tilt calibration -> orientation %d (long-press BOOT to cycle)",
             accel_input_get_orientation());
}

static void setup_ask_button(void)
{
    button_handle_t ask_btn = NULL;

    if (accel_input_uses_buttons()) {
        /* accel_input owns the button handles in fallback mode. */
        ask_btn = accel_input_button(BSP_BUTTON_5);
    } else if (bsp_iot_button_create(s_buttons, NULL, BSP_BUTTON_NUM) == ESP_OK) {
        ask_btn = s_buttons[BSP_BUTTON_5];
    }

    if (ask_btn == NULL) {
        ESP_LOGW(TAG, "no BOOT button available; ask-by-button disabled");
        return;
    }
    iot_button_register_cb(ask_btn, BUTTON_SINGLE_CLICK, NULL, on_ask_click, NULL);
    iot_button_register_cb(ask_btn, BUTTON_LONG_PRESS_START, NULL, on_orient_long_press, NULL);
    ESP_LOGI(TAG, "BOOT: click -> ask the PC server AI, long-press -> cycle tilt calibration");
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    if (bsp_display_start() == NULL) {
        ESP_LOGE(TAG, "display init failed");
        return;
    }
    bsp_display_backlight_on();

    ESP_ERROR_CHECK_WITHOUT_ABORT(accel_input_init());

    ESP_ERROR_CHECK(ui_init());
    setup_ask_button();

    wifi_link_start();
    transport_start();

    ESP_LOGI(TAG, "rw1 board app: IMU %dHz -> batch -> %s%s",
             (int)(1000 / CONFIG_RW1_SAMPLE_PERIOD_MS),
             CONFIG_RW1_SERVER_URL, "/api/telemetry");
}
