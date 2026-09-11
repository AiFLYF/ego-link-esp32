/*
 * SPDX-License-Identifier: CC0-1.0
 *
 * Accelerometer auto-detection driver for the ESP32-S3-EYE.
 * See accel_input.h for the supported parts and detection strategy.
 */
#include "accel_input.h"

#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "bsp/esp-bsp.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "iot_button.h"
#include "nvs.h"

#define ACCEL_I2C_TIMEOUT_MS 30
#define ACCEL_I2C_FREQ_HZ 400000
#define ACCEL_ALT_I2C_PORT I2C_NUM_1

/* Complementary filter / conditioning of the raw sensor.
 *  - GRAV_ALPHA: low-pass weight for the gravity estimate at ~30 fps. Smaller =
 *    smoother + more lag. The on-board SC7A20 measures very cleanly (±0.01 g of
 *    noise at rest), so 0.15 (~190 ms settle) stays smooth while tracking the
 *    hand promptly.
 *  - DEAD_G:     in-plane gravity below this (sensor bias/noise on a flat board)
 *    is squelched to zero so a level pool sits perfectly still.
 *  - DEAD_MOTION: motion residual below this is ignored, so only a deliberate
 *    flick sloshes the water — gentle tilts don't. */
#define ACCEL_GRAV_ALPHA  0.15f
#define ACCEL_DEAD_G      0.045f
#define ACCEL_DEAD_MOTION 0.060f

#define ACCEL_NVS_NS  "accel"
#define ACCEL_NVS_KEY "orient"
/* Default sensor→screen map: no axis swap, Y inverted (matches the QMA7981 tilt
 * calibration recorded for this board). If left/right or up/down still run the
 * wrong way it is corrected live from the info screen, no rebuild needed. */
#define ACCEL_ORIENT_DEFAULT 4

typedef enum {
    ACCEL_SOURCE_NONE = 0,
    ACCEL_SOURCE_DEMO,
    ACCEL_SOURCE_MPU6050,
    ACCEL_SOURCE_LIS3DH,
    ACCEL_SOURCE_QMA7981,
    ACCEL_SOURCE_BUTTONS,
} accel_source_t;

typedef struct {
    uint8_t reg_base;
    uint8_t shift;
    float lsb_per_g;
    bool ready;
} qma_format_t;

static const char *TAG = "accel_input";

static i2c_master_bus_handle_t s_i2c_bus = NULL;
static bool s_i2c_bus_owned = false;
static const char *s_i2c_bus_name = "none";
static i2c_master_dev_handle_t s_i2c_device = NULL;
static accel_source_t s_source = ACCEL_SOURCE_NONE;
static const char *s_source_name = "Demo";
static button_handle_t s_buttons[BSP_BUTTON_NUM] = {0};
static bool s_buttons_ready = false;
static qma_format_t s_qma_format = {
    .reg_base = 0x01,
    .shift = 4,
    .lsb_per_g = 1024.0f,
    .ready = false,
};

/* Motion-filter state (screen frame). */
static int   s_orient = ACCEL_ORIENT_DEFAULT;
static float s_grav_x = 0.0f;   /* low-pass gravity estimate */
static float s_grav_y = 0.0f;
static bool  s_primed = false;  /* seed the filter on the first real sample */

static void accel_input_remove_device(void)
{
    if (s_i2c_device != NULL) {
        i2c_master_bus_rm_device(s_i2c_device);
        s_i2c_device = NULL;
    }
}

static void accel_input_release_owned_bus(void)
{
    accel_input_remove_device();
    if (s_i2c_bus_owned && s_i2c_bus != NULL) {
        i2c_del_master_bus(s_i2c_bus);
    }
    s_i2c_bus = NULL;
    s_i2c_bus_owned = false;
    s_i2c_bus_name = "none";
}

static esp_err_t accel_input_select_bsp_bus(void)
{
    accel_input_remove_device();
    esp_err_t ret = bsp_i2c_init();
    if (ret != ESP_OK) {
        return ret;
    }

    if (s_i2c_bus_owned && s_i2c_bus != NULL) {
        i2c_del_master_bus(s_i2c_bus);
    }
    s_i2c_bus = bsp_i2c_get_handle();
    s_i2c_bus_owned = false;
    s_i2c_bus_name = "BSP";
    return (s_i2c_bus != NULL) ? ESP_OK : ESP_FAIL;
}

