/*
 * SPDX-License-Identifier: MIT
 *
 * HTTP transport between the board and the PC server (server.py):
 *   POST /api/telemetry
 *        {"batch":[[x,y,z],...],"x":..,"y":..,"z":..,"source":"..","ask":bool,"q":".."}
 *        every CONFIG_RW1_TELEMETRY_PERIOD_MS, carrying every IMU sample taken
 *        since the previous POST (CONFIG_RW1_SAMPLE_PERIOD_MS apart).
 *   response
 *        {"ok":bool,"activity":"..","reply":"..","pending":bool}
 *
 * x/y are already mapped into screen axes by accel_input_map_to_screen(), so the
 * server and the LCD always agree on which way "up" is.
 *
 * A BOOT-button press sets the `ask` flag on the next telemetry frame, which
 * makes the server produce a fresh AI answer ("交互" round-trip). If that frame
 * fails to reach the server the flag is kept and retried, so a button press is
 * never silently swallowed.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TRANSPORT_ACTIVITY_LEN 96
#define TRANSPORT_REPLY_LEN    512

typedef struct {
    bool  server_ok;                                   /*!< last POST succeeded */
    int   fail_streak;                                 /*!< consecutive failures */
    char  activity[TRANSPORT_ACTIVITY_LEN];            /*!< server's current label */
    char  reply[TRANSPORT_REPLY_LEN];                  /*!< latest AI reply text */
    char  source[16];                                  /*!< IMU chip name */
    float x_g, y_g, z_g;                               /*!< last sent sample (screen frame) */
    uint32_t posts_ok, posts_fail;
    uint16_t batch_last;                               /*!< samples in the last batch */
    uint8_t  orient;                                   /*!< active tilt calibration 0..7 */
    bool  ai_pending;                                  /*!< server is still generating the reply */
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
