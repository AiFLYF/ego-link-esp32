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
 * @param[in]  x_g,y_g,z_g  Raw sensor values.
 * @param[out] out_x,out_y,out_z Screen-frame values. May not be NULL.
 */
void accel_input_map_to_screen(float x_g, float y_g, float z_g,
                               float *out_x, float *out_y, float *out_z);

/**
 * @brief Sensor→screen axis orientation, 0..15 —— **完整的三轴置换，含镜像族**。
 *
 * 2026-09-23 真机实测订正两次：
 *  ① 原来是"xy 平面内互换/翻转"，修不了"传感器竖着装"（法线落在传感器 y 轴上）
 *     的板子 —— 实测这块板子就是，8 个旧档位里没有一个能用。
 *  ② 补上三轴置换后，用户实测"上下对、左右反" —— 只翻一个轴在右手系里做不到
 *     （那是镜像），说明这颗芯片相对板面是镜像的。所以档位表要同时覆盖两族。
 *
 * 现在 16 档 = {法线 = ±传感器 y} × {平面内 4 种 90° 旋转} × {右手/镜像}：
 *   o0..o7  法线 = −传感器 y（本机实测族，**o0 就是实测正确的那个**）
 *   o8..o15 法线 = +传感器 y
 *
 * Lets the tilt direction be corrected on-device without a rebuild. The value
 * persists in NVS（键名 `orient3`；语义变过两次所以换了两次键名，旧值自动失效）。
 * ::accel_input_cycle_orientation steps to the next one and is
 * bound to a long-press of the BOOT key in main.c.
 *
 * 判据：**平放屏幕朝上、把右边压低 → 屏幕和仪表盘都应写「向右倾斜」**。
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