static esp_err_t accel_input_add_device(const uint8_t address)
{
    if (s_i2c_bus == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = address,
        .scl_speed_hz = ACCEL_I2C_FREQ_HZ,
    };
    return i2c_master_bus_add_device(s_i2c_bus, &dev_cfg, &s_i2c_device);
}

static esp_err_t accel_input_read_reg(const uint8_t reg, uint8_t *data, const size_t len)
{
    return i2c_master_transmit_receive(s_i2c_device, &reg, 1, data, len, ACCEL_I2C_TIMEOUT_MS);
}

static esp_err_t accel_input_write_reg(const uint8_t reg, const uint8_t value)
{
    uint8_t payload[2] = {reg, value};
    return i2c_master_transmit(s_i2c_device, payload, sizeof(payload), ACCEL_I2C_TIMEOUT_MS);
}

static void accel_input_log_i2c_scan(void)
{
    if (s_i2c_bus == NULL) {
        return;
    }

    ESP_LOGW(TAG, "Scanning %s I2C bus for unknown devices...", s_i2c_bus_name);

    for (uint8_t address = 0x08; address < 0x78; ++address) {
        if (i2c_master_probe(s_i2c_bus, address, ACCEL_I2C_TIMEOUT_MS) != ESP_OK) {
            continue;
        }

        uint8_t reg00 = 0;
        uint8_t reg0f = 0;
        const char *reg00_state = "n/a";
        const char *reg0f_state = "n/a";

        accel_input_remove_device();
        if (accel_input_add_device(address) == ESP_OK) {
            if (accel_input_read_reg(0x00, &reg00, 1) == ESP_OK) {
                reg00_state = "ok";
            }
            if (accel_input_read_reg(0x0F, &reg0f, 1) == ESP_OK) {
                reg0f_state = "ok";
            }
        }

        ESP_LOGW(TAG, "%s I2C device @0x%02X reg00(%s)=0x%02X reg0F(%s)=0x%02X",
                 s_i2c_bus_name, address, reg00_state, reg00, reg0f_state, reg0f);
    }

    accel_input_remove_device();
}

static bool accel_input_fill_demo(accel_input_sample_t *sample)
{
    sample->x_g = 0.0f;
    sample->y_g = 0.0f;
    sample->z_g = 1.0f;
    sample->valid = true;
    sample->source_name = s_source_name;
    return true;
}

static bool accel_input_fill_buttons(accel_input_sample_t *sample)
{
    if (!s_buttons_ready) {
        return accel_input_fill_demo(sample);
    }

    const float x = (iot_button_get_key_level(s_buttons[BSP_BUTTON_4]) ? 0.20f : 0.0f) -
                    (iot_button_get_key_level(s_buttons[BSP_BUTTON_1]) ? 0.20f : 0.0f);
    const float y = (iot_button_get_key_level(s_buttons[BSP_BUTTON_3]) ? 0.20f : 0.0f) -
                    (iot_button_get_key_level(s_buttons[BSP_BUTTON_2]) ? 0.20f : 0.0f);

    sample->x_g = x;
    sample->y_g = y;
    sample->z_g = 1.0f;
    sample->valid = true;
    sample->source_name = s_source_name;
    return true;
}

static esp_err_t accel_input_init_buttons(void)
{
    esp_err_t ret = bsp_iot_button_create(s_buttons, NULL, BSP_BUTTON_NUM);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "failed to init buttons fallback: %s", esp_err_to_name(ret));
        return ret;
    }

    s_buttons_ready = true;
    s_source = ACCEL_SOURCE_BUTTONS;
    s_source_name = "Buttons";
    ESP_LOGW(TAG, "No live IMU data found, using built-in buttons as fallback control");
    ESP_LOGI(TAG, "Buttons fallback mapping: BTN1=left BTN2=up BTN3=down BTN4=right BTN5=center");
    return ESP_OK;
}

