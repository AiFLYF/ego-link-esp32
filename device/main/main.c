/*
 * SPDX-License-Identifier: MIT
 *
 * AI交互课 第3周 · 开发板端应用 —— 按键触发 + 本地/远端物理反馈闭环
 *
 * Flow:  on-board IMU --(HTTP POST /api/telemetry, batched)--> PC server (server.py)
 *        PC server classifies the motion ("AI") and answers --> shown here.
 *        BOOT single click  => LED 立刻闪一下（**本地**物理反馈，不等网络），
 *                              下一帧遥测带上 ask=true；服务器立刻回"正在思考…"，
 *                              大模型的答案由后续帧带回，到达时再闪两下（**远端**反馈）。
 *        BOOT long press    => cycle the tilt calibration (which way is "up"),
 *                              persisted in NVS, no rebuild needed.
 *        远端指令           => 服务器可下发 led_blink / led_set 驱动这颗灯，
 *                              例如判定跌落时自动快闪 3 次做物理告警。
 *
 *   - bsp_display_start():   LCD + LVGL
 *   - accel_input_init():    SC7A20/LIS3DH/MPU6050/QMA7981 auto-detect
 *   - led_feedback_init():   板载 LED（GPIO3）图案反馈
 *   - sd_card_init():        可选：挂载板载 microSD 到 /sdcard 并读写自检
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
#include "led_feedback.h"
#include "net_config.h"
#include "provisioning.h"
#include "sd_card.h"
#include "sd_log.h"
#include "transport.h"
#include "ui.h"
#include "wifi_link.h"

static const char *TAG = "main";

static button_handle_t s_buttons[BSP_BUTTON_NUM];

/* 三条配网入口的第 2 条：双击 BOOT 强制重新配网。
 * 刻意**不动**现有的单击（提问）和长按（校准）——"双击"在用户直觉里就是
 * "我要设置点什么"，加一个新手势比改旧手势安全（PROPOSAL §1.3）。 */
static void on_prov_double_click(void *btn, void *arg)
{
    (void)btn;
    (void)arg;
    if (provisioning_is_active()) {
        ESP_LOGW(TAG, "已经在配网模式了");
        return;
    }
    led_feedback_play(LED_FB_ACK);
    ESP_LOGW(TAG, "双击 BOOT -> 进入配网模式");
    /* provisioning_start() 内部会切成 WIFI_MODE_AP，STA 连接自然断开 */
    if (provisioning_start() != ESP_OK) {
        ESP_LOGE(TAG, "配网模式启动失败（原因见上面的 prov 日志）");
    }
}

/* 看护：5 分钟没人动配网页就自动关热点回 STA（PROPOSAL §1.4）。
 * 单独起个小任务而不是塞进别处，是为了让"谁负责关 AP"只有一个答案。 */
static void prov_watchdog_task(void *arg)
{
    (void)arg;
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        provisioning_poll_timeout();
    }
}

static void on_ask_click(void *btn, void *arg)
{
    (void)btn;
    (void)arg;
    /* 先给本地反馈：按键被识别这件事不该等网络往返 */
    led_feedback_play(LED_FB_ACK);
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
    /* 两下短闪 = 长按生效，和单击的"一下"区分得开 */
    led_feedback_play(LED_FB_REPLY);
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
    iot_button_register_cb(ask_btn, BUTTON_DOUBLE_CLICK, NULL, on_prov_double_click, NULL);
    ESP_LOGI(TAG, "BOOT: click -> ask, long-press -> cycle tilt, double-click -> 重新配网");
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
    /* LED 反馈是锦上添花，没有灯也不该拦住主流程 */
    ESP_ERROR_CHECK_WITHOUT_ABORT(led_feedback_init());

    /* SD 卡同样是可选外设：有卡且是 FAT32 就挂上 /sdcard，没卡/格式不对也只是
     * 少个存储，绝不能拦住开机。挂上后顺手做一次读写自检（串口看 "自检 PASS"）。 */
    if (sd_card_init() == ESP_OK) {
        ESP_ERROR_CHECK_WITHOUT_ABORT(sd_card_selftest());
        /* 本地留档：网络断了也能在卡上查到轨迹。没卡/没空间都只是少个功能。 */
        ESP_ERROR_CHECK_WITHOUT_ABORT(sd_log_init());
    }

    ESP_ERROR_CHECK(ui_init());

    /* WiFi 栈先建起来（netif / 事件循环 / wifi init），但先不连 ——
     * 配网需要"栈在、连接待定"这个中间状态。 */
    ESP_ERROR_CHECK(wifi_link_init());

    /* transport_start() 必须排在 setup_ask_button() 之前：前者会创建状态互斥锁，
     * 而按键回调（transport_request_ask）要用那把锁。反过来的话，
     * 开机瞬间的按键会取到 NULL 锁。 */
    transport_start();
    setup_ask_button();

    /* 把表单解析的边界用例跑一遍，串口看 "N/N PASS"（本机没有 host C 编译器，
     * 所以自检放在目标板上跑，见 provisioning.h 的说明）。 */
    prov_form_selftest();

    /* 三条配网入口的第 1 条：NVS 里没有凭据就自动开热点。
     * 有凭据就直接连 —— 老 sdkconfig 一字不改仍然走这条路（向后兼容）。 */
    if (net_config_present()) {
        wifi_link_start();
    } else {
        ESP_LOGW(TAG, "NVS 里没有 WiFi 配置，进入配网模式（双击 BOOT 可再次进入）");
        /* 配网失败**不能**让板子变砖：provisioning_start() 内部已经把 abort
         * 换成"记原因 + 返回失败"，这里只负责把话说明白。 */
        if (provisioning_start() != ESP_OK) {
            ESP_LOGE(TAG, "配网模式启动失败（原因见上面的 prov 日志），双击 BOOT 可重试");
        }
    }
    xTaskCreatePinnedToCore(prov_watchdog_task, "prov_wd", 2560, NULL, 3, NULL, 0);

    net_config_t cfg;
    net_config_load(&cfg);
    ESP_LOGI(TAG, "rw1 board app: IMU %dHz -> batch -> %s%s",
             (int)(1000 / CONFIG_RW1_SAMPLE_PERIOD_MS),
             cfg.url, "/api/telemetry");
}
