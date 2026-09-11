/*
 * SPDX-License-Identifier: CC0-1.0
 * See transport.h. One task does: read IMU -> POST -> parse reply -> store.
 */
#include "transport.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "accel_input.h"
#include "wifi_link.h"

static const char *TAG = "transport";
#define TX_PATH "/api/telemetry"
#define TX_HTTP_TIMEOUT_MS 3000

static transport_status_t s_st;
static SemaphoreHandle_t s_lock;
static volatile bool s_ask_pending;
static char s_ask_text[64] = "我现在的运动状态怎么样？";
static uint32_t s_activity_flip_count;
static char s_last_activity[TRANSPORT_ACTIVITY_LEN];

/* Like strlcpy but never cuts a multi-byte UTF-8 character in half (a torn
 * trailing sequence renders as garbage at the end of LVGL labels). */
static void utf8_strlcpy(char *dst, const char *src, size_t cap)
{
    if (cap == 0) {
        return;
    }
    size_t n = strlen(src);
    if (n > cap - 1) {
        n = cap - 1;
        while (n > 0 && ((unsigned char)src[n] & 0xC0) == 0x80) {
            n--;   /* back off continuation bytes; src[n] is now a lead byte */
        }
    }
    memcpy(dst, src, n);
    dst[n] = '\0';
}

static void status_lock(void)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
}

static void status_unlock(void)
{
    xSemaphoreGive(s_lock);
}

/* Build the telemetry JSON. Returns a cJSON_PrintUnformatted string that the
 * caller must free(), or NULL. */
static char *build_body(const accel_input_sample_t *s, bool ask)
{
    char *out = NULL;
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return NULL;
    }
    cJSON_AddNumberToObject(root, "x", (double)s->x_g);
    cJSON_AddNumberToObject(root, "y", (double)s->y_g);
    cJSON_AddNumberToObject(root, "z", (double)s->z_g);
    cJSON_AddStringToObject(root, "source", s->source_name ? s->source_name : "?");
    cJSON_AddBoolToObject(root, "ask", ask);
    if (ask) {
        cJSON_AddStringToObject(root, "q", s_ask_text);
    }
    out = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return out;
}

static void apply_response(const char *body, size_t len)
{
    char activity[TRANSPORT_ACTIVITY_LEN] = "";
    char reply[TRANSPORT_REPLY_LEN] = "";
    bool ok = false;

    if (body != NULL && len > 0) {
        cJSON *root = cJSON_ParseWithLength(body, len);
        if (root != NULL) {
            const cJSON *jok = cJSON_GetObjectItemCaseSensitive(root, "ok");
            const cJSON *jact = cJSON_GetObjectItemCaseSensitive(root, "activity");
            const cJSON *jrep = cJSON_GetObjectItemCaseSensitive(root, "reply");
            ok = cJSON_IsTrue(jok);
            if (cJSON_IsString(jact) && jact->valuestring != NULL) {
                utf8_strlcpy(activity, jact->valuestring, sizeof(activity));
            }
            if (cJSON_IsString(jrep) && jrep->valuestring != NULL) {
                utf8_strlcpy(reply, jrep->valuestring, sizeof(reply));
            }
            cJSON_Delete(root);
        }
    }

    status_lock();
    s_st.posts_ok += ok ? 1 : 0;
    s_st.posts_fail += ok ? 0 : 1;
    s_st.server_ok = ok;
    s_st.fail_streak = ok ? 0 : s_st.fail_streak + 1;
    if (activity[0] != '\0') {
        strlcpy(s_st.activity, activity, sizeof(s_st.activity));
    }
    if (reply[0] != '\0') {
        strlcpy(s_st.reply, reply, sizeof(s_st.reply));
    }
    status_unlock();
}

/* Response body is collected during esp_http_client_perform() via the
 * HTTP_EVENT_ON_DATA callback — the most reliable pattern for esp_http_client
 * (post-perform esp_http_client_read can return 0 on HTTP/1.0 responses). */
typedef struct {
    char buf[1200];
    int  len;
} resp_acc_t;

