/*
 * SPDX-License-Identifier: MIT
 * See net_config.h.
 */
#include "net_config.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_mac.h"
#include "nvs.h"
#include "sdkconfig.h"

static const char *TAG = "net_config";

#define NVS_NS "netcfg"

/* 开机自检用：本次生效的配置来自哪里（现在只有 "NVS" / "default" 两种） */
static const char *s_source = "default";

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

    /* **凭据只从配网来，不再回退 Kconfig**（用户 2026-09-23 的决定）。
     *
     * 原来这里会把 CONFIG_RW1_WIFI_SSID / PASSWORD / SERVER_URL 铺成"出厂默认值"，
     * 于是 PROPOSAL §1.8 验收 #9 和 README 都写着"老 sdkconfig 一字不改仍然能跑"。
     * 但实机上那句话**不成立**：`net_config_present()` 只看 NVS，NVS 空就进配网，
     * 根本走不到这条回退路径 —— 文档承诺了一个到不了的分支。
     *
     * 现在的选择是"配网是唯一入口"，顺带解决一个更硬的隐患：
     * **编译期塞进去的 WiFi 密码会随固件一起分发出去**（发固件 = 发密码）。
     * 只要凭据不再从 sdkconfig 来，固件里就永远不可能包含 WiFi 密码。
     *
     * 唯一保留的 Kconfig 值是上报周期：它不是凭据，而且配网页"留空用默认"要用它。
     */
    out->device[0] = '\0';   /* 留空 = 由 net_config_device_id() 按 MAC 生成 rw1-XXXX */
    out->period_ms = CONFIG_RW1_TELEMETRY_PERIOD_MS;
    s_source = "default";

    /* NVS 是唯一来源 */
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

void net_config_device_id(const net_config_t *cfg, char *out, size_t cap)
{
    if (out == NULL || cap == 0) {
        return;
    }
    if (cfg != NULL && cfg->device[0] != '\0') {
        strlcpy(out, cfg->device, cap);
        return;
    }

    /* 没起名字就按 MAC 生成。用 SOFTAP 的 MAC 是为了和 provisioning.c 里的
     * 热点名 EGO-LINK-%02X%02X 取**同样两个字节** —— 学生看到热点名就能在
     * 仪表盘上找到对应的那台板。 */
    uint8_t mac[6] = {0};
    if (esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP) != ESP_OK) {
        strlcpy(out, "rw1", cap);       /* 读不到 MAC 也别返回空：空会退化成"默认设备" */
        return;
    }
    snprintf(out, cap, "rw1-%02X%02X", mac[4], mac[5]);
}
