/*
 * SPDX-License-Identifier: MIT
 *
 * See ui.h.
 *
 * 240x240 图形化仪表盘（LVGL 9.6 / ESP32-S3-EYE）。
 * 取代原来「四个居中纯文本 label」的版本——那个版本没有图形、没有层级、没有动效，
 * 而且把服务器的整句活动文案（如「运动/步行 (峰值 1.2g, 约8步)」）直接塞进 240px 宽的
 * label 里被 LV_LABEL_LONG_DOT 截成「…」，现场看不出重点。
 *
 *   +--------------------------------------------+
 *   | * 在线   100Hz   ^1234    SC7A20 o0        | 状态胶囊
 *   +----------------------+---------------------+
 *   |        /-----\\       |     /-------\\       |
 *   |        | 静置 |       |     |   *   |       | 左：活动环（=|a| 量程 + 活动词）
 *   |        \\-----/       |     \\-------/       | 右：姿态球（重力方向 + 倾角）
 *   |         水平         |      倾角 12°        |
 *   +----------------------+---------------------+
 *   | X ===------   Y ==-----   Z =====-----     | 三轴对称条（±2g，从中点向两侧长）
 *   +--------------------------------------------+
 *   | [AI]                             (o)  1/2  |
 *   |  服务器回复（超长自动分页，每 4 秒翻一页）    |
 *   +--------------------------------------------+
 *
 * 设计要点
 * --------
 * 1. **图形优先**：活动不再是长句，而是「活动词 + 语义色 + 环」；
 *    服务器给的细节（水平 / 向左倾斜 / 峰值 / 约N步）另起一行小字，不再被截断。
 * 2. **颜色即语义**：静置绿 / 步行紫 / 运动蓝 / 晃动琥珀 / 跌落红。
 *    跌落时活动环外多一圈呼吸红光，隔着两米也看得见。
 * 3. **长回复不丢字**：AI 回复按显示宽度自动分页（每页 ≤ 30 列，中文记 2 列），
 *    每 4 秒翻一页，右下角显示 `1/2`，比原来的 `…` 截断多出完整内容。
 * 4. **动效克制**：只有三处动画——姿态球跟随（260ms）、跌落呼吸光、离线呼吸点。
 *    其余状态一律静态，避免 240x240 SPI 屏因持续重绘产生撕裂。
 * 5. **几何与配色集中在下面的 UI_* 宏**：`tools/ui_preview.py` 会**解析这些宏**
 *    渲染像素级预览图（docs/ui-preview 下的 png），所以改布局时预览图自动跟着变，
 *    不会出现「代码改了、预览图还是旧的」。
 *
 * 坐标系说明：st.x_g / st.y_g 是**屏幕坐标系**（+x 右、+y 下），与上报给服务器的一致
 * （见 accel_input_map_to_screen 与 transport.c）。姿态球直接按这两个值放点，
 * 不做任何二次翻转，保证「板子屏幕上看到的倾斜方向」与「网页仪表盘」永远一致。
 */
#include "ui.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>     /* strtof：从服务器文案里抠数值 */
#include <string.h>

#include "bsp/esp-bsp.h"
#include "esp_log.h"
#include "lvgl.h"

#include "transport.h"
#include "wifi_link.h"

#if CONFIG_LV_USE_TINY_TTF
#include "rw1_font.h"
#endif

static const char *TAG = "ui";

/* ==========================================================================
 * 布局常量（tools/ui_preview.py 解析这一段来生成预览图，改这里就够了）
 * ======================================================================== */
#define UI_SCR_W        240
#define UI_SCR_H        240

#define UI_MARGIN       6                       /* 左右留白 */

/* 状态胶囊 */
#define UI_STATUS_Y     4
#define UI_STATUS_H     22
#define UI_STATUS_X     UI_MARGIN
#define UI_STATUS_W     (UI_SCR_W - 2 * UI_MARGIN)
#define UI_DOT_X        14
#define UI_DOT_D        8
#define UI_LINK_X       26
#define UI_LINK_W       52
#define UI_HZ_X         82
#define UI_HZ_W         40
#define UI_POSTS_X      126
#define UI_POSTS_W      40
#define UI_INFO_X       166
#define UI_INFO_W       62
#define UI_BADGE_X      192
#define UI_BADGE_W      36
#define UI_BADGE_H      16

/* 两块主视觉面板 */
#define UI_PANEL_Y      30
#define UI_PANEL_H      88
#define UI_PANEL_W      108
#define UI_PANEL_LX     8
#define UI_PANEL_RX     124

/* 左面板：活动环 */
#define UI_RING_D       76
#define UI_RING_Y       0
#define UI_RING_W       7                       /* 环的线宽 */
#define UI_WORD_W       52
#define UI_WORD_H       18
#define UI_WORD_Y       21                      /* 环心文字（活动词） */
#define UI_ABS_W        52
#define UI_ABS_H        14
#define UI_ABS_Y        43                      /* 环心第二行（|a| 读数） */
#define UI_PANEL_CAP_Y  72                      /* 面板底部小字 */
#define UI_PANEL_CAP_W  100
#define UI_PANEL_CAP_H  14

/* 右面板：姿态球 */
#define UI_BALL_D       76
#define UI_BALL_Y       0
#define UI_BALL_W       1                       /* 外圈线宽 */
#define UI_BUBBLE_D     18                      /* 重力球直径 */
#define UI_BUBBLE_MAX   26                      /* 球心最大偏移（px），对应 1g */
#define UI_LEVEL_D      22                      /* 中心「水平区」参考圈 */
#define UI_CROSS_LEN    52

/* 三轴对称条 */
#define UI_AXIS_Y       124
#define UI_AXIS_ROW_H   11
#define UI_AXIS_GAP     3
#define UI_AXIS_LBL_X   10
#define UI_AXIS_LBL_W   12
#define UI_AXIS_BAR_X   26
#define UI_AXIS_BAR_W   158
#define UI_AXIS_BAR_H   7
#define UI_AXIS_VAL_X   190
#define UI_AXIS_VAL_W   42

