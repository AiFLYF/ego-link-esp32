/*
 * SPDX-License-Identifier: MIT
 *
 * HTTP transport between the board and the PC server (server.py):
 *   POST /api/telemetry
 *        {"batch":[[x,y,z],...],"x":..,"y":..,"z":..,"source":"..","ask":bool,"q":".."
 *         [,"result":{"id":"..","ok":bool,"ms":..,"n":..,"x":..,"y":..,"z":..,"std":..}]}
 *        every CONFIG_RW1_TELEMETRY_PERIOD_MS, carrying every IMU sample taken
 *        since the previous POST (CONFIG_RW1_SAMPLE_PERIOD_MS apart).
 *   response
 *        {"ok":bool,"activity":"..","reply":"..","pending":bool
 *         [,"cmd":{"id":"..","name":"capture_once","params":{}}]}
 *
 * x/y are already mapped into screen axes by accel_input_map_to_screen(), so the
 * server and the LCD always agree on which way "up" is.
 *
 * A BOOT-button press sets the `ask` flag on the next telemetry frame, which
 * makes the server produce a fresh AI answer ("交互" round-trip). If that frame
 * fails to reach the server the flag is kept and retried, so a button press is
 * never silently swallowed.
 *
 * Remote commands (week 2): the board is a pure client with no inbound socket,
 * so the server piggybacks a command onto the response of telemetry frame N; the
 * board runs it and reports the result in the *request* of frame N+1, tagged
 * with the same request_id. One round trip therefore takes two telemetry
 * periods. The capture reuses the normal 10 ms sampling tick, so it never blocks
 * the loop.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TRANSPORT_ACTIVITY_LEN 96
#define TRANSPORT_REPLY_LEN    512
#define TRANSPORT_CMD_ID_LEN   32

/** What the board is doing with remote commands (for the UI). */
typedef enum {
    TRANSPORT_CMD_IDLE = 0,     /*!< no command seen yet */
    TRANSPORT_CMD_RUNNING,      /*!< a command is being executed right now */
    TRANSPORT_CMD_DONE,         /*!< the last command finished successfully */
    TRANSPORT_CMD_FAILED,       /*!< the last command failed (timeout/OOM/...) */
} transport_cmd_state_t;

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
    uint8_t  cmd_state;                                /*!< transport_cmd_state_t */
    uint16_t cmd_count;                                /*!< commands executed since boot */
    char  cmd_id[TRANSPORT_CMD_ID_LEN];                /*!< last command's request_id */
} transport_status_t;

/** Start the telemetry task (waits for WiFi internally). */
void transport_start(void);

/**
 * 重新从 net_config 读取上报地址。
 * 配网保存成功后调用，让新地址立即生效（不用重启）。
 */
void transport_reload_config(void);

/** Ask the server's AI a question on the next telemetry frame. */
void transport_request_ask(const char *question);

/** Snapshot of the link + last server answer (thread-safe). */
void transport_get_status(transport_status_t *out);

#ifdef __cplusplus
}
#endif
