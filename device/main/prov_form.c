/*
 * SPDX-License-Identifier: MIT
 * See prov_form.h.
 *
 * 本文件**不得**包含任何 ESP-IDF 头文件 —— 它要能在 PC 上单独编译来做回归测试。
 */
#include "prov_form.h"

#include <stdlib.h>
#include <string.h>

#define PERIOD_MIN_MS 100
#define PERIOD_MAX_MS 10000

static int hexval(char c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

void prov_url_decode(const char *src, size_t srclen, char *dst, size_t dstcap)
{
    size_t w = 0;
    if (dstcap == 0) {
        return;
    }
    for (size_t i = 0; i < srclen && w + 1 < dstcap; i++) {
        const char c = src[i];
        if (c == '+') {
            dst[w++] = ' ';
        } else if (c == '%' && i + 2 < srclen) {
            const int hi = hexval(src[i + 1]);
            const int lo = hexval(src[i + 2]);
            if (hi >= 0 && lo >= 0) {
                dst[w++] = (char)((hi << 4) | lo);
                i += 2;
            } else {
                dst[w++] = c;        /* 非法转义就原样保留，别丢字符 */
            }
        } else {
            dst[w++] = c;
        }
    }
    dst[w] = '\0';
}

/* 在 body 里找 key 对应的值，解码后写进 dst。
 * 返回 false 表示字段不存在（调用方保留 base 的值）。 */
static bool field(const char *body, size_t len, const char *key,
                  char *dst, size_t dstcap)
{
    const size_t klen = strlen(key);
    size_t i = 0;

    while (i < len) {
        /* 跳过前导分隔符 */
        while (i < len && (body[i] == '&' || body[i] == ';')) {
            i++;
        }
        const size_t start = i;
        while (i < len && body[i] != '&' && body[i] != ';') {
            i++;
        }
        const size_t seglen = i - start;

        /* 这个片段是 "key=value" 吗？ */
        if (seglen > klen && body[start + klen] == '=' &&
            memcmp(body + start, key, klen) == 0) {
            prov_url_decode(body + start + klen + 1, seglen - klen - 1, dst, dstcap);
            return true;
        }
    }
    return false;
}

bool prov_validate(const net_config_t *cfg, char *err, size_t errcap)
{
    if (cfg->ssid[0] == '\0') {
        strlcpy(err, "请选择或填写 WiFi 名称", errcap);
        return false;
    }
    if (strlen(cfg->ssid) > 32) {
        strlcpy(err, "WiFi 名称过长（最多 32 字节）", errcap);
        return false;
    }
    /* 8–63 是 WPA2 的要求；0 表示开放网络，允许 */
    const size_t plen = strlen(cfg->pass);
    if (plen != 0 && (plen < 8 || plen > 63)) {
        strlcpy(err, "WiFi 密码长度应为 8–63 位（开放网络留空）", errcap);
        return false;
    }
    if (cfg->url[0] == '\0') {
        strlcpy(err, "请填写服务器地址", errcap);
        return false;
    }
    if (strncmp(cfg->url, "http://", 7) != 0 && strncmp(cfg->url, "https://", 8) != 0) {
        strlcpy(err, "服务器地址要以 http:// 或 https:// 开头", errcap);
        return false;
    }
    if (cfg->period_ms != 0 &&
        (cfg->period_ms < PERIOD_MIN_MS || cfg->period_ms > PERIOD_MAX_MS)) {
        strlcpy(err, "上报周期应在 100–10000 ms 之间", errcap);
        return false;
    }
    return true;
}

bool prov_parse_form(const char *body, size_t len, const net_config_t *base,
                     net_config_t *out, char *err, size_t errcap)
{
    if (out == NULL) {
        return false;
    }
    if (err != NULL && errcap > 0) {
        err[0] = '\0';
    }
    /* 从基线出发：缺省字段沿用旧值，而不是清空 */
    *out = *base;

    if (body != NULL && len > 0) {
        field(body, len, "ssid", out->ssid, sizeof(out->ssid));
        field(body, len, "pass", out->pass, sizeof(out->pass));
        field(body, len, "url", out->url, sizeof(out->url));
        field(body, len, "device", out->device, sizeof(out->device));

        char tmp[16];
        if (field(body, len, "period", tmp, sizeof(tmp))) {
            /* 非数字按 0 处理（= 用默认值），不因为一个输入框没填就整单失败 */
            const long v = strtol(tmp, NULL, 10);
            out->period_ms = (v > 0 && v <= 65535) ? (uint16_t)v : 0;
        }
    }

    /* 设备名**刻意允许留空**：留空 = 用 MAC 自动命名（net_config_device_id）。
     * 以前这里兜底填 "rw1"，于是所有没改过名的板子在服务端是同一台设备、
     * 姿态球互相覆盖 —— 多板场景下这个"贴心默认值"反而是坑。 */
    return prov_validate(out, err, errcap);
}