/* AI 回复卡片 */
#define UI_CARD_Y       168
#define UI_CARD_H       66
#define UI_CARD_X       UI_MARGIN
#define UI_CARD_W       (UI_SCR_W - 2 * UI_MARGIN)
#define UI_TAG_X        14
#define UI_TAG_Y        5                       /* 相对卡片 */
#define UI_TAG_W        26
#define UI_TAG_H        15
#define UI_SPIN_X       168
#define UI_SPIN_Y       5
#define UI_SPIN_D       14
#define UI_PAGE_X       192
#define UI_PAGE_W       32
#define UI_REPLY_X      14
#define UI_REPLY_Y      22                      /* 相对卡片 */
#define UI_REPLY_W      200
#define UI_REPLY_H      40

/* ==========================================================================
 * 配色（深色玻璃拟态：深底 + 低对比描边 + 高饱和语义色）
 * ======================================================================== */
#define UI_C_BG         0x0A0E14                /* 屏幕底色（顶亮底暗渐变） */
#define UI_C_BG_DEEP    0x05070A
#define UI_C_CARD       0x141A23                /* 面板/卡片底 */
#define UI_C_CARD_HI    0x1B2230                /* 卡片渐变上端 */
#define UI_C_LINE       0x252D3A                /* 描边 */
#define UI_C_TRACK      0x1E2530                /* 进度/环的底槽 */
#define UI_C_TEXT       0xE6EDF3
#define UI_C_DIM        0x8B949E
#define UI_C_FAINT      0x5A6472
#define UI_C_GREEN      0x3FB950                /* 静置 / 在线 */
#define UI_C_TEAL       0x2DD4BF
#define UI_C_BLUE       0x58A6FF                /* 运动 / 信息 */
#define UI_C_AMBER      0xE3B341                /* 晃动 / AI 回复 */
#define UI_C_RED        0xF85149                /* 跌落 / 失败 */
#define UI_C_PURPLE     0xA371F7                /* 步行 */
#define UI_C_AXIS_X     0xF85149
#define UI_C_AXIS_Y     0x3FB950
#define UI_C_AXIS_Z     0x58A6FF

/* 活动环量程：|a| 0..2g 映射到 0..100 */
#define UI_ABS_FULL_G   2.0f
/* 姿态球偏移换算：1g 对应 UI_BUBBLE_MAX 像素 */
#define UI_BUBBLE_SCALE ((float)UI_BUBBLE_MAX)
/* 三轴条量程：±2g（整数，LVGL bar 只吃 int32） */
#define UI_AXIS_FULL    200
/* AI 回复每页列数（中文按 2 列算）。
 * 卡片正文区 200px 宽、14px 字号、2 行 → 每行约 28 列，两行 56 列；
 * 留一点余量给行末的 `…`，取 54。这个值是 tools/ui_preview.py 验证过的：
 * 30 列时每页只有一行文字、卡片第二行白白空着。 */
#define UI_REPLY_COLS   54
/* 每页停留的刷新周期数（UI 定时器 500ms → 8 拍 = 4 秒） */
#define UI_PAGE_TICKS   8

/* ==========================================================================
 * 控件句柄
 * ======================================================================== */
static lv_obj_t *s_dot;
static lv_obj_t *s_lbl_link;
static lv_obj_t *s_lbl_hz;
static lv_obj_t *s_lbl_posts;
static lv_obj_t *s_lbl_info;
static lv_obj_t *s_badge;
static lv_obj_t *s_lbl_badge;

static lv_obj_t *s_glow;                        /* 跌落告警的呼吸光环 */
static lv_obj_t *s_arc;                         /* 活动环 */
static lv_obj_t *s_lbl_word;                    /* 活动词（静置/步行/…） */
static lv_obj_t *s_lbl_abs;                     /* |a| 读数 */
static lv_obj_t *s_lbl_detail;                  /* 活动细节（水平/向左/约8步） */

static lv_obj_t *s_bubble;                      /* 姿态球里的重力球 */
static lv_obj_t *s_lbl_tilt;                    /* 倾角 */

static lv_obj_t *s_axis_bar[3];
static lv_obj_t *s_axis_val[3];

static lv_obj_t *s_lbl_reply;
static lv_obj_t *s_lbl_page;
static lv_obj_t *s_spinner;

/* 只在值变化时才动控件，避免 500ms 定时器把动画一次次重置 */
#define UI_NONE (-1000000)

static char     s_last_reply[TRANSPORT_REPLY_LEN];
static char     s_last_activity[TRANSPORT_ACTIVITY_LEN];
static char     s_last_info[40];
static bool     s_last_pending;
static int      s_last_abs = -1;
static int      s_last_axis[3] = {UI_NONE, UI_NONE, UI_NONE};
static int      s_last_badge = -1;
static bool     s_last_online = true;
static bool     s_fall_on;
static int      s_bubble_tx = UI_NONE, s_bubble_ty = UI_NONE;
static uint32_t s_last_ball_color;
static int      s_page;                         /* 当前回复页（0 起） */
static int      s_page_total = 1;
static int      s_page_tick;

/* ==========================================================================
 * 字体
 * ======================================================================== */
/* LVGL 自带的 CJK 演示字体只含少量随机字，所以用 tools/gen_font.py 裁出 SimHei 子集，
 * 交给 tiny_ttf 运行时栅格化。三档字号（活动词 18 / 正文 14 / 小字 12）建立视觉层级。
 * 必须在 LVGL 锁内创建（ui_init 持锁）。 */
static lv_font_t *s_font[3];

static const lv_font_t *cjk_font(int size)
{
#if CONFIG_LV_USE_TINY_TTF
    int idx = (size >= 18) ? 0 : (size >= 14) ? 1 : 2;
    static const int px[3] = {18, 14, 12};
    if (s_font[idx] == NULL) {
        s_font[idx] = lv_tiny_ttf_create_data(rw1_font_ttf, rw1_font_ttf_len, px[idx]);
        if (s_font[idx] == NULL) {
            ESP_LOGE(TAG, "tiny_ttf %dpx create failed", px[idx]);
        }
    }
    if (s_font[idx] != NULL) {
        return s_font[idx];
    }
#else
    (void)size;
#endif
    return &lv_font_montserrat_14;
}

