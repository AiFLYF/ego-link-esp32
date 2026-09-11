/*
 * SPDX-License-Identifier: CC0-1.0
 *
 * Accelerometer input abstraction for the ESP32-S3-EYE watch face project.
 *
 * The ESP32-S3-EYE BSP reports BSP_CAPS_IMU == 0, so the IMU is not exposed
 * through the sensor hub. This module talks to the on-board accelerometer
 * directly over the BSP I2C bus and auto-detects the chip:
 *   - SC7A20  (WHO_AM_I 0x0F == 0x11, LIS3DH compatible)  <-- common on S3-EYE
 *   - LIS3DH  (WHO_AM_I 0x0F == 0x33)
 *   - MPU6050 (WHO_AM_I 0x75 == 0x68/0x70/0x71)
 *   - QMA7981 (reg00 == 0xE7)
 * If no accelerometer is found it falls back to the five on-board buttons,
 * so the fluid simulation always has a usable "gravity" input.
 */
#pragma once

#include <stdbool.h>
#include "esp_err.h"
#include "iot_button.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    float x_g;               /*!< X acceleration in g */
    float y_g;               /*!< Y acceleration in g */
    float z_g;               /*!< Z acceleration in g */
    bool valid;              /*!< true when the sample holds real data */
    const char *source_name; /*!< Human readable source ("SC7A20", "Buttons", ...) */
} accel_input_sample_t;

/**
 * @brief High-level motion for the fluid, derived from the raw sensor.
 *
 * The raw accelerometer is split with a complementary filter into a slowly
 * varying *gravity* component (steady tilt → which way is downhill) and a fast
 * *motion* residual (a flick or shake of the board). Both are already mapped
 * into screen coordinates (+x right, +y down) and smoothed, so the UI can use
 * them directly without knowing anything about the chip or its mounting.
 */
typedef struct {
    float grav_x;            /*!< smoothed in-plane gravity, screen frame, ~[-1,1] g */
    float grav_y;
    float motion_x;          /*!< fast motion residual, screen frame, g (slosh source) */
    float motion_y;
    float tilt;              /*!< |in-plane gravity|: 0 = flat, ~1 = on edge */
    bool  valid;
    const char *source_name;
} accel_motion_t;

/**
 * @brief Detect and initialise the accelerometer (or button fallback).
 *
 * @return ESP_OK on success (sensor or fallback ready), otherwise an error.
 */
esp_err_t accel_input_init(void);

/**
 * @brief Read one acceleration sample.
 *
 * @param[out] sample Filled with the latest reading. Never NULL.
 * @return true when @p sample contains a valid reading.
 */
bool accel_input_poll(accel_input_sample_t *sample);

/**
 * @brief Read one frame of high-level motion (gravity + slosh), screen-mapped
 *        and smoothed. Call once per render frame; it advances the filter.
 *
 * @param[out] out Filled with the latest motion. Never NULL.
 */
void accel_input_read_motion(accel_motion_t *out);

/**
 * @brief Sensor→screen axis orientation, 0..7 (swap-xy / flip-x / flip-y).
 *
 * Lets the pour direction be corrected on-device without a rebuild. The value
 * persists in NVS. ::accel_input_cycle_orientation steps to the next one.
 */
int  accel_input_get_orientation(void);
void accel_input_set_orientation(int idx);
void accel_input_cycle_orientation(void);

/**
 * @brief Whether the driver fell back to using the on-board buttons as the
 *        "gravity" source (i.e. no real IMU was detected). When true the UI
 *        must NOT create its own button handles, but may register extra
 *        callbacks on the handles returned by accel_input_button().
 */
bool accel_input_uses_buttons(void);

/**
 * @brief Get a button handle when in button-fallback mode.
 * @param idx One of the BSP_BUTTON_* indices.
 * @return handle, or NULL if not in button mode / out of range.
 */
button_handle_t accel_input_button(int idx);

#ifdef __cplusplus
}
#endif
