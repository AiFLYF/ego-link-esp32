/*
 * SPDX-License-Identifier: MIT
 *
 * 运行期可变配置的唯一入口（第 4 周配网功能）。
 *
 * 背景：`RW1_WIFI_SSID` / `RW1_SERVER_URL` 这些原本只能靠 menuconfig 改，
 * 改一次要全量重编译 + 烧录，PC 换个 DHCP 地址就得再来一遍；更要命的是
 * **编译出来的固件里 WiFi 密码是明文**，把固件发给同学等于把密码一起发出去。
 *
 * 这个模块把"配置"从编译期挪到运行期，但**刻意做得很薄**：
 *   - NVS 里有值就用 NVS 的
 *   - NVS 里没有就回退 `CONFIG_RW1_*`（出厂默认值）
 * 于是老 `sdkconfig` 一字不改仍然能跑，Kconfig 那两项从"唯一来源"平滑降级为
 * "出厂默认值"，迁移是渐进的。
 *
 * 注意：本模块只负责"存/取"，不碰 WiFi。真正连网在 wifi_link.c。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** 字段长度按 802.11 / URL 上限取，够用且能整块塞进 NVS。 */
#define NET_SSID_MAX   33      /* 32 + NUL */
#define NET_PASS_MAX   65      /* 64 + NUL */
#define NET_URL_MAX   128
#define NET_DEV_MAX    24

typedef struct {
    char     ssid[NET_SSID_MAX];
    char     pass[NET_PASS_MAX];
    char     url[NET_URL_MAX];   /* 空字符串 = 未配置，配网时必填 */
    char     device[NET_DEV_MAX];/* 设备名，多板场景下仪表盘区分用 */
    uint16_t period_ms;          /* 上报周期；0 = 用 Kconfig 默认 */
} net_config_t;

/**
 * 读取生效配置。NVS 缺项逐项回退 `CONFIG_RW1_*`。
 * 结果可通过 net_config_source() 查询来源（开机自检会打印一行）。
 */
void net_config_load(net_config_t *out);

/** 整块写入 NVS。返回 false 表示写入或提交失败（调用方应提示用户）。 */
bool net_config_save(const net_config_t *in);

/** 删除整个命名空间 → 下次开机 NVS 为空，自动进配网。 */
void net_config_clear(void);

/** NVS 里是否已有完整配置（决定开机是否要进 AP）。 */
bool net_config_present(void);

/** "NVS" / "Kconfig" / "default" —— 供开机自检打印。 */
const char *net_config_source(void);

#ifdef __cplusplus
}
#endif
