/*
 * SPDX-License-Identifier: MIT
 * See net_config.h.
 */
#include "net_config.h"

#include <string.h>

#include "esp_log.h"
#include "nvs.h"
#include "sdkconfig.h"

static const char *TAG = "net_config";

#define NVS_NS "netcfg"

/* 开机自检用：本次生效的配置来自哪里 */
static const char *s_source = "default";

static void copy_str(char *dst, size_t cap, const char *src)
{
    if (cap == 0) {
        return;
    }
    if (src == NULL) {
        dst[0] = '\0';
        return;
    }
    strlcpy(dst, src, cap);
}

/* 逐项读 NVS：任何一项读不到就保留调用方已经填好的默认值。
 * 这样"只配了一半"的 NVS 不会把其余字段清空。 */
static bool load_from_nvs(net_config_t *out)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        return false;
    }

    bool got_ssid = false;
    size_t len = sizeof(out->ssid);
    if (nvs_get_str(h, "ssid", out->ssid, &len) == ESP_OK && out->ssid[0] != '\0') {
        got_ssid = true;
    }

    len = sizeof(out->pass);
    if (nvs_get_str(h, "pass", out->pass, &len) != ESP_OK) {
        /* 读不到就保持 Kconfig 默认（可能是空密码的开放网络） */
    }
    len = sizeof(out->url);
    if (nvs_get_str(h, "url", out->url, &len) != ESP_OK) {
        /* ditto */
    }
    len = sizeof(out->device);
    if (nvs_get_str(h, "device", out->device, &len) != ESP_OK) {
        /* ditto */
    }
    uint16_t period = 0;
    if (nvs_get_u16(h, "period", &period) == ESP_OK && period > 0) {
        out->period_ms = period;
    }

    nvs_close(h);
    return got_ssid;      /* 没有 SSID 就当作"没配过" */
}

void net_config_load(net_config_t *out)
{
    memset(out, 0, sizeof(*out));

    /* 1) 先铺 Kconfig 出厂默认值 —— 这就是"向后兼容老固件行为"的那一层 */
    copy_str(out->ssid, sizeof(out->ssid), CONFIG_RW1_WIFI_SSID);
    copy_str(out->pass, sizeof(out->pass), CONFIG_RW1_WIFI_PASSWORD);
    copy_str(out->url, sizeof(out->url), CONFIG_RW1_SERVER_URL);
    copy_str(out->device, sizeof(out->device), "rw1");
    out->period_ms = CONFIG_RW1_TELEMETRY_PERIOD_MS;
    s_source = (out->ssid[0] != '\0') ? "Kconfig" : "default";

    /* 2) NVS 覆盖 */
    if (load_from_nvs(out)) {
        s_source = "NVS";
    }
}

bool net_config_save(const net_config_t *in)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nvs_open failed: %s", esp_err_to_name(err));
        return false;
    }

    /* 逐项写，任一失败就整体失败——不做"写一半"的配置 */
    err = nvs_set_str(h, "ssid", in->ssid);
    if (err == ESP_OK) {
        err = nvs_set_str(h, "pass", in->pass);
    }
    if (err == ESP_OK) {
        err = nvs_set_str(h, "url", in->url);
    }
    if (err == ESP_OK) {
        err = nvs_set_str(h, "device", in->device);
    }
    if (err == ESP_OK) {
        err = nvs_set_u16(h, "period", in->period_ms);
    }
    if (err == ESP_OK) {
        err = nvs_commit(h);          /* 不 commit 的话断电就没了 */
    }
    nvs_close(h);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "save failed: %s", esp_err_to_name(err));
        return false;
    }
    s_source = "NVS";
    ESP_LOGI(TAG, "saved: ssid='%s' url='%s' device='%s' period=%u",
             in->ssid, in->url, in->device, (unsigned)in->period_ms);
    return true;
}

void net_config_clear(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    esp_err_t err = nvs_erase_all(h);
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    s_source = "default";
    ESP_LOGW(TAG, "config cleared (%s) - next boot will enter provisioning",
             esp_err_to_name(err));
}

bool net_config_present(void)
{
    net_config_t cfg;
    net_config_load(&cfg);
    /* 只看 NVS 这一层：Kconfig 有值不代表"配过网"，那只是出厂默认 */
    return strcmp(s_source, "NVS") == 0 && cfg.ssid[0] != '\0' && cfg.url[0] != '\0';
}

const char *net_config_source(void)
{
    return s_source;
}