/* ==========================================================================
 * 小工具
 * ======================================================================== */
static lv_obj_t *make_box(lv_obj_t *parent, int x, int y, int w, int h,
                          uint32_t bg, uint32_t border, int radius)
{
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_set_scrollable(o, false);
    lv_obj_set_clickable(o, false);
    lv_obj_set_size(o, w, h);
    lv_obj_set_pos(o, x, y);
    lv_obj_set_style_radius(o, radius, 0);
    lv_obj_set_style_pad_all(o, 0, 0);
    lv_obj_set_style_border_width(o, (border == 0) ? 0 : 1, 0);
    if (border != 0) {
        lv_obj_set_style_border_color(o, lv_color_hex(border), 0);
    }
    if (bg == 0) {
        lv_obj_set_style_bg_opa(o, LV_OPA_TRANSP, 0);
    } else {
        lv_obj_set_style_bg_color(o, lv_color_hex(bg), 0);
        lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
    }
    return o;
}

static lv_obj_t *make_label(lv_obj_t *parent, int x, int y, int w, int h,
                            const lv_font_t *font, uint32_t color, lv_text_align_t align)
{
    lv_obj_t *lbl = lv_label_create(parent);
    lv_obj_set_style_text_font(lbl, font, 0);
    lv_obj_set_style_text_color(lbl, lv_color_hex(color), 0);
    lv_obj_set_style_text_align(lbl, align, 0);
    lv_obj_set_style_pad_all(lbl, 0, 0);
    lv_obj_set_size(lbl, w, h);
    lv_label_set_long_mode(lbl, LV_LABEL_LONG_DOT);
    lv_obj_set_pos(lbl, x, y);
    return lbl;
}

/* 从服务器文案里抠一个数字，例如「峰值 1.2g」「约8步」。找不到返回 false。 */
static bool pick_number(const char *text, const char *key, float *out)
{
    const char *p = strstr(text, key);
    if (p == NULL) {
        return false;
    }
    p += strlen(key);
    /* 跳过 key 与数字之间的空白与冒号。注意全角「：」是 3 字节 UTF-8（EF BC 9A），
     * **不能写成字符常量** —— '：' 在多字节源码里会变成多字节常量，比较恒为假
     * （GCC 会同时报 -Wmultichar 和「比较恒为假」两条警告）。 */
    for (;;) {
        if (*p == ' ' || *p == ':' || *p == '\t') {
            p++;
        } else if ((unsigned char)p[0] == 0xEF && (unsigned char)p[1] == 0xBC &&
                   (unsigned char)p[2] == 0x9A) {
            p += 3;
        } else {
            break;
        }
    }
    char *end = NULL;
    float v = strtof(p, &end);
    if (end == p) {
        return false;
    }
    *out = v;
    return true;
}

/* --------------------------------------------------------------------------
 * 活动分类：把服务器那句长文案压成「活动词 + 语义色 + 一行细节」
 * ------------------------------------------------------------------------ */
typedef enum {
    ACT_IDLE = 0,       /* 还没拿到服务器结论 */
    ACT_STILL,
    ACT_MOVE,
    ACT_WALK,
    ACT_SHAKE,
    ACT_FALL,
    ACT_COUNT
} act_kind_t;

static const uint32_t ACT_COLOR[ACT_COUNT] = {
    UI_C_FAINT, UI_C_GREEN, UI_C_BLUE, UI_C_PURPLE, UI_C_AMBER, UI_C_RED
};
static const char *const ACT_WORD[ACT_COUNT] = {
    "等待", "静置", "运动", "步行", "晃动", "跌落"
};

static act_kind_t classify(const char *activity)
{
    if (activity == NULL || activity[0] == '\0') {
        return ACT_IDLE;
    }
    /* 顺序有讲究：先判最紧急的，再判方向性的 */
    if (strstr(activity, "跌落") || strstr(activity, "失重")) {
        return ACT_FALL;
    }
    if (strstr(activity, "晃动")) {
        return ACT_SHAKE;
    }
    if (strstr(activity, "步行")) {
        return ACT_WALK;
    }
    if (strstr(activity, "运动")) {
        return ACT_MOVE;
    }
    if (strstr(activity, "静置")) {
        return ACT_STILL;
    }
    return ACT_IDLE;
}

/* 细节行：静置给方向，运动/步行给峰值与步数，其余给一句短的。
 * 全部是**从服务器文案里解析出来的真实数值**，不是另编的。 */
static void build_detail(act_kind_t kind, const char *activity, char *out, size_t out_sz)
{
    float peak = 0.0f;
    float steps = 0.0f;

    switch (kind) {
    case ACT_FALL:
        snprintf(out, out_sz, "失重告警");
        return;
    case ACT_SHAKE:
        snprintf(out, out_sz, "剧烈晃动");
        return;
    case ACT_STILL:
        if (strstr(activity, "向左") || strstr(activity, "左")) {
            snprintf(out, out_sz, "向左倾斜");
        } else if (strstr(activity, "向右") || strstr(activity, "右")) {
            snprintf(out, out_sz, "向右倾斜");
        } else if (strstr(activity, "向上") || strstr(activity, "上")) {
            snprintf(out, out_sz, "向上倾斜");
        } else if (strstr(activity, "向下") || strstr(activity, "下")) {
            snprintf(out, out_sz, "向下倾斜");
        } else {
            snprintf(out, out_sz, "水平");
        }
        return;
    case ACT_WALK:
        if (pick_number(activity, "约", &steps)) {
            snprintf(out, out_sz, "约 %d 步", (int)(steps + 0.5f));
        } else {
            snprintf(out, out_sz, "计步中");
        }
        return;
    case ACT_MOVE:
        if (pick_number(activity, "峰值", &peak)) {
            snprintf(out, out_sz, "峰值 %.1fg", peak);
        } else {
            snprintf(out, out_sz, "运动中");
        }
        return;
    default:
        snprintf(out, out_sz, "暂无数据");
        return;
    }
}