static esp_err_t http_event_handler(esp_http_client_event_t *evt)
{
    if (evt->event_id == HTTP_EVENT_ON_DATA && evt->user_data != NULL) {
        resp_acc_t *acc = (resp_acc_t *)evt->user_data;
        int space = (int)sizeof(acc->buf) - acc->len - 1;
        int n = evt->data_len < space ? evt->data_len : space;
        if (n > 0) {
            memcpy(acc->buf + acc->len, evt->data, (size_t)n);
            acc->len += n;
            acc->buf[acc->len] = '\0';
        }
    }
    return ESP_OK;
}

static void post_once(const accel_input_sample_t *sample, bool ask)
{
    char url[160];
    snprintf(url, sizeof(url), "%s%s", CONFIG_RW1_SERVER_URL, TX_PATH);

    char *body = build_body(sample, ask);
    if (body == NULL) {
        return;
    }

    resp_acc_t acc = {.len = 0};
    acc.buf[0] = '\0';
    esp_http_client_config_t cfg = {
        .url = url,
        .timeout_ms = TX_HTTP_TIMEOUT_MS,
        .event_handler = http_event_handler,
        .user_data = &acc,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        free(body);
        return;
    }

    esp_http_client_set_method(client, HTTP_METHOD_POST);
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, body, strlen(body));

    esp_err_t ret = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    free(body);

    if (ret == ESP_OK && status == 200 && acc.len > 0) {
        apply_response(acc.buf, (size_t)acc.len);
        if (ask) {
            ESP_LOGI(TAG, "AI reply: %s", acc.buf);
        }
    } else {
        apply_response(NULL, 0);
        ESP_LOGW(TAG, "POST failed ret=%s status=%d body=%d",
                 esp_err_to_name(ret), status, acc.len);
    }
}

static void transport_task(void *arg)
{
    ESP_LOGI(TAG, "waiting for WiFi...");
    while (!wifi_link_is_up()) {
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    ESP_LOGI(TAG, "telemetry -> %s%s every %d ms",
             CONFIG_RW1_SERVER_URL, TX_PATH, CONFIG_RW1_TELEMETRY_PERIOD_MS);

    while (true) {
        accel_input_sample_t sample = {0};
        bool have = accel_input_poll(&sample);
        if (!have) {
            sample.x_g = 0.0f;
            sample.y_g = 0.0f;
            sample.z_g = 1.0f;
            sample.source_name = "none";
        }

        bool ask = s_ask_pending;
        post_once(&sample, ask);
        if (ask) {
            s_ask_pending = false;   /* consumed regardless, retried if failed via reply check */
        }

        status_lock();
        s_st.x_g = sample.x_g;
        s_st.y_g = sample.y_g;
        s_st.z_g = sample.z_g;
        strlcpy(s_st.source, sample.source_name ? sample.source_name : "?", sizeof(s_st.source));
        bool ok = s_st.server_ok;
        char act[TRANSPORT_ACTIVITY_LEN];
        strlcpy(act, s_st.activity, sizeof(act));
        status_unlock();

        if (ok && strcmp(act, s_last_activity) != 0) {
            strlcpy(s_last_activity, act, sizeof(s_last_activity));
            s_activity_flip_count++;
            ESP_LOGI(TAG, "activity: %s (x=%+.2f y=%+.2f z=%+.2f)",
                     act, sample.x_g, sample.y_g, sample.z_g);
        }
        if ((s_activity_flip_count % 20) == 0) {
            ESP_LOGI(TAG, "link %s ok=%u fail=%u", ok ? "OK" : "DOWN",
                     (unsigned)s_st.posts_ok, (unsigned)s_st.posts_fail);
        }

        vTaskDelay(pdMS_TO_TICKS(CONFIG_RW1_TELEMETRY_PERIOD_MS));
    }
}

void transport_start(void)
{
    if (s_lock == NULL) {
        s_lock = xSemaphoreCreateMutex();
    }
    xTaskCreatePinnedToCore(transport_task, "transport", 8192, NULL, 5, NULL, 0);
}

void transport_request_ask(const char *question)
{
    if (question != NULL && question[0] != '\0') {
        strlcpy(s_ask_text, question, sizeof(s_ask_text));
    }
    s_ask_pending = true;
    ESP_LOGI(TAG, "ask queued: %s", s_ask_text);
}

void transport_get_status(transport_status_t *out)
{
    if (s_lock == NULL) {
        memset(out, 0, sizeof(*out));
        return;
    }
    status_lock();
    memcpy(out, &s_st, sizeof(*out));
    status_unlock();
}
