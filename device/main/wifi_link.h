/*
 * SPDX-License-Identifier: MIT
 *
 * WiFi STA link for the board app. Connects the board to the LAN so it can
 * reach the PC server, and keeps Aliyun SNTP running so log timestamps are
 * meaningful.
 *
 * 第 4 周起凭据不再直接来自 Kconfig，而是走 `net_config`（NVS 优先，
 * NVS 为空则回退 Kconfig）——这样配网改完立即生效，不用重编译。
 *
 * 初始化被拆成两步，因为配网需要"先把 WiFi 栈建起来、但先不连"：
 *   wifi_link_init()  → netif / event loop / wifi init + 注册 handler
 *   wifi_link_start() → 用生效配置连 STA
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * 建 WiFi 栈（幂等）。**必须在 provisioning_start() 之前调用** ——
 * `esp_event_loop_create_default()` 只能建一次，由本模块负责，
 * 配网模块只注册自己的 handler（见 PROPOSAL §1.7）。
 */
esp_err_t wifi_link_init(void);

/** 用 net_config 里的凭据连接 STA（非阻塞，事件驱动重连）。 */
void wifi_link_start(void);

/** true once an IP address has been obtained (and stays true across retries). */
bool wifi_link_is_up(void);

/** Human readable link state for the UI: "WiFi在线" / "WiFi连接中". */
const char *wifi_link_state_str(void);

/** 当前 IP 字符串；未连接时返回 "-"。 */
const char *wifi_link_ip_str(void);

/**
 * 用给定凭据**试连一次**（配网"先试连再保存"用）。
 *
 * 会把模式切成 APSTA —— 这样板子自己开的热点不掉，手机上的配网页才能
 * 收到结果。如果切成纯 STA，手机连接立刻断，用户永远看不到"密码错误"。
 *
 * @param timeout_ms 等 GOT_IP 的上限（配网页用 15000）
 * @param ip_out     成功后写入拿到的 IP
 * @return true = 连上并拿到 IP
 */
bool wifi_link_try_sta(const char *ssid, const char *pass, uint32_t timeout_ms,
                       char *ip_out, size_t ipcap);

/** 配网结束后回 STA-only（关掉 AP），沿用刚保存的配置。 */
void wifi_link_resume_sta(void);

#ifdef __cplusplus
}
#endif
