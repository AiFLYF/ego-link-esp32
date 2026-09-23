/*
 * SPDX-License-Identifier: MIT
 *
 * 配网表单的解析与校验 —— **刻意做成纯函数，不依赖任何 ESP-IDF**。
 *
 * 为什么单独一个文件：SoftAP + HTTP 服务是纯固件行为，`fake_board.py` 那套
 * host 侧回归测试够不着（PROPOSAL §1.7 明确点了这条）。但"表单字符串 →
 * net_config_t"这段逻辑恰恰是最容易写错、也最值得测的部分（URL 解码、超长截断、
 * 非法 URL、越界数值……）。把它抽成不依赖 IDF 的纯 C，就能在 PC 上跑断言。
 *
 * 对应的 host 侧测试：tools/verify_netcfg.py
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "net_config.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 校验失败的说明会写进这里，调用方直接回显给用户（中文，面向使用者）。 */
#define PROV_ERR_MAX 96

/**
 * 解析 `application/x-www-form-urlencoded` 请求体并校验。
 *
 * 字段：ssid / pass / url / device / period（period 单位 ms）。
 * 缺省字段沿用 `base` 里的值（配网页只提交用户改过的项时也能工作）。
 *
 * @param body     请求体（不要求以 NUL 结尾）
 * @param len      请求体长度
 * @param base     基线配置，通常是 net_config_load() 的结果
 * @param out      解析结果
 * @param err      失败原因（可为 NULL）
 * @param errcap   err 的容量
 * @return true 表示解析且校验通过；false 时 err 里有面向用户的原因
 */
bool prov_parse_form(const char *body, size_t len, const net_config_t *base,
                     net_config_t *out, char *err, size_t errcap);

/**
 * 单独校验一份配置（供"测试连接"等场景复用）。
 * @return true 表示合法；false 时 err 里有原因
 */
bool prov_validate(const net_config_t *cfg, char *err, size_t errcap);

/**
 * URL 解码（`+` → 空格，`%XX` → 字节）。纯函数，越界即截断。
 * 单独导出是为了能在 host 侧直接测边界。
 */
void prov_url_decode(const char *src, size_t srclen, char *dst, size_t dstcap);

#ifdef __cplusplus
}
#endif