/* --------------------------------------------------------------------------
 * AI 回复分页：按显示宽度切（中文 2 列、ASCII 1 列），优先在空格/句读处断
 * ------------------------------------------------------------------------ */
static int reply_page(const char *src, int page, char *out, size_t out_sz)
{
    if (out_sz == 0) {
        return 1;
    }
    out[0] = '\0';
    if (src == NULL || src[0] == '\0') {
        return 1;
    }

    const char *p = src;
    int total = 0;

    for (int idx = 0; *p != '\0'; idx++) {
        const char *start = p;
        const char *brk = NULL;
        int cols = 0;

        while (*p != '\0' && cols < UI_REPLY_COLS) {
            unsigned char c = (unsigned char)*p;
            int len = 1;
            if (c >= 0xF0) {
                len = 4;
            } else if (c >= 0xE0) {
                len = 3;
            } else if (c >= 0xC0) {
                len = 2;
            }
            int width = (len == 1) ? 1 : 2;
            if (cols + width > UI_REPLY_COLS) {
                break;
            }
            cols += width;
            if (len == 1 && (*p == ' ' || *p == ',' || *p == '.' || *p == ';')) {
                brk = p + 1;        /* 记住最后一个可断处，回头再决定用不用 */
            }
            p += len;
        }

        /* 已经吃到半页以上、后面还有内容时，退到更自然的断点 */
        if (brk != NULL && brk > start + 10 && *p != '\0') {
            p = brk;
        }

        /* 防御：一页至少要吃掉一个字符，否则死循环 */
        if (p == start) {
            p += 1;
        }

        if (idx == page) {
            size_t n = (size_t)(p - start);
            if (n >= out_sz) {
                n = out_sz - 1;
            }
            memcpy(out, start, n);
            out[n] = '\0';
        }
        /* 不提前 break：这里要把总页数数完，调用方才知道右下角该写 1/N 还是留空 */
        total = idx + 1;
    }
    return (total > 0) ? total : 1;
}

/* --------------------------------------------------------------------------
 * 动画回调（LVGL 的 exec_cb 签名固定为 void(*)(void*, int32_t)）
 * ------------------------------------------------------------------------ */
static void anim_translate_x(void *obj, int32_t v)
{
    lv_obj_set_style_translate_x((lv_obj_t *)obj, v, 0);
}

static void anim_translate_y(void *obj, int32_t v)
{
    lv_obj_set_style_translate_y((lv_obj_t *)obj, v, 0);
}

static void anim_opa(void *obj, int32_t v)
{
    lv_obj_set_style_opa((lv_obj_t *)obj, (lv_opa_t)v, 0);
}

/* 跌落告警：环外那圈红光呼吸。只在状态翻转时起停，不每帧重启动画。 */
static void set_fall_alert(bool on)
{
    if (on == s_fall_on) {
        return;
    }
    s_fall_on = on;

    if (on) {
        lv_obj_set_hidden(s_glow, false);
        lv_anim_t a;
        lv_anim_init(&a);
        lv_anim_set_var(&a, s_glow);
        lv_anim_set_values(&a, 30, 230);
        lv_anim_set_duration(&a, 700);
        lv_anim_set_reverse_duration(&a, 700);
        lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
        lv_anim_set_exec_cb(&a, anim_opa);
        lv_anim_start(&a);
    } else {
        lv_anim_delete(s_glow, anim_opa);
        lv_obj_set_hidden(s_glow, true);
    }
}

/* 离线时状态点呼吸（提醒「链路断了」），在线时保持常亮，不做无谓重绘。 */
static void set_offline_pulse(bool on)
{
    if (on) {
        lv_anim_t a;
        lv_anim_init(&a);
        lv_anim_set_var(&a, s_dot);
        lv_anim_set_values(&a, 60, 255);
        lv_anim_set_duration(&a, 900);
        lv_anim_set_reverse_duration(&a, 900);
        lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
        lv_anim_set_exec_cb(&a, anim_opa);
        lv_anim_start(&a);
    } else {
        lv_anim_delete(s_dot, anim_opa);
        lv_obj_set_style_opa(s_dot, LV_OPA_COVER, 0);
    }
}

/* 姿态球平滑跟随：把生硬的 2Hz 跳变变成 260ms 的滑动 */
static void move_bubble(int tx, int ty)
{
    if (tx == s_bubble_tx && ty == s_bubble_ty) {
        return;
    }

    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, s_bubble);
    lv_anim_set_duration(&a, 260);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
    lv_anim_set_exec_cb(&a, anim_translate_x);
    lv_anim_set_values(&a, lv_obj_get_style_translate_x(s_bubble, 0), tx);
    lv_anim_start(&a);

    lv_anim_init(&a);
    lv_anim_set_var(&a, s_bubble);
    lv_anim_set_duration(&a, 260);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
    lv_anim_set_exec_cb(&a, anim_translate_y);
    lv_anim_set_values(&a, lv_obj_get_style_translate_y(s_bubble, 0), ty);
    lv_anim_start(&a);

    s_bubble_tx = tx;
    s_bubble_ty = ty;
}

/* ==========================================================================
 * 定时刷新（跑在显示任务上下文里，可以安全碰 LVGL）
 * ======================================================================== */