static bool accel_input_qma_decode_block(const uint8_t raw_block[7], const uint8_t reg_base,
                                         const uint8_t shift, const float lsb_per_g,
                                         accel_input_sample_t *sample)
{
    if (raw_block == NULL || sample == NULL) {
        return false;
    }

    const uint8_t offset = (reg_base == 0x02) ? 1 : 0;
    const uint8_t *raw = &raw_block[offset];
    const int16_t raw_x = (int16_t)((((uint16_t)raw[1]) << 8) | raw[0]) >> shift;
    const int16_t raw_y = (int16_t)((((uint16_t)raw[3]) << 8) | raw[2]) >> shift;
    const int16_t raw_z = (int16_t)((((uint16_t)raw[5]) << 8) | raw[4]) >> shift;

    sample->x_g = (float)raw_x / lsb_per_g;
    sample->y_g = (float)raw_y / lsb_per_g;
    sample->z_g = (float)raw_z / lsb_per_g;
    sample->valid = true;
    sample->source_name = s_source_name;
    return true;
}

static bool accel_input_qma_block_is_zero(const uint8_t raw_block[7])
{
    for (size_t i = 0; i < 7; ++i) {
        if (raw_block[i] != 0) {
            return false;
        }
    }
    return true;
}

static void accel_input_qma_select_format(const uint8_t raw_block[7])
{
    static const qma_format_t candidates[] = {
        {.reg_base = 0x01, .shift = 2, .lsb_per_g = 4096.0f, .ready = true},
        {.reg_base = 0x01, .shift = 4, .lsb_per_g = 1024.0f, .ready = true},
        {.reg_base = 0x01, .shift = 6, .lsb_per_g = 256.0f,  .ready = true},
        {.reg_base = 0x02, .shift = 2, .lsb_per_g = 4096.0f, .ready = true},
        {.reg_base = 0x02, .shift = 4, .lsb_per_g = 1024.0f, .ready = true},
        {.reg_base = 0x02, .shift = 6, .lsb_per_g = 256.0f,  .ready = true},
    };

    float best_score = 1e9f;
    qma_format_t best = s_qma_format;

    for (size_t i = 0; i < sizeof(candidates) / sizeof(candidates[0]); ++i) {
        accel_input_sample_t candidate_sample = {0};
        accel_input_qma_decode_block(raw_block, candidates[i].reg_base, candidates[i].shift,
                                     candidates[i].lsb_per_g, &candidate_sample);

        const float magnitude = sqrtf(candidate_sample.x_g * candidate_sample.x_g +
                                      candidate_sample.y_g * candidate_sample.y_g +
                                      candidate_sample.z_g * candidate_sample.z_g);
        float score = fabsf(magnitude - 1.0f);

        if (magnitude < 0.20f || magnitude > 3.00f) {
            score += 3.0f;
        }
        if (candidates[i].reg_base == 0x01) {
            score -= 0.02f;
        }

        if (score < best_score) {
            best_score = score;
            best = candidates[i];
        }
    }

    s_qma_format = best;
    s_qma_format.ready = true;
}

static bool accel_input_fill_qma(accel_input_sample_t *sample)
{
    uint8_t raw_block[7] = {0};
    if (accel_input_read_reg(0x01, raw_block, sizeof(raw_block)) != ESP_OK) {
        return false;
    }
    if (accel_input_qma_block_is_zero(raw_block)) {
        return false;
    }

    if (!s_qma_format.ready) {
        accel_input_qma_select_format(raw_block);
    }

    return accel_input_qma_decode_block(raw_block, s_qma_format.reg_base, s_qma_format.shift,
                                        s_qma_format.lsb_per_g, sample);
}

