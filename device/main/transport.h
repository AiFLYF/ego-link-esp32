/*
 * SPDX-License-Identifier: CC0-1.0
 *
 * HTTP transport between the board and the PC server (server.py):
 *   POST /api/telemetry  {x,y,z,source,ask,q}   every CONFIG_RW1_TELEMETRY_PERIOD_MS
 *   response             {ok,activity,reply}    shown on the LVGL screen
 *
 * A BOOT-button press sets the `ask` flag on the next telemetry frame, which
 * makes the server produce a fresh AI answer ("交互" round-trip).
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TRANSPORT_ACTIVITY_LEN 96
#define TRANSPORT_REPLY_LEN    384

typedef struct {
    bool  server_ok;                                   /*!< last POST succeeded */
    int   fail_streak;                                 /*!< consecutive failures */
    char  activity[TRANSPORT_ACTIVITY_LEN];            /*!< server's current label */
    char  reply[TRANSPORT_REPLY_LEN];                  /*!< latest AI reply text */
    char  source[16];                                  /*!< IMU chip name */
    float x_g, y_g, z_g;                               /*!< last sent sample */
    uint32_t posts_ok, posts_fail;
} transport_status_t;

/** Start the telemetry task (waits for WiFi internally). */
void transport_start(void);

/** Ask the server's AI a question on the next telemetry frame. */
void transport_request_ask(const char *question);

/** Snapshot of the link + last server answer (thread-safe). */
void transport_get_status(transport_status_t *out);

#ifdef __cplusplus
}
#endif