static void ui_timer_cb(lv_timer_t *timer)
{
    (void)timer;

    transport_status_t st;
    transport_get_status(&st);

    char buf[80];
    bool wifi_up = wifi_link_is_up();
    bool online = wifi_up && st.server_ok;

    /* ---------- 状态胶囊：链路 ---------- */
    const char *link_text;
    uint32_t link_color;
    if (!wifi_up) {
        link_text = "连WiFi";
        link_color = UI_C_AMBER;
    } else if (st.server_ok) {
        link_text = "在线";
        link_color = UI_C_GREEN;
    } else {
        link_text = "无服务";
        link_color = UI_C_RED;
    }
    lv_label_set_text(s_lbl_link, link_text);
    lv_obj_set_style_text_color(s_lbl_link, lv_color_hex(link_color), 0);
    lv_obj_set_style_bg_color(s_dot, lv_color_hex(link_color), 0);

    if (online != s_last_online) {
        s_last_online = online;
        /* 断了才呼吸（提醒现场），通了就常亮——不做无意义的重绘 */
        set_offline_pulse(!online);
    }

    /* ---------- 状态胶囊：采样率 / 上报数 / 芯片与校准档 ---------- */
    snprintf(buf, sizeof(buf), "%dHz", (int)(1000 / CONFIG_RW1_SAMPLE_PERIOD_MS));
    lv_label_set_text(s_lbl_hz, buf);

    snprintf(buf, sizeof(buf), "↑%u", (unsigned)st.posts_ok);
    lv_label_set_text(s_lbl_posts, buf);

    snprintf(buf, sizeof(buf), "%s o%u", st.source[0] ? st.source : "—",
             (unsigned)st.orient);
    if (strcmp(buf, s_last_info) != 0) {
        strlcpy(s_last_info, buf, sizeof(s_last_info));
        lv_label_set_text(s_lbl_info, buf);
    }

    /* ---------- 远程指令徽标（占住 info 那一格，两者互斥） ---------- */
    int badge = (st.cmd_state <= TRANSPORT_CMD_FAILED) ? (int)st.cmd_state : 0;
    if (badge != s_last_badge) {
        s_last_badge = badge;
        static const char *const badge_text[4] = {"", "···", "OK", "NG"};
        static const uint32_t badge_color[4] = {0, UI_C_AMBER, UI_C_GREEN, UI_C_RED};

        if (badge == TRANSPORT_CMD_IDLE) {
            lv_obj_set_hidden(s_badge, true);
            lv_obj_set_hidden(s_lbl_info, false);
        } else {
            lv_label_set_text(s_lbl_badge, badge_text[badge]);
            lv_obj_set_style_bg_color(s_badge, lv_color_hex(badge_color[badge]), 0);
            lv_obj_set_hidden(s_badge, false);
            lv_obj_set_hidden(s_lbl_info, true);
        }
    }

    /* ---------- 活动环：活动词 + 语义色 + 细节行 ---------- */
    if (strcmp(st.activity, s_last_activity) != 0) {
        strlcpy(s_last_activity, st.activity, sizeof(s_last_activity));

        act_kind_t kind = classify(st.activity);
        uint32_t act_color = ACT_COLOR[kind];

        lv_label_set_text(s_lbl_word, ACT_WORD[kind]);
        lv_obj_set_style_text_color(s_lbl_word, lv_color_hex(act_color), 0);
        lv_obj_set_style_arc_color(s_arc, lv_color_hex(act_color), LV_PART_INDICATOR);

        char detail[32];
        build_detail(kind, st.activity, detail, sizeof(detail));
        lv_label_set_text(s_lbl_detail, detail);

        set_fall_alert(kind == ACT_FALL);
    }

    /* ---------- 活动环数值：|a| 映射到 0..100 ---------- */
    float mag = sqrtf(st.x_g * st.x_g + st.y_g * st.y_g + st.z_g * st.z_g);
    int abs_v = (int)(mag / UI_ABS_FULL_G * 100.0f + 0.5f);
    if (abs_v > 100) {
        abs_v = 100;
    }
    if (abs_v != s_last_abs) {
        s_last_abs = abs_v;
        lv_arc_set_value(s_arc, abs_v);
        snprintf(buf, sizeof(buf), "%.2fg", mag);
        lv_label_set_text(s_lbl_abs, buf);
    }

    /* ---------- 姿态球：直接用屏幕坐标系的 x/y，不做二次翻转 ---------- */
    float bx = st.x_g * UI_BUBBLE_SCALE;
    float by = st.y_g * UI_BUBBLE_SCALE;
    if (bx > UI_BUBBLE_MAX) {
        bx = UI_BUBBLE_MAX;
    } else if (bx < -UI_BUBBLE_MAX) {
        bx = -UI_BUBBLE_MAX;
    }
    if (by > UI_BUBBLE_MAX) {
        by = UI_BUBBLE_MAX;
    } else if (by < -UI_BUBBLE_MAX) {
        by = -UI_BUBBLE_MAX;
    }
    move_bubble((int)bx, (int)by);

    /* 球色跟着倾斜量走：水平绿 / 倾斜琥珀 / 大角度红。
     * mag < 0.05g 是「还没拿到有效读数」（开机第一帧、断链时 x/y/z 都是 0），
     * 这时候既不是失重也不该亮红——用暗灰，别让姿态球红着喊失重误导现场。 */
    float horiz = sqrtf(st.x_g * st.x_g + st.y_g * st.y_g);
    uint32_t ball_color;
    if (mag < 0.05f) {
        ball_color = UI_C_FAINT;
    } else if (mag < 0.35f) {
        ball_color = UI_C_RED;
    } else if (horiz < 0.15f) {
        ball_color = UI_C_GREEN;
    } else if (horiz < 0.7f) {
        ball_color = UI_C_AMBER;
    } else {
        ball_color = UI_C_RED;
    }
    if (ball_color != s_last_ball_color) {
        s_last_ball_color = ball_color;
        lv_obj_set_style_bg_color(s_bubble, lv_color_hex(ball_color), 0);
        lv_obj_set_style_shadow_color(s_bubble, lv_color_hex(ball_color), 0);
    }

    if (mag < 0.05f) {
        lv_label_set_text(s_lbl_tilt, "无读数");
    } else if (mag < 0.35f) {
        lv_label_set_text(s_lbl_tilt, "失重");
    } else {
        float c = fabsf(st.z_g) / mag;
        if (c > 1.0f) {
            c = 1.0f;
        }
        int deg = (int)(acosf(c) * 57.29578f + 0.5f);
        snprintf(buf, sizeof(buf), "倾角 %d°", deg);
        lv_label_set_text(s_lbl_tilt, buf);
    }

    /* ---------- 三轴对称条 ---------- */
    float axis[3] = {st.x_g, st.y_g, st.z_g};
    for (int i = 0; i < 3; i++) {
        int iv = (int)(axis[i] * 100.0f);
        if (iv > UI_AXIS_FULL) {
            iv = UI_AXIS_FULL;
        } else if (iv < -UI_AXIS_FULL) {
            iv = -UI_AXIS_FULL;
        }
        if (iv != s_last_axis[i]) {
            s_last_axis[i] = iv;
            lv_bar_set_value(s_axis_bar[i], iv, LV_ANIM_OFF);
            snprintf(buf, sizeof(buf), "%+.2f", axis[i]);
            lv_label_set_text(s_axis_val[i], buf);
        }
    }

    /* ---------- AI 回复：新内容就重排分页，否则每 4 秒翻一页 ---------- */
    if (strcmp(st.reply, s_last_reply) != 0 || st.ai_pending != s_last_pending) {
        strlcpy(s_last_reply, st.reply, sizeof(s_last_reply));
        s_last_pending = st.ai_pending;
        s_page = 0;
        s_page_tick = 0;

        const char *text = st.reply[0] ? st.reply : "按 BOOT 键向电脑服务器的 AI 提问";
        char pagebuf[128];
        s_page_total = reply_page(text, 0, pagebuf, sizeof(pagebuf));
        lv_label_set_text(s_lbl_reply, pagebuf);
        lv_obj_set_style_text_color(s_lbl_reply,
                                    lv_color_hex(st.ai_pending ? UI_C_DIM : UI_C_AMBER), 0);

        if (st.ai_pending) {
            /* 生成中：转圈表示「服务端在忙」，同时正文压暗，避免被当成最终答案 */
            lv_obj_set_hidden(s_spinner, false);
            lv_label_set_text(s_lbl_page, "");
        } else {
            lv_obj_set_hidden(s_spinner, true);
            if (s_page_total > 1) {
                snprintf(buf, sizeof(buf), "1/%d", s_page_total);
            } else {
                buf[0] = '\0';
            }
            lv_label_set_text(s_lbl_page, buf);
        }
    } else if (!st.ai_pending && s_page_total > 1) {
        if (++s_page_tick >= UI_PAGE_TICKS) {
            s_page_tick = 0;
            s_page = (s_page + 1) % s_page_total;

            char pagebuf[128];
            reply_page(st.reply, s_page, pagebuf, sizeof(pagebuf));
            lv_label_set_text(s_lbl_reply, pagebuf);
            snprintf(buf, sizeof(buf), "%d/%d", s_page + 1, s_page_total);
            lv_label_set_text(s_lbl_page, buf);
        }
    }
}