static bool accel_input_try_probe_qma7981(const uint8_t address)
{
    uint8_t reg00 = 0;
    uint8_t reg0f = 0;

    accel_input_remove_device();
    if (accel_input_add_device(address) != ESP_OK) {
        return false;
    }
    /* Require the genuine QMA7981 chip id (reg00 == 0xE7). Do NOT accept
     * reg0F == 0x11 here: that is the SC7A20/LIS3DH WHO_AM_I, and accepting it
     * made the QMA path hijack the SC7A20 and read the wrong data registers
     * (garbage X/Y, dead Z). The SC7A20 is handled by the LIS3DH probe instead. */
    if (accel_input_read_reg(0x00, &reg00, 1) != ESP_OK ||
        accel_input_read_reg(0x0F, &reg0f, 1) != ESP_OK ||
        reg00 != 0xE7) {
        accel_input_remove_device();
        return false;
    }

    ESP_LOGI(TAG, "QMA7981 found @0x%02X (reg00=0x%02X reg0F=0x%02X), initializing...",
             address, reg00, reg0f);

    /* QMA7981 init: ODR=100Hz -> normal mode -> wait first frame */
    ESP_ERROR_CHECK_WITHOUT_ABORT(accel_input_write_reg(0x10, 0x05)); /* ODR 100Hz */
    ESP_ERROR_CHECK_WITHOUT_ABORT(accel_input_write_reg(0x11, 0x01)); /* normal mode */
    vTaskDelay(pdMS_TO_TICKS(50));                                    /* wait for first frame */

    /* Verify the data registers actually produce non-zero output */
    uint8_t raw_block[7] = {0};
    if (accel_input_read_reg(0x01, raw_block, sizeof(raw_block)) != ESP_OK ||
        accel_input_qma_block_is_zero(raw_block)) {
        ESP_LOGW(TAG, "QMA7981 @0x%02X: data still zero after init, skipping", address);
        accel_input_remove_device();
        return false;
    }

    s_qma_format.ready = false;
    accel_input_qma_select_format(raw_block);
    s_source = ACCEL_SOURCE_QMA7981;
    s_source_name = "QMA7981";
    ESP_LOGI(TAG, "Detected accelerometer: %s @ 0x%02X on %s (shift=%u scale=%.0f)",
             s_source_name, address, s_i2c_bus_name,
             s_qma_format.shift, s_qma_format.lsb_per_g);
    return true;
}

static bool accel_input_try_probe_mpu6050(const uint8_t address)
{
    uint8_t who_am_i = 0;

    accel_input_remove_device();
    if (accel_input_add_device(address) != ESP_OK) {
        return false;
    }
    if (accel_input_read_reg(0x75, &who_am_i, 1) != ESP_OK ||
        (who_am_i != 0x68 && who_am_i != 0x70 && who_am_i != 0x71)) {
        accel_input_remove_device();
        return false;
    }

    ESP_ERROR_CHECK_WITHOUT_ABORT(accel_input_write_reg(0x6B, 0x00));
    vTaskDelay(pdMS_TO_TICKS(10));
    ESP_ERROR_CHECK_WITHOUT_ABORT(accel_input_write_reg(0x1A, 0x03));
    ESP_ERROR_CHECK_WITHOUT_ABORT(accel_input_write_reg(0x1B, 0x00));
    ESP_ERROR_CHECK_WITHOUT_ABORT(accel_input_write_reg(0x1C, 0x00));
    s_source = ACCEL_SOURCE_MPU6050;
    s_source_name = "MPU6050";
    ESP_LOGI(TAG, "Detected accelerometer: %s @ 0x%02X on %s", s_source_name, address, s_i2c_bus_name);
    return true;
}

static bool accel_input_try_probe_lis3dh_family(const uint8_t address)
{
    uint8_t who_am_i = 0;

    accel_input_remove_device();
    if (accel_input_add_device(address) != ESP_OK) {
        return false;
    }
    if (accel_input_read_reg(0x0F, &who_am_i, 1) != ESP_OK) {
        accel_input_remove_device();
        return false;
    }

    /* 0x33 = LIS3DH, 0x11 = SC7A20 (LIS3DH protocol compatible) */
    if (who_am_i != 0x33 && who_am_i != 0x11) {
        accel_input_remove_device();
        return false;
    }

    /* ODR=100Hz, all axes enabled; high resolution, +-2g */
    ESP_ERROR_CHECK_WITHOUT_ABORT(accel_input_write_reg(0x20, 0x57));
    ESP_ERROR_CHECK_WITHOUT_ABORT(accel_input_write_reg(0x23, 0x88));

    s_source = ACCEL_SOURCE_LIS3DH;
    s_source_name = (who_am_i == 0x11) ? "SC7A20" : "LIS3DH";
    ESP_LOGI(TAG, "Detected accelerometer: %s (WHO_AM_I=0x%02X) @ 0x%02X on %s",
             s_source_name, who_am_i, address, s_i2c_bus_name);
    return true;
}

