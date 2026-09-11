/*
 * SPDX-License-Identifier: CC0-1.0
 *
 * Small LVGL status screen for the RW1 board app: link state, current activity
 * classified by the PC server, live G values, and the latest AI reply.
 */
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Build the screen; call after bsp_display_start(). */
esp_err_t ui_init(void);

#ifdef __cplusplus
}
#endif