/* ==========================================================================
 * 界面构建
 * ======================================================================== */
static void build_status_bar(lv_obj_t *scr)
{
    lv_obj_t *bar = make_box(scr, UI_STATUS_X, UI_STATUS_Y, UI_STATUS_W, UI_STATUS_H,
                             UI_C_CARD, UI_C_LINE, UI_STATUS_H / 2);

    s_dot = make_box(bar, UI_DOT_X - UI_STATUS_X, (UI_STATUS_H - UI_DOT_D) / 2,
                     UI_DOT_D, UI_DOT_D, UI_C_GREEN, 0, LV_RADIUS_CIRCLE);

    s_lbl_link = make_label(bar, UI_LINK_X - UI_STATUS_X, (UI_STATUS_H - 14) / 2,
                            UI_LINK_W, 14, cjk_font(12), UI_C_GREEN, LV_TEXT_ALIGN_LEFT);
    lv_label_set_text(s_lbl_link, "启动中");

    s_lbl_hz = make_label(bar, UI_HZ_X - UI_STATUS_X, (UI_STATUS_H - 14) / 2,
                          UI_HZ_W, 14, &lv_font_montserrat_14, UI_C_DIM, LV_TEXT_ALIGN_CENTER);
    lv_label_set_text(s_lbl_hz, "—");

    s_lbl_posts = make_label(bar, UI_POSTS_X - UI_STATUS_X, (UI_STATUS_H - 14) / 2,
                             UI_POSTS_W, 14, &lv_font_montserrat_14, UI_C_DIM,
                             LV_TEXT_ALIGN_CENTER);
    lv_label_set_text(s_lbl_posts, "↑0");

    s_lbl_info = make_label(bar, UI_INFO_X - UI_STATUS_X, (UI_STATUS_H - 12) / 2,
                            UI_INFO_W, 12, cjk_font(12), UI_C_FAINT, LV_TEXT_ALIGN_RIGHT);
    lv_label_set_text(s_lbl_info, "—");

    s_badge = make_box(bar, UI_BADGE_X - UI_STATUS_X, (UI_STATUS_H - UI_BADGE_H) / 2,
                       UI_BADGE_W, UI_BADGE_H, UI_C_AMBER, 0, UI_BADGE_H / 2);
    lv_obj_set_hidden(s_badge, true);
    s_lbl_badge = make_label(s_badge, 0, 1, UI_BADGE_W, UI_BADGE_H - 2,
                             &lv_font_montserrat_14, 0x0A0E14, LV_TEXT_ALIGN_CENTER);
    lv_label_set_text(s_lbl_badge, "···");
}

