/*
 * SPDX-License-Identifier: CC0-1.0
 *
 * WiFi STA link for the RW1 board app (adapted from the watch's time_sync.c).
 * Connects the board to the LAN so it can reach the PC server, and keeps
 * Aliyun SNTP running so log timestamps are meaningful.
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

/** Human readable link state for the UI ("WiFi.." / "在线" / "无WiFi"). */
const char *wifi_link_state_str(void);

#ifdef __cplusplus
}
#endif
