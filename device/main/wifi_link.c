/*
 * SPDX-License-Identifier: CC0-1.0
 *
 * WiFi STA + Aliyun SNTP (same pattern as biaopan main/time_sync.c, but the
 * link state is exposed as a simple up/down flag for the transport task).
 */
#include "wifi_link.h"

#include <string.h>
#include <time.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "freertos/timers.h"

#define WIFI_CONNECTED_BIT BIT0

static const char *TAG = "wifi_link";

static EventGroupHandle_t s_wifi_events;
static int s_retry_count;
static volatile bool s_up;
static volatile bool s_sntp_started;

static void start_sntp(void);

static void on_ntp_sync(struct timeval *tv)
{
    (void)tv;
    ESP_LOGI(TAG, "NTP time synchronised");
}

static void start_sntp(void)
{
    esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG_MULTIPLE(
        3, ESP_SNTP_SERVER_LIST("ntp.aliyun.com", "ntp1.aliyun.com", "ntp2.aliyun.com"));
    cfg.sync_cb = on_ntp_sync;
    cfg.start = true;
    ESP_ERROR_CHECK(esp_netif_sntp_init(&cfg));
}

/*
 * Reconnect policy: never give up. Retries come back fast for the first
 * CONFIG_RW1_WIFI_MAX_RETRY attempts (handles a transient AP blip), then
 * settle to one attempt per 15 s so a dead/renamed AP doesn't spam the log.
 * If the network comes back later the board rejoins without a reflash.
 */
static void reconnect_timer_cb(TimerHandle_t timer)
{
    xTimerDelete(timer, 0);
    esp_wifi_connect();
}

static void schedule_reconnect(void)
{
    s_retry_count++;
    const TickType_t delay = (s_retry_count <= CONFIG_RW1_WIFI_MAX_RETRY)
        ? pdMS_TO_TICKS(2000) : pdMS_TO_TICKS(15000);
    if ((s_retry_count % 10) == 1) {
        ESP_LOGW(TAG, "WiFi disconnected, retry %d (backoff %u s)",
                 s_retry_count, (unsigned)(pdTICKS_TO_MS(delay) / 1000));
    }
    TimerHandle_t t = xTimerCreate("wifi_rc", delay, pdFALSE, NULL, reconnect_timer_cb);
    if (t != NULL) {
        xTimerStart(t, 0);
    }
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_up = false;
        schedule_reconnect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_count = 0;
        s_up = true;
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
        if (!s_sntp_started) {   /* sync once; system clock free-runs after */
            s_sntp_started = true;
            start_sntp();
        }
    }
}

static void wifi_link_task(void *arg)
{
    setenv("TZ", "CST-8", 1);
    tzset();

    s_wifi_events = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        &wifi_event_handler, NULL, NULL));

    wifi_config_t wifi_cfg = {
        .sta = {
            .ssid = CONFIG_RW1_WIFI_SSID,
            .password = CONFIG_RW1_WIFI_PASSWORD,
            .threshold.authmode = WIFI_AUTH_OPEN,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));

    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "Connecting to SSID '%s' (endless retries until it comes back)",
             CONFIG_RW1_WIFI_SSID);
    /* Connection + reconnect are fully event-driven from here on. */

    vTaskDelete(NULL);
}

void wifi_link_start(void)
{
    xTaskCreatePinnedToCore(wifi_link_task, "wifi_link", 4096, NULL, 4, NULL, 0);
}

bool wifi_link_is_up(void)
{
    return s_up;
}

const char *wifi_link_state_str(void)
{
    return s_up ? "WiFi在线" : "WiFi连接中";
}
