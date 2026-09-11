/*
 * SPDX-License-Identifier: CC0-1.0
 *
 * AI交互课 第1周 · 开发板端应用
 *
 * Flow:  on-board IMU --(HTTP POST /api/telemetry)--> PC server (server.py)
 *        PC server classifies the motion ("AI") and answers --> shown here.
 *        BOOT single click  =>  next telemetry frame carries ask=true, the
 *        server returns a fresh AI summary/reply.
 *
 *   - bsp_display_start():   LCD + LVGL
 *   - accel_input_init():    SC7A20/LIS3DH/MPU6050/QMA7981 auto-detect
 *   - ui_init():             status/activity/AI-reply screen
 *   - wifi_link_start():     WiFi STA (+ Aliyun SNTP for log timestamps)
 *   - transport_start():     telemetry task talking to the PC server
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
    ESP_LOGI(TAG, "BOOT click -> ask the PC server AI");
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    bsp_display_start();
    bsp_display_backlight_on();

    ESP_ERROR_CHECK_WITHOUT_ABORT(accel_input_init());

    ESP_ERROR_CHECK(ui_init());
    setup_ask_button();

    wifi_link_start();
    transport_start();

    ESP_LOGI(TAG, "rw1 board app: IMU -> %s%s", CONFIG_RW1_SERVER_URL, "/api/telemetry");
}
