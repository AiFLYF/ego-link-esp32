/*
 * SPDX-License-Identifier: MIT
 *
 * Accelerometer input abstraction for the Ego Link board app (ESP32-S3-EYE).
 *
 * The ESP32-S3-EYE BSP reports BSP_CAPS_IMU == 0, so the IMU is not exposed
 * through the sensor hub. This module talks to the on-board accelerometer
 * directly over the BSP I2C bus and auto-detects the chip:
 *   - SC7A20  (WHO_AM_I 0x0F == 0x11, LIS3DH compatible)  <-- common on S3-EYE
 *   - LIS3DH  (WHO_AM_I 0x0F == 0x33)
 *   - MPU6050 (WHO_AM_I 0x75 == 0x68/0x70/0x71)
 *   - QMA7981 (reg00 == 0xE7)
 * If no accelerometer is found it falls back to the five on-board buttons, so
 * the app always has a usable "tilt" input.
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
 * @brief Detect and initialise the accelerometer (or button fallback).
 *
 * @return ESP_OK on success (sensor or fallback ready), otherwise an error.
 */
esp_err_t accel_input_init(void);

/**
 * @brief Read one acceleration sample.
 *
 * The returned x/y are the *raw sensor* axes. Call ::accel_input_map_to_screen
 * before sending them anywhere a human will read them.
 *
 * @param[out] sample Filled with the latest reading. Never NULL.
 * @return true when @p sample contains a valid reading.
 */
bool accel_input_poll(accel_input_sample_t *sample);

/**
 * @brief Map raw sensor in-plane axes onto screen axes (+x right, +y down).
 *
 * The board calls this before every upload, so the PC server always receives
 * screen-frame numbers and its "up/down/left/right" labels agree with what is
 * on the LCD. Before this existed the server interpreted raw sensor axes while
 * the UI used the flipped ones, so the two ends reported opposite tilt
 * directions for the same physical pose.
 *
 * The mapping is the NVS-persisted orientation (see below), so it survives
 * reboots and can be corrected in the field without a rebuild.
 *
 * @param[in]  x_g,y_g  Raw sensor values.
 * @param[out] out_x,out_y Screen-frame values. May not be NULL.
 */
void accel_input_map_to_screen(float x_g, float y_g, float *out_x, float *out_y);

/**
 * @brief Sensor→screen axis orientation, 0..7 (swap-xy / flip-x / flip-y).
 *
 * Lets the tilt direction be corrected on-device without a rebuild. The value
 * persists in NVS. ::accel_input_cycle_orientation steps to the next one and is
 * bound to a long-press of the BOOT key in main.c.
 */
int  accel_input_get_orientation(void);
void accel_input_set_orientation(int idx);
void accel_input_cycle_orientation(void);

/**
 * @brief Whether the driver fell back to using the on-board buttons as the
 *        "tilt" source (i.e. no real IMU was detected). When true the UI
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
