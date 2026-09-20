/*
 * SPDX-License-Identifier: MIT
 *
 * WiFi STA + Aliyun SNTP (same pattern as biaopan main/time_sync.c, but the
 * link state is exposed as a simple up/down flag for the transport task).
 *
 * 第 4 周：凭据改从 net_config 取（NVS 优先 / 回退 Kconfig），并新增
 * 配网用的 try_sta() —— 见 wifi_link.h 的说明。
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

#include "net_config.h"

#define WIFI_CONNECTED_BIT BIT0

static const char *TAG = "wifi_link";

static EventGroupHandle_t s_wifi_events;
static int s_retry_count;
static volatile bool s_up;
static volatile bool s_sntp_started;
static volatile bool s_trying;          /* 配网试连中：抑制自动重连与状态改写 */
static bool s_inited;
static char s_ip[20] = "-";

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
    if (!s_trying) {
        esp_wifi_connect();
    }
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
        if (!s_trying) {
            esp_wifi_connect();
        }
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_up = false;
        if (!s_trying) {
            schedule_reconnect();
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_count = 0;
        s_up = true;
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
        if (!s_sntp_started) {   /* sync once; system clock free-runs after */
            s_sntp_started = true;
            start_sntp();
        }
    }
}

esp_err_t wifi_link_init(void)
{
    if (s_inited) {
        return ESP_OK;
    }
    setenv("TZ", "CST-8", 1);
    tzset();

    s_wifi_events = xEventGroupCreate();
    if (s_wifi_events == NULL) {
        return ESP_ERR_NO_MEM;
    }

    ESP_ERROR_CHECK(esp_netif_init());
    /* 事件循环由本模块负责创建：配网模块不再重复调用，
     * 否则会拿到 ESP_ERR_INVALID_STATE（PROPOSAL §1.7 记过这个坑）。 */
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        &wifi_event_handler, NULL, NULL));
    s_inited = true;
    return ESP_OK;
}

void wifi_link_start(void)
{
    if (!s_inited) {
        ESP_LOGE(TAG, "wifi_link_start() before wifi_link_init()");
        return;
    }

    net_config_t cfg;
    net_config_load(&cfg);

    wifi_config_t wifi_cfg = {0};
    strlcpy((char *)wifi_cfg.sta.ssid, cfg.ssid, sizeof(wifi_cfg.sta.ssid));
    strlcpy((char *)wifi_cfg.sta.password, cfg.pass, sizeof(wifi_cfg.sta.password));
    wifi_cfg.sta.threshold.authmode = WIFI_AUTH_OPEN;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* PROPOSAL §1.8 验收 #10：开机自检一行，看清配置到底从哪来 */
    ESP_LOGI(TAG, "net: ssid='%s' url='%s' device='%s' source=%s",
             cfg.ssid, cfg.url, cfg.device, net_config_source());
    ESP_LOGI(TAG, "Connecting to SSID '%s' (endless retries until it comes back)", cfg.ssid);
    /* Connection + reconnect are fully event-driven from here on. */
}

bool wifi_link_try_sta(const char *ssid, const char *pass, uint32_t timeout_ms,
                       char *ip_out, size_t ipcap)
{
    if (!s_inited || ssid == NULL) {
        return false;
    }
    if (ip_out != NULL && ipcap > 0) {
        ip_out[0] = '\0';
    }

    /* 切 APSTA：板子自己的热点不能掉，否则手机上的配网页收不到结果 */
    if (esp_wifi_set_mode(WIFI_MODE_APSTA) != ESP_OK) {
        ESP_LOGE(TAG, "cannot enter APSTA");
        return false;
    }

    wifi_config_t sta = {0};
    strlcpy((char *)sta.sta.ssid, ssid, sizeof(sta.sta.ssid));
    strlcpy((char *)sta.sta.password, pass, sizeof(sta.sta.password));
    sta.sta.threshold.authmode = WIFI_AUTH_OPEN;
    if (esp_wifi_set_config(WIFI_IF_STA, &sta) != ESP_OK) {
        return false;
    }

    s_trying = true;
    s_up = false;
    xEventGroupClearBits(s_wifi_events, WIFI_CONNECTED_BIT);
    esp_wifi_disconnect();          /* 先断开旧的，避免拿上一次的 GOT_IP 误判成功 */
    vTaskDelay(pdMS_TO_TICKS(120));
    esp_wifi_connect();

    const EventBits_t bits = xEventGroupWaitBits(s_wifi_events, WIFI_CONNECTED_BIT,
                                                 pdFALSE, pdFALSE,
                                                 pdMS_TO_TICKS(timeout_ms));
    const bool ok = (bits & WIFI_CONNECTED_BIT) != 0;
    s_trying = false;

    if (ok) {
        if (ip_out != NULL && ipcap > 0) {
            strlcpy(ip_out, s_ip, ipcap);
        }
        return true;
    }
    ESP_LOGW(TAG, "try_sta('%s') failed within %u ms", ssid, (unsigned)timeout_ms);
    return false;
}

void wifi_link_resume_sta(void)
{
    if (!s_inited) {
        return;
    }
    /* 关掉 AP：APSTA → STA。STA 侧配置已经是对的，掉线会由事件驱动重连接上。 */
    esp_err_t err = esp_wifi_set_mode(WIFI_MODE_STA);
    ESP_LOGI(TAG, "resume STA-only (%s), ip=%s", esp_err_to_name(err), s_ip);
}

bool wifi_link_is_up(void)
{
    return s_up;
}

const char *wifi_link_state_str(void)
{
    return s_up ? "WiFi在线" : "WiFi连接中";
}

const char *wifi_link_ip_str(void)
{
    return s_ip;
}
