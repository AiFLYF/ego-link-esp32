/*
 * SPDX-License-Identifier: MIT
 *
 * SoftAP 配网：开热点 → 手机连上填表 → **先试连再保存** → 转 STA。
 *
 * 触发入口（三条后路，缺一条就可能"配错了只能重烧"）：
 *   1. NVS 里没有凭据 → 开机自动进 AP（首次上电的自然路径）
 *   2. 双击 BOOT → 强制重新配网（main.c 注册）
 *   3. 配网页上的「清除配置并重启」
 *
 * 与 wifi_link 的分工（见 PROPOSAL §1.7 的坑）：
 *   - `esp_netif_init` / `esp_event_loop_create_default` / `esp_wifi_init` 由
 *     **wifi_link 负责**，本模块只注册自己的 handler，不重复初始化
 *     （重复调 `esp_event_loop_create_default()` 会返回 ESP_ERR_INVALID_STATE）
 *   - STA 的试连借 `wifi_link_try_sta()`
 *
 * 关于 APSTA：页面/扫描阶段是**纯 WIFI_MODE_AP**；只有"试连"那一小段切成
 * APSTA——否则手机连接会断，用户看不到"密码错误"的提示（验收标准 #4 要求）。
 */
#pragma once

#include <stdbool.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** AP 的固定地址（SoftAP 默认网段）。 */
#define PROV_AP_IP "192.168.4.1"

/** 无操作多久自动关 AP 回 STA（PROPOSAL §1.4：避免忘记关、长期占道 + 耗电）。 */
#define PROV_IDLE_TIMEOUT_MS (5 * 60 * 1000)

/**
 * 启动配网：开 AP + 起 HTTP 服务。**要求 wifi_link_init() 已经跑过。**
 * 返回 ESP_OK 表示 AP 与服务器都起来了。
 */
esp_err_t provisioning_start(void);

/** 关 AP、停 HTTP 服务。之后由调用方决定回 STA 还是重启。 */
void provisioning_stop(void);

/** 配网是否进行中（UI 据此切"配网中"屏幕）。 */
bool provisioning_is_active(void);

/**
 * AP 名（EGO-LINK-XXXX）与随机密码，屏幕要显示给用户。
 *
 * 密码是 **8 位数字**（不是 4 位）：WPA2 要求 8–63 位，4 位会被
 * `esp_wifi_set_config()` 拒绝 —— 2026-09-22 真机实测，那会让板子一开机就 abort
 * 重启，配网完全不可用。改 8 位后仍是纯数字，手机端好输入。
 */
const char *provisioning_ap_ssid(void);
const char *provisioning_ap_pass(void);

/** 最近一次配网结果，给 UI 提示用："已连接 192.168.1.37" / "密码错误" / ""。 */
const char *provisioning_last_result(void);

/**
 * 由看护任务周期调用：5 分钟无操作自动关 AP 回 STA。
 * 放在调用方而不是内部起任务，是为了让"谁拥有 AP 生命周期"这件事只有一个答案。
 */
void provisioning_poll_timeout(void);

/**
 * 表单解析的开机自检（把边界用例做成表跑一遍，串口打印 N/N PASS）。
 *
 * 为什么是设备端自检而不是 host 测试：本机没有 host C 编译器
 * （ESP 自带的 clang 是纯交叉工具链、无 wasm 目标、也没有 host libc）。
 * `prov_form.c` 本身零 IDF 依赖，有编译器时可直接：
 *     cc -I device/main device/main/prov_form.c 你的测试.c -o t && ./t
 */
void prov_form_selftest(void);

#ifdef __cplusplus
}
#endif