static bool accel_input_try_supported_sensors_on_current_bus(void)
{
    /* Probe the SC7A20/LIS3DH family FIRST. It shares addresses 0x18/0x19 with
     * the QMA7981, and the SC7A20 (WHO_AM_I 0x0F == 0x11) must be read via the
     * LIS3DH register map (0x28..) to give correct, in-plane X/Y/Z. If we let
     * the QMA probe run first it would misread the SC7A20. A genuine QMA7981
     * (WHO_AM_I != 0x11) is skipped here and caught by the QMA probe below. */
    if (accel_input_try_probe_lis3dh_family(0x18) ||
        accel_input_try_probe_lis3dh_family(0x19)) {
        return true;
    }

    static const uint8_t qma_addresses[] = {0x18, 0x19, 0x12, 0x13, 0x14};
    for (size_t i = 0; i < sizeof(qma_addresses) / sizeof(qma_addresses[0]); ++i) {
        if (accel_input_try_probe_qma7981(qma_addresses[i])) {
            return true;
        }
    }

    return accel_input_try_probe_mpu6050(0x68) || accel_input_try_probe_mpu6050(0x69);
}

/* ---------------- orientation calibration (sensor->screen axis map) -------- */

static void accel_orient_load(void)
{
    nvs_handle_t h;
    if (nvs_open(ACCEL_NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        uint8_t v = ACCEL_ORIENT_DEFAULT;
        if (nvs_get_u8(h, ACCEL_NVS_KEY, &v) == ESP_OK) {
            s_orient = v & 7;
        }
        nvs_close(h);
    }
}

static void accel_orient_save(void)
{
    nvs_handle_t h;
    if (nvs_open(ACCEL_NVS_NS, NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_u8(h, ACCEL_NVS_KEY, (uint8_t)(s_orient & 7));
        nvs_commit(h);
        nvs_close(h);
    }
}

/* Map raw sensor in-plane axes to screen axes (+x right, +y down).
 * bit0: swap x/y, bit1: flip screen-x, bit2: flip screen-y. */
static void accel_apply_orientation(float sx, float sy, float *out_x, float *out_y)
{
    const int o = s_orient & 7;
    float a = (o & 1) ? sy : sx;
    float b = (o & 1) ? sx : sy;
    *out_x = (o & 2) ? -a : a;
    *out_y = (o & 4) ? -b : b;
}

int accel_input_get_orientation(void)
{
    return s_orient & 7;
}

void accel_input_set_orientation(int idx)
{
    s_orient = idx & 7;
    s_primed = false; /* re-seed the filter so the flip doesn't cause a slosh */
    accel_orient_save();
    ESP_LOGI(TAG, "accel orientation set to %d", s_orient);
}

void accel_input_cycle_orientation(void)
{
    accel_input_set_orientation((s_orient + 1) & 7);
}

esp_err_t accel_input_init(void)
{
    accel_orient_load();

    esp_err_t ret = accel_input_select_bsp_bus();
    if (ret == ESP_OK && accel_input_try_supported_sensors_on_current_bus()) {
        return ESP_OK;
    }

    if (ret == ESP_OK) {
        accel_input_log_i2c_scan();
    }
    accel_input_release_owned_bus();
    return accel_input_init_buttons();
}

bool accel_input_poll(accel_input_sample_t *sample)
{
    if (sample == NULL) {
        return false;
    }

    memset(sample, 0, sizeof(*sample));

    if (s_source == ACCEL_SOURCE_MPU6050) {
        uint8_t raw[6] = {0};
        if (accel_input_read_reg(0x3B, raw, sizeof(raw)) == ESP_OK) {
            const int16_t raw_x = (int16_t)((raw[0] << 8) | raw[1]);
            const int16_t raw_y = (int16_t)((raw[2] << 8) | raw[3]);
            const int16_t raw_z = (int16_t)((raw[4] << 8) | raw[5]);
            sample->x_g = (float)raw_x / 16384.0f;
            sample->y_g = (float)raw_y / 16384.0f;
            sample->z_g = (float)raw_z / 16384.0f;
            sample->valid = true;
            sample->source_name = s_source_name;
            return true;
        }
    } else if (s_source == ACCEL_SOURCE_LIS3DH) {
        uint8_t raw[6] = {0};
        if (accel_input_read_reg(0x28 | 0x80, raw, sizeof(raw)) == ESP_OK) {
            const int16_t raw_x = (int16_t)(((uint16_t)raw[1] << 8) | raw[0]) >> 4;
            const int16_t raw_y = (int16_t)(((uint16_t)raw[3] << 8) | raw[2]) >> 4;
            const int16_t raw_z = (int16_t)(((uint16_t)raw[5] << 8) | raw[4]) >> 4;
            sample->x_g = (float)raw_x / 1024.0f;
            sample->y_g = (float)raw_y / 1024.0f;
            sample->z_g = (float)raw_z / 1024.0f;
            sample->valid = true;
            sample->source_name = s_source_name;
            return true;
        }
    } else if (s_source == ACCEL_SOURCE_QMA7981) {
        if (accel_input_fill_qma(sample)) {
            return true;
        }
    } else if (s_source == ACCEL_SOURCE_BUTTONS) {
        return accel_input_fill_buttons(sample);
    }

    return accel_input_fill_demo(sample);
}

bool accel_input_uses_buttons(void)
{
    return s_source == ACCEL_SOURCE_BUTTONS && s_buttons_ready;
}

button_handle_t accel_input_button(int idx)
{
    if (!s_buttons_ready || idx < 0 || idx >= BSP_BUTTON_NUM) {
        return NULL;
    }
    return s_buttons[idx];
}

void accel_input_read_motion(accel_motion_t *out)
{
    if (out == NULL) {
        return;
    }
    memset(out, 0, sizeof(*out));

    accel_input_sample_t s;
    accel_input_poll(&s);
    out->valid = s.valid;
    out->source_name = s.source_name;

    /* Map the raw reading into screen axes. The button fallback already reports
     * in screen convention, so it bypasses the (sensor-only) orientation map. */
    float sx, sy;
    if (s_source == ACCEL_SOURCE_BUTTONS) {
        sx = s.x_g;
        sy = s.y_g;
    } else {
        accel_apply_orientation(s.x_g, s.y_g, &sx, &sy);
    }

    /* Complementary split: slow low-pass = gravity, fast residual = motion. */
    if (!s_primed) {
        s_grav_x = sx;
        s_grav_y = sy;
        s_primed = true;
    } else {
        s_grav_x += ACCEL_GRAV_ALPHA * (sx - s_grav_x);
        s_grav_y += ACCEL_GRAV_ALPHA * (sy - s_grav_y);
    }

    /* Gravity with a radial dead zone (a flat board reads ~0 and sits still). */
    float gx = s_grav_x;
    float gy = s_grav_y;
    const float gm = sqrtf(gx * gx + gy * gy);
    if (gm < ACCEL_DEAD_G) {
        gx = 0.0f;
        gy = 0.0f;
    } else {
        const float k = (gm - ACCEL_DEAD_G) / gm;
        gx *= k;
        gy *= k;
    }
    out->grav_x = gx;
    out->grav_y = gy;
    out->tilt = gm;

    /* Motion residual (device shake), also dead-zoned so noise doesn't slosh. */
    float mx = sx - s_grav_x;
    float my = sy - s_grav_y;
    if (sqrtf(mx * mx + my * my) < ACCEL_DEAD_MOTION) {
        mx = 0.0f;
        my = 0.0f;
    }
    out->motion_x = mx;
    out->motion_y = my;
}
