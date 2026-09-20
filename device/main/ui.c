/*
 * SPDX-License-Identifier: MIT
 *
 * See ui.h. An LVGL timer (runs in the display task context, safe for LVGL)
 * refreshes the labels from transport_get_status() every 500 ms.
 *
 * Every label has an explicit size and LV_LABEL_LONG_DOT, so a long AI reply is
 * clipped with "…" instead of growing past the 240x240 screen and printing over
 * the labels above it (which is what a wrapped, unbounded label did before).
 */
#include "ui.h"

#include <stdio.h>
#include <string.h>

#include "bsp/esp-bsp.h"
#include "esp_log.h"
#include "lvgl.h"

#include "transport.h"
#include "provisioning.h"
#include "wifi_link.h"

#if CONFIG_LV_USE_TINY_TTF
#include "libs/tiny_ttf/lv_tiny_ttf.h"
#include "rw1_font.h"
#endif

static const char *TAG = "ui";

#define SCREEN_W 240

static lv_obj_t *s_lbl_status;
static lv_obj_t *s_lbl_act;
static lv_obj_t *s_lbl_data;
static lv_obj_t *s_lbl_reply;
static char s_last_reply[TRANSPORT_REPLY_LEN];
static bool s_last_pending;
static bool s_prov_shown;      /* 上一帧是否在显示配网屏幕（用于切回时重画） */

/* LVGL's built-in CJK demo fonts cover only a handful of random glyphs, so we
 * embed a proper SimHei subset (tools/gen_font.py) and rasterise it with
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

/* Fixed-size, clipped, wrapping label. */
static lv_obj_t *make_label(lv_obj_t *parent, uint32_t color, int y, int w, int h)
{
    lv_obj_t *lbl = lv_label_create(parent);
    lv_obj_set_style_text_font(lbl, CJK_FONT(), 0);
    lv_obj_set_style_text_color(lbl, lv_color_hex(color), 0);
    lv_obj_set_style_text_align(lbl, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_size(lbl, w, h);
    lv_label_set_long_mode(lbl, LV_LABEL_LONG_DOT);
    lv_obj_align(lbl, LV_ALIGN_TOP_MID, 0, y);
    return lbl;
}

static void ui_timer_cb(lv_timer_t *timer)
{
    (void)timer;
    transport_status_t st;
    transport_get_status(&st);

    char buf[128];

    /* 配网模式：这时候板子还没连上任何网络，遥测数据没有意义，
     * 而屏幕是用户唯一的"说明书"——必须把 AP 名、4 位密码、要打开的地址
     * 显示清楚（PROPOSAL §1.8 验收 #1）。整屏让给配网提示。 */
    if (provisioning_is_active()) {
        s_prov_shown = true;

        lv_label_set_text(s_lbl_status, "配网模式 · 请用手机连接下面的热点");
        lv_obj_set_style_text_color(s_lbl_status, lv_color_hex(0xe3b341), 0);

        snprintf(buf, sizeof(buf), "① WiFi: %s\n② 密码: %s",
                 provisioning_ap_ssid(), provisioning_ap_pass());
        lv_label_set_text(s_lbl_act, buf);
        lv_obj_set_style_text_color(s_lbl_act, lv_color_hex(0x7ee787), 0);

        lv_label_set_text(s_lbl_data, "③ 浏览器打开 " PROV_AP_IP);
        lv_obj_set_style_text_color(s_lbl_data, lv_color_hex(0x79c0ff), 0);

        const char *res = provisioning_last_result();
        lv_label_set_text(s_lbl_reply, res[0] ? res : "④ 选 WiFi、填服务器地址，点「保存并连接」");
        lv_obj_set_style_text_color(s_lbl_reply, lv_color_hex(0xe3b341), 0);
        return;
    }

    if (s_prov_shown) {
        /* 刚从配网屏幕切回来：清掉回复文本的缓存，强制重画那一行 ——
         * 否则若回复内容与配网前恰好相同，下面那段"只在变化时才写"会跳过它，
         * 屏幕上就残留着配网提示。 */
        s_prov_shown = false;
        s_last_reply[0] = '\0';
    }

    snprintf(buf, sizeof(buf), "%s · 服务器:%s · %dHz",
             wifi_link_state_str(), st.server_ok ? "OK" : "无连接",
             (int)(1000 / CONFIG_RW1_SAMPLE_PERIOD_MS));
    lv_label_set_text(s_lbl_status, buf);
    lv_obj_set_style_text_color(s_lbl_status,
                                lv_color_hex(st.server_ok ? 0x7ee787 : 0xf85149), 0);

    lv_label_set_text(s_lbl_act, st.activity[0] ? st.activity : "等待服务器…");

    /* 远程指令状态：让现场能直接看见「网页下发的指令到了、正在采、采完了」 */
    static const char *CMD_MARK[] = {"", " 采集中", " 采集OK", " 采集NG"};
    const char *mark = CMD_MARK[(st.cmd_state < 4) ? st.cmd_state : 0];
    snprintf(buf, sizeof(buf), "%s  o%d  %d/批%s\nX%+.2f Y%+.2f Z%+.2f",
             st.source, st.orient, st.batch_last, mark, st.x_g, st.y_g, st.z_g);
    lv_label_set_text(s_lbl_data, buf);
    /* 指令失败时把数据行染红，现场一眼能看出来 */
    lv_obj_set_style_text_color(s_lbl_data,
                                lv_color_hex(st.cmd_state == TRANSPORT_CMD_FAILED
                                             ? 0xf85149 : 0x79c0ff), 0);

    /* Only rewrite the reply label when the text actually changed. */
    if (strcmp(st.reply, s_last_reply) != 0 || st.ai_pending != s_last_pending) {
        strlcpy(s_last_reply, st.reply, sizeof(s_last_reply));
        s_last_pending = st.ai_pending;
        lv_label_set_text(s_lbl_reply,
                          st.reply[0] ? st.reply : "按 BOOT 键向电脑服务器的AI提问");
        /* Dim it while the server is still generating, so "正在思考…" reads as
         * a state rather than as the answer. */
        lv_obj_set_style_text_color(s_lbl_reply,
                                    lv_color_hex(st.ai_pending ? 0x8b949e : 0xe3b341), 0);
    }
}

esp_err_t ui_init(void)
{
    bsp_display_lock(0);

    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x0d1117), 0);

    lv_obj_t *title = lv_label_create(scr);
    /* 周次不写进 UI：每周都要改一遍，已经漂移过两次（REVIEW P2-1）。
     * 周次只出现在 README 的进度行里。 */
    lv_label_set_text(title, "AI交互课 · Ego Link");
    lv_obj_set_style_text_font(title, CJK_FONT(), 0);
    lv_obj_set_style_text_color(title, lv_color_hex(0x8b949e), 0);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 4);

    s_lbl_status = make_label(scr, 0x7ee787, 26, SCREEN_W - 12, 20);
    lv_label_set_text(s_lbl_status, "启动中…");

    s_lbl_act = make_label(scr, 0x7ee787, 50, SCREEN_W - 12, 42);
    lv_label_set_text(s_lbl_act, "启动中…");

    s_lbl_data = make_label(scr, 0x79c0ff, 96, SCREEN_W - 12, 42);

    s_lbl_reply = make_label(scr, 0xe3b341, 142, SCREEN_W - 12, 90);
    lv_label_set_text(s_lbl_reply, "按 BOOT 键向电脑服务器的AI提问");

    lv_timer_create(ui_timer_cb, 500, NULL);

    bsp_display_unlock();
    ESP_LOGI(TAG, "ui ready");
    return ESP_OK;
}
