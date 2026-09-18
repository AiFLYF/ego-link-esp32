/*
 * SPDX-License-Identifier: MIT
 *
 * WiFi STA link for the board app. Connects the board to the LAN so it can
 * reach the PC server, and keeps Aliyun SNTP running so log timestamps are
 * meaningful.
 */
#pragma once

#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Start the connect task (returns immediately). */
void wifi_link_start(void);

/** true once an IP address has been obtained (and stays true across retries). */
bool wifi_link_is_up(void);

/** Human readable link state for the UI: "WiFi在线" / "WiFi连接中". */
const char *wifi_link_state_str(void);

#ifdef __cplusplus
}
#endif