static void build_activity_panel(lv_obj_t *scr)
{
    lv_obj_t *p = make_box(scr, UI_PANEL_LX, UI_PANEL_Y, UI_PANEL_W, UI_PANEL_H,
                           UI_C_CARD, UI_C_LINE, 16);

    /* 跌落呼吸光环：比活动环大一圈，平时隐藏 */
    s_glow = make_box(p, (UI_PANEL_W - (UI_RING_D + 14)) / 2, UI_RING_Y - 7,
                      UI_RING_D + 14, UI_RING_D + 14, 0, UI_C_RED, LV_RADIUS_CIRCLE);
    lv_obj_set_style_border_width(s_glow, 2, 0);
    lv_obj_set_style_opa(s_glow, LV_OPA_TRANSP, 0);
    lv_obj_set_hidden(s_glow, true);

    /* 活动环：270° 仪表，量程 |a| 0..2g */
    s_arc = lv_arc_create(p);
    lv_obj_set_scrollable(s_arc, false);
    lv_obj_set_clickable(s_arc, false);
    lv_obj_remove_style(s_arc, NULL, LV_PART_KNOB);
    lv_obj_set_size(s_arc, UI_RING_D, UI_RING_D);
    lv_obj_align(s_arc, LV_ALIGN_TOP_MID, 0, UI_RING_Y);
    lv_arc_set_rotation(s_arc, 0);
    lv_arc_set_bg_angles(s_arc, 135, 405);      /* 底部留 90° 缺口放小字 */
    lv_arc_set_range(s_arc, 0, 100);
    lv_arc_set_value(s_arc, 0);
    lv_obj_set_style_bg_opa(s_arc, LV_OPA_TRANSP, LV_PART_MAIN);
    lv_obj_set_style_arc_width(s_arc, UI_RING_W, LV_PART_MAIN);
    lv_obj_set_style_arc_color(s_arc, lv_color_hex(UI_C_TRACK), LV_PART_MAIN);
    lv_obj_set_style_arc_rounded(s_arc, true, LV_PART_MAIN);
    lv_obj_set_style_arc_width(s_arc, UI_RING_W, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(s_arc, lv_color_hex(UI_C_GREEN), LV_PART_INDICATOR);
    lv_obj_set_style_arc_rounded(s_arc, true, LV_PART_INDICATOR);

    s_lbl_word = make_label(p, (UI_PANEL_W - UI_WORD_W) / 2, UI_WORD_Y, UI_WORD_W, UI_WORD_H,
                            cjk_font(18), UI_C_GREEN, LV_TEXT_ALIGN_CENTER);
    lv_label_set_long_mode(s_lbl_word, LV_LABEL_LONG_CLIP);
    lv_label_set_text(s_lbl_word, "等待");

    s_lbl_abs = make_label(p, (UI_PANEL_W - UI_ABS_W) / 2, UI_ABS_Y, UI_ABS_W, UI_ABS_H,
                           &lv_font_montserrat_14, UI_C_DIM, LV_TEXT_ALIGN_CENTER);
    lv_label_set_long_mode(s_lbl_abs, LV_LABEL_LONG_CLIP);
    lv_label_set_text(s_lbl_abs, "0.00g");

    s_lbl_detail = make_label(p, (UI_PANEL_W - UI_PANEL_CAP_W) / 2, UI_PANEL_CAP_Y,
                              UI_PANEL_CAP_W, UI_PANEL_CAP_H, cjk_font(12), UI_C_DIM,
                              LV_TEXT_ALIGN_CENTER);
    lv_label_set_text(s_lbl_detail, "暂无数据");
}

static void build_tilt_panel(lv_obj_t *scr)
{
    lv_obj_t *p = make_box(scr, UI_PANEL_RX, UI_PANEL_Y, UI_PANEL_W, UI_PANEL_H,
                           UI_C_CARD, UI_C_LINE, 16);

    /* 圆盘 + 十字准线 + 中心「水平区」参考圈：让球的位移有参照 */
    lv_obj_t *disc = make_box(p, (UI_PANEL_W - UI_BALL_D) / 2, UI_BALL_Y,
                              UI_BALL_D, UI_BALL_D, UI_C_BG, UI_C_LINE, LV_RADIUS_CIRCLE);
    lv_obj_set_style_border_width(disc, UI_BALL_W, 0);

    int cx = UI_BALL_D / 2;
    make_box(disc, cx, (UI_BALL_D - UI_CROSS_LEN) / 2, 1, UI_CROSS_LEN, UI_C_LINE, 0, 0);
    make_box(disc, (UI_BALL_D - UI_CROSS_LEN) / 2, cx, UI_CROSS_LEN, 1, UI_C_LINE, 0, 0);
    make_box(disc, cx - UI_LEVEL_D / 2, cx - UI_LEVEL_D / 2, UI_LEVEL_D, UI_LEVEL_D,
             0, UI_C_TRACK, LV_RADIUS_CIRCLE);

    s_bubble = make_box(disc, cx - UI_BUBBLE_D / 2, cx - UI_BUBBLE_D / 2,
                        UI_BUBBLE_D, UI_BUBBLE_D, UI_C_GREEN, 0, LV_RADIUS_CIRCLE);
    lv_obj_set_style_shadow_width(s_bubble, 10, 0);
    lv_obj_set_style_shadow_color(s_bubble, lv_color_hex(UI_C_GREEN), 0);
    lv_obj_set_style_shadow_opa(s_bubble, 90, 0);

    s_lbl_tilt = make_label(p, (UI_PANEL_W - UI_PANEL_CAP_W) / 2, UI_PANEL_CAP_Y,
                            UI_PANEL_CAP_W, UI_PANEL_CAP_H, cjk_font(12), UI_C_DIM,
                            LV_TEXT_ALIGN_CENTER);
    lv_label_set_text(s_lbl_tilt, "倾角 —");
}

static void build_axis_rows(lv_obj_t *scr)
{
    static const uint32_t axis_color[3] = {UI_C_AXIS_X, UI_C_AXIS_Y, UI_C_AXIS_Z};
    static const char *const axis_name[3] = {"X", "Y", "Z"};

    for (int i = 0; i < 3; i++) {
        int y = UI_AXIS_Y + i * (UI_AXIS_ROW_H + UI_AXIS_GAP);

        lv_obj_t *lbl = make_label(scr, UI_AXIS_LBL_X, y, UI_AXIS_LBL_W, UI_AXIS_ROW_H,
                                   &lv_font_montserrat_14, axis_color[i], LV_TEXT_ALIGN_LEFT);
        lv_label_set_long_mode(lbl, LV_LABEL_LONG_CLIP);
        lv_label_set_text(lbl, axis_name[i]);

        /* 对称条：0 在正中，往两侧长，正负一眼分得开 */
        s_axis_bar[i] = lv_bar_create(scr);
        lv_obj_set_scrollable(s_axis_bar[i], false);
        lv_obj_set_clickable(s_axis_bar[i], false);
        lv_obj_set_size(s_axis_bar[i], UI_AXIS_BAR_W, UI_AXIS_BAR_H);
        lv_obj_set_pos(s_axis_bar[i], UI_AXIS_BAR_X,
                       y + (UI_AXIS_ROW_H - UI_AXIS_BAR_H) / 2);
        lv_bar_set_range(s_axis_bar[i], -UI_AXIS_FULL, UI_AXIS_FULL);
        lv_bar_set_mode(s_axis_bar[i], LV_BAR_MODE_SYMMETRICAL);
        lv_bar_set_value(s_axis_bar[i], 0, LV_ANIM_OFF);
        lv_obj_set_style_radius(s_axis_bar[i], UI_AXIS_BAR_H / 2, LV_PART_MAIN);
        lv_obj_set_style_bg_color(s_axis_bar[i], lv_color_hex(UI_C_TRACK), LV_PART_MAIN);
        lv_obj_set_style_bg_opa(s_axis_bar[i], LV_OPA_COVER, LV_PART_MAIN);
        lv_obj_set_style_radius(s_axis_bar[i], UI_AXIS_BAR_H / 2, LV_PART_INDICATOR);
        lv_obj_set_style_bg_color(s_axis_bar[i], lv_color_hex(axis_color[i]), LV_PART_INDICATOR);
        lv_obj_set_style_bg_grad_color(s_axis_bar[i],
                                       lv_color_hex(axis_color[i] & 0x7F7F7F), LV_PART_INDICATOR);
        lv_obj_set_style_bg_grad_dir(s_axis_bar[i], LV_GRAD_DIR_HOR, LV_PART_INDICATOR);

        s_axis_val[i] = make_label(scr, UI_AXIS_VAL_X, y, UI_AXIS_VAL_W, UI_AXIS_ROW_H,
                                   &lv_font_montserrat_14, UI_C_DIM, LV_TEXT_ALIGN_RIGHT);
        lv_label_set_long_mode(s_axis_val[i], LV_LABEL_LONG_CLIP);
        lv_label_set_text(s_axis_val[i], "0.00");
    }
}

static void build_reply_card(lv_obj_t *scr)
{
    lv_obj_t *card = make_box(scr, UI_CARD_X, UI_CARD_Y, UI_CARD_W, UI_CARD_H,
                              UI_C_CARD_HI, UI_C_LINE, 14);
    lv_obj_set_style_bg_grad_color(card, lv_color_hex(UI_C_CARD), 0);
    lv_obj_set_style_bg_grad_dir(card, LV_GRAD_DIR_VER, 0);

    lv_obj_t *tag = make_box(card, UI_TAG_X, UI_TAG_Y, UI_TAG_W, UI_TAG_H, UI_C_BLUE, 0, 7);
    lv_obj_set_style_bg_opa(tag, 48, 0);        /* 半透明徽标，不抢正文 */
    lv_obj_t *tag_lbl = make_label(tag, 0, 0, UI_TAG_W, UI_TAG_H,
                                   &lv_font_montserrat_14, UI_C_BLUE, LV_TEXT_ALIGN_CENTER);
    lv_label_set_long_mode(tag_lbl, LV_LABEL_LONG_CLIP);
    lv_label_set_text(tag_lbl, "AI");

    /* 生成中：转圈；否则右下角显示页码 */
    s_spinner = lv_spinner_create(card);
    lv_obj_set_size(s_spinner, UI_SPIN_D, UI_SPIN_D);
    lv_obj_set_pos(s_spinner, UI_SPIN_X, UI_SPIN_Y);
    lv_spinner_set_anim_params(s_spinner, 900, 60);
    lv_obj_set_style_bg_opa(s_spinner, LV_OPA_TRANSP, LV_PART_MAIN);
    lv_obj_set_style_arc_width(s_spinner, 2, LV_PART_MAIN);
    lv_obj_set_style_arc_color(s_spinner, lv_color_hex(UI_C_TRACK), LV_PART_MAIN);
    lv_obj_set_style_arc_width(s_spinner, 2, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(s_spinner, lv_color_hex(UI_C_AMBER), LV_PART_INDICATOR);
    lv_obj_set_hidden(s_spinner, true);

    s_lbl_page = make_label(card, UI_PAGE_X, UI_TAG_Y, UI_PAGE_W, UI_TAG_H,
                            &lv_font_montserrat_14, UI_C_FAINT, LV_TEXT_ALIGN_RIGHT);
    lv_label_set_long_mode(s_lbl_page, LV_LABEL_LONG_CLIP);
    lv_label_set_text(s_lbl_page, "");

    s_lbl_reply = make_label(card, UI_REPLY_X, UI_REPLY_Y, UI_REPLY_W, UI_REPLY_H,
                             cjk_font(14), UI_C_AMBER, LV_TEXT_ALIGN_LEFT);
    lv_label_set_text(s_lbl_reply, "按 BOOT 键向电脑服务器的 AI 提问");
}

esp_err_t ui_init(void)
{
    bsp_display_lock(0);

    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(UI_C_BG), 0);
    lv_obj_set_style_bg_grad_color(scr, lv_color_hex(UI_C_BG_DEEP), 0);
    lv_obj_set_style_bg_grad_dir(scr, LV_GRAD_DIR_VER, 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);

    build_status_bar(scr);
    build_activity_panel(scr);
    build_tilt_panel(scr);
    build_axis_rows(scr);
    build_reply_card(scr);

    lv_timer_create(ui_timer_cb, 500, NULL);

    bsp_display_unlock();
    ESP_LOGI(TAG, "ui ready (graphical dashboard, %dx%d)", UI_SCR_W, UI_SCR_H);
    return ESP_OK;
}
