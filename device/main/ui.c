/*
 * SPDX-License-Identifier: CC0-1.0
 * See ui.h. An LVGL timer (runs in the display task context, safe for LVGL)
 * refreshes the labels from transport_get_status() every 500 ms.
 */
#include "ui.h"

#include <stdio.h>
#include <string.h>

#include "bsp/esp-bsp.h"
#include "esp_log.h"
#include "lvgl.h"

#include "transport.h"
#include "wifi_link.h"

#if CONFIG_LV_USE_TINY_TTF
#include "libs/tiny_ttf/lv_tiny_ttf.h"
#include "rw1_font.h"
#endif

static const char *TAG = "ui";

static lv_obj_t *s_lbl_status;
static lv_obj_t *s_lbl_act;
static lv_obj_t *s_lbl_data;
static lv_obj_t *s_lbl_reply;
static char s_last_reply[TRANSPORT_REPLY_LEN];

/* LVGL's built-in CJK demo fonts cover only a handful of random glyphs, so we
 * embed a proper SimHei subset (rw1/tools/gen_font.py) and rasterise it with
 * tiny_ttf. Must be created under the LVGL lock (ui_init holds it). */
static const lv_font_t *CJK_FONT(void)
{
#if CONFIG_LV_USE_TINY_TTF
    static lv_font_t *s_tiny_font;
    if (s_tiny_font == NULL) {
        s_tiny_font = lv_tiny_ttf_create_data(rw1_font_ttf, rw1_font_ttf_len, 16);
        if (s_tiny_font == NULL) {
            ESP_LOGE(TAG, "tiny_ttf font create failed");
        }
    }
    return s_tiny_font ? s_tiny_font : &lv_font_montserrat_14;
#else
    return &lv_font_montserrat_14;
#endif
}

static void style_label(lv_obj_t *lbl, uint32_t color)
{
    lv_obj_set_style_text_font(lbl, CJK_FONT(), 0);
    lv_obj_set_style_text_color(lbl, lv_color_hex(color), 0);
}

static void ui_timer_cb(lv_timer_t *timer)
{
    (void)timer;
    transport_status_t st;
    transport_get_status(&st);

    char buf[128];
    snprintf(buf, sizeof(buf), "%s · 服务器:%s · 发送%u",
             wifi_link_state_str(), st.server_ok ? "OK" : "无连接",
             (unsigned)(st.posts_ok + st.posts_fail));
    lv_label_set_text(s_lbl_status, buf);
    lv_obj_set_style_text_color(s_lbl_status,
                                lv_color_hex(st.server_ok ? 0x7ee787 : 0xf85149), 0);

    lv_label_set_text(s_lbl_act, st.activity[0] ? st.activity : "等待服务器…");

    snprintf(buf, sizeof(buf), "IMU %s  X%+.2f Y%+.2f Z%+.2f",
             st.source, st.x_g, st.y_g, st.z_g);
    lv_label_set_text(s_lbl_data, buf);

    /* Only rewrite the (wrapped) reply label when the text actually changed. */
    if (strcmp(st.reply, s_last_reply) != 0) {
        strlcpy(s_last_reply, st.reply, sizeof(s_last_reply));
        lv_label_set_text(s_lbl_reply,
                          st.reply[0] ? st.reply : "按 BOOT 键向电脑服务器的AI提问");
    }
}

esp_err_t ui_init(void)
{
    bsp_display_lock(0);

    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x0d1117), 0);

    lv_obj_t *title = lv_label_create(scr);
    lv_label_set_text(title, "AI交互课 · 第1周");
    style_label(title, 0x8b949e);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 6);

    s_lbl_status = lv_label_create(scr);
    style_label(s_lbl_status, 0x7ee787);
    lv_obj_align(s_lbl_status, LV_ALIGN_TOP_MID, 0, 28);

    s_lbl_act = lv_label_create(scr);
    style_label(s_lbl_act, 0x7ee787);
    lv_obj_set_style_text_color(s_lbl_act, lv_color_hex(0x7ee787), 0);
    lv_label_set_text(s_lbl_act, "启动中…");
    lv_obj_set_width(s_lbl_act, 228);
    lv_label_set_long_mode(s_lbl_act, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_align(s_lbl_act, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(s_lbl_act, LV_ALIGN_CENTER, 0, -38);

    s_lbl_data = lv_label_create(scr);
    style_label(s_lbl_data, 0x79c0ff);
    lv_obj_align(s_lbl_data, LV_ALIGN_CENTER, 0, 8);

    s_lbl_reply = lv_label_create(scr);
    style_label(s_lbl_reply, 0xe3b341);
    lv_obj_set_width(s_lbl_reply, 224);
    lv_label_set_long_mode(s_lbl_reply, LV_LABEL_LONG_WRAP);
    lv_label_set_text(s_lbl_reply, "按 BOOT 键向电脑服务器的AI提问");
    lv_obj_align(s_lbl_reply, LV_ALIGN_BOTTOM_MID, 0, -22);

    lv_timer_create(ui_timer_cb, 500, NULL);

    bsp_display_unlock();
    ESP_LOGI(TAG, "ui ready");
    return ESP_OK;
}
