/*
 * SPDX-License-Identifier: MIT
 *
 * 板载麦克风录音 —— 见 audio.h 的说明。
 *
 * 用 BSP 的 bsp_audio_init() + bsp_audio_codec_microphone_init()，
 * 再走 esp_codec_dev 的 read 接口取 PCM。这三步都在 BSP 里包好了，
 * 自己配 I2S 只会把 BSP 已经调对的时钟/位宽再错一遍。
 */
#include "audio.h"

#include "bsp/esp-bsp.h"
#include "esp_codec_dev.h"
#include "esp_heap_caps.h"
#include "esp_log.h"

static const char *TAG = "audio";

static esp_codec_dev_handle_t s_mic;
static uint8_t *s_buf;          /* PSRAM，AUDIO_MAX_BYTES 字节 */

esp_err_t audio_init(void)
{
    if (bsp_audio_init(NULL) != ESP_OK) {
        ESP_LOGW(TAG, "bsp_audio_init 失败，语音功能不可用");
        return ESP_FAIL;
    }
    s_mic = bsp_audio_codec_microphone_init();
    if (s_mic == NULL) {
        ESP_LOGW(TAG, "麦克风初始化失败，语音功能不可用");
        return ESP_FAIL;
    }

    esp_codec_dev_sample_info_t fs = {
        .bits_per_sample = 16,
        .channel = 1,
        .channel_mask = 0,
        .sample_rate = AUDIO_RATE_HZ,
        .mclk_multiple = 0,          /* 0 = 交给驱动按 sample_rate*256 算 */
    };
    if (esp_codec_dev_open(s_mic, &fs) != ESP_OK) {
        ESP_LOGW(TAG, "codec 打开失败，语音功能不可用");
        s_mic = NULL;
        return ESP_FAIL;
    }

    s_buf = heap_caps_malloc(AUDIO_MAX_BYTES, MALLOC_CAP_SPIRAM);
    if (s_buf == NULL) {
        ESP_LOGW(TAG, "PSRAM 分配 %d 字节失败，语音功能不可用", AUDIO_MAX_BYTES);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "麦克风就绪：%d Hz / 16 bit / 单声道，每次录 %d ms",
             AUDIO_RATE_HZ, AUDIO_RECORD_MS);
    return ESP_OK;
}

bool audio_ready(void)
{
    return s_mic != NULL && s_buf != NULL;
}

const uint8_t *audio_record(size_t *out_len)
{
    if (out_len != NULL) {
        *out_len = 0;
    }
    if (!audio_ready()) {
        return NULL;
    }

    /* 分段读，而不是一次要 96 KB：一次大读会让 I2S 侧缓冲吃紧，
     * 而且中途出错时前面已经读到的部分还能救回来（至少听得到前半句）。 */
    size_t got = 0;
    const size_t chunk = 4096;
    while (got < AUDIO_MAX_BYTES) {
        size_t want = AUDIO_MAX_BYTES - got;
        if (want > chunk) {
            want = chunk;
        }
        int n = esp_codec_dev_read(s_mic, s_buf + got, (int)want);
        if (n <= 0) {
            ESP_LOGW(TAG, "读取失败（已录 %u/%u 字节）",
                     (unsigned)got, (unsigned)AUDIO_MAX_BYTES);
            break;
        }
        got += (size_t)n;
    }

    if (out_len != NULL) {
        *out_len = got;
    }
    if (got == 0) {
        return NULL;
    }
    ESP_LOGI(TAG, "录到 %u 字节（约 %u ms）", (unsigned)got,
             (unsigned)(got * 1000 / (AUDIO_RATE_HZ * 2)));
    return s_buf;
}
