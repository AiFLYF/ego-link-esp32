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
 *   | * 在线        100Hz                 ↑1234  | 状态胶囊（指令执行时右侧改显徽标）
 *   +----------------------+---------------------+
 *   |        /-----\\       |     /-------\\       | 左：活动环（=|a| 量程 + 活动词）
 *   |        | 静置 |       |     |   *   |       |     面板边框随活动色微染
 *   |        \\-----/       |     \\-------/       | 右：姿态球（重力方向 + 倾角 + 斜刻度）
 *   |  ● 水平             |      倾角 12°        |
 *   +----------------------+---------------------+
 *   | X ===------ | ---●--- Y ==-----   Z ...    | 三轴对称条（±2g，亮线是 0 位）
 *   +--------------------------------------------+
 *   |▌[AI]                          ● ● ○        |
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

#include "accel_input.h"    /* accel_input_get_orientation()：倾角小字里要显示档位 oN */
#include "provisioning.h"
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

/* 状态胶囊（三段式：左链路 / 中采样率 / 右上报数；指令徽章出现时临时顶替上报数。
 * 旧版最右格塞「SC7A20 o0」芯片名+朝向档，观众看不懂还把胶囊挤得像乱码，
 * 调试信息在网页仪表盘上仍然看得到，板端不再显示。） */
#define UI_STATUS_Y     4
#define UI_STATUS_H     22
#define UI_STATUS_X     UI_MARGIN
#define UI_STATUS_W     (UI_SCR_W - 2 * UI_MARGIN)
#define UI_DOT_X        14
#define UI_DOT_D        8
#define UI_LINK_X       26
#define UI_LINK_W       48
#define UI_HZ_X         98
#define UI_HZ_W         44
#define UI_POSTS_X      132
#define UI_POSTS_W      90
#define UI_BADGE_X      186
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
#define UI_PANEL_CAP_W  108
#define UI_PANEL_CAP_H  14
#define UI_CAP_DOT_D    5                       /* 细节行左侧语义色圆点 */
#define UI_CAP_DOT_X    10

/* 右面板：姿态球 */
#define UI_BALL_D       76
#define UI_BALL_Y       0
#define UI_BALL_W       1                       /* 外圈线宽 */
#define UI_BUBBLE_D     18                      /* 重力球直径 */
#define UI_BUBBLE_MAX   26                      /* 球心最大偏移（px），对应 1g */
#define UI_LEVEL_D      22                      /* 中心「水平区」参考圈 */
#define UI_CROSS_LEN    52
#define UI_CROSS_DIAG   34                      /* 45° 斜辅助线（水平仪刻度感） */

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
/* 分页圆点指示器（右上，与转圈互斥）。总页数 ≤ UI_PDOTS_N 时显示圆点，
 * 超出（极长回复）退回文字页码。 */
#define UI_PDOTS_N      5
#define UI_PDOT_D       4
#define UI_PDOT_GAP     3
#define UI_PDOTS_RIGHT  226                     /* 圆点区右缘（卡片内坐标） */
#define UI_PDOTS_Y      11                      /* 与 AI 徽标同一行 */
#define UI_STRIPE_W     3                       /* 卡片左侧语义色竖条 */
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
#define UI_C_TRACK      0x232C39                /* 进度/环的底槽（提亮一档，轨道不再发灰） */
#define UI_C_MARK       0x3D4757                /* 0 位中线 / 斜辅助线等刻度 */
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
static lv_obj_t *s_badge;
static lv_obj_t *s_lbl_badge;

static lv_obj_t *s_glow;                        /* 跌落告警的呼吸光环 */
static lv_obj_t *s_panel_act;                  /* 活动面板（边框随活动色微染） */
static lv_obj_t *s_cap_dot;                    /* 细节行左侧语义色圆点 */
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
static lv_obj_t *s_pdot[UI_PDOTS_N];           /* 分页圆点（总页数 ≤ N） */
static lv_obj_t *s_spinner;

/* 只在值变化时才动控件，避免 500ms 定时器把动画一次次重置 */
#define UI_NONE (-1000000)

static char     s_last_reply[TRANSPORT_REPLY_LEN];
static char     s_last_activity[TRANSPORT_ACTIVITY_LEN];
static char     s_last_detail[32];          /* 细节行上次的文本（含本地算的倾斜方向） */
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

/* ---- 配网覆盖层（PROPOSAL §1.8 验收 #1）----------------------------------
 * 配网时板子还没连上任何网络，遥测数据没有意义，而屏幕是用户唯一的"说明书"：
 * 必须把 AP 名、4 位密码、要打开的地址显示清楚。做成整屏覆盖层而不是另建 screen，
 * 是为了不动既有的布局代码；显示时定时器提前返回，底下的控件不会被重绘。 */
static lv_obj_t *s_prov_panel;
static lv_obj_t *s_prov_ssid;
static lv_obj_t *s_prov_pass;
static lv_obj_t *s_prov_url;
static lv_obj_t *s_prov_hint;
static lv_obj_t *s_prov_result;
static bool      s_prov_shown;

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

/* 本地倾斜方位 —— **与服务端 tilt_direction() 用同一套约定**（屏幕系 +x 右、+y 下，
 * atan2(y,x) 定方位），所以板子和网页永远说同一句话。
 *
 * 为什么板子要自己算一份：原来的方向词是**从服务器文案里解析**出来的，于是
 * **没网就永远停在旧值**，"长按 BOOT 标定方向"这件事在离线时根本做不了
 * （2026-09-23 真机实测就卡在这）。服务器文案还在时优先用它，这里只做兜底。 */
static const char *local_tilt_word(float x, float y)
{
    if (x * x + y * y < 0.0625f) {          /* 0.25²，与服务端阈值一致 */
        return "水平";
    }
    float deg = atan2f(y, x) * 57.29578f;
    if (deg < 0.0f) {
        deg += 360.0f;
    }
    static const char *const W[8] = {"右", "右下", "下", "左下", "左", "左上", "上", "右上"};
    return W[((int)((deg + 22.5f) / 45.0f)) & 7];
}

/* 细节行：静置给方向，运动/步行给峰值与步数，其余给一句短的。
 * 有服务器文案时**优先用从文案里解析出来的真实数值**；没有时（离线）用本地算的方位兜底。 */
static void build_detail(act_kind_t kind, const char *activity,
                         float x_g, float y_g, char *out, size_t out_sz)
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
    case ACT_IDLE:      /* 离线：服务器文案是空的，只能本地算 */
        if (strstr(activity, "向左") || strstr(activity, "左")) {
            snprintf(out, out_sz, "向左倾斜");
        } else if (strstr(activity, "向右") || strstr(activity, "右")) {
            snprintf(out, out_sz, "向右倾斜");
        } else if (strstr(activity, "向上") || strstr(activity, "上")) {
            snprintf(out, out_sz, "向上倾斜");
        } else if (strstr(activity, "向下") || strstr(activity, "下")) {
            snprintf(out, out_sz, "向下倾斜");
        } else {
            const char *w = local_tilt_word(x_g, y_g);
            if (strcmp(w, "水平") == 0) {
                snprintf(out, out_sz, "水平");
            } else {
                snprintf(out, out_sz, "向%s倾斜", w);
            }
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

/* 活动切换时让文字快速淡入一次（40→255，260ms），提示「状态变了」又不吵闹 */
static void flash_label(lv_obj_t *obj)
{
    lv_anim_delete(obj, anim_opa);
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, obj);
    lv_anim_set_values(&a, 40, 255);
    lv_anim_set_duration(&a, 260);
    lv_anim_set_exec_cb(&a, anim_opa);
    lv_anim_start(&a);
}

/* AI 回复分页指示器：页数少时用圆点，页数多（极长回复）退回 "3/12" 文字。
 * pending 生成中由调用方隐藏整个指示器、改显示转圈。 */
static void refresh_pager(int page, int total, bool pending)
{
    bool dots = (!pending && total > 1 && total <= UI_PDOTS_N);

    for (int i = 0; i < UI_PDOTS_N; i++) {
        if (dots && i < total) {
            lv_obj_set_hidden(s_pdot[i], false);
            uint32_t c = (i == page) ? UI_C_AMBER : UI_C_MARK;
            lv_obj_set_style_bg_color(s_pdot[i], lv_color_hex(c), 0);
        } else {
            lv_obj_set_hidden(s_pdot[i], true);
        }
    }

    if (pending || dots || total <= 1) {
        lv_label_set_text(s_lbl_page, "");
    } else {
        /* 24 字节容得下两个 10 位 int + '/' + '\0'，避免 -Wformat-truncation：
         * 实际 total 受回复长度限制是个位数，但编译器按 int 最坏情况静态推算。 */
        char buf[24];
        snprintf(buf, sizeof(buf), "%d/%d", page + 1, total);
        lv_label_set_text(s_lbl_page, buf);
    }
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

    /* 配网模式：整屏让给配网提示（验收 #1 要求屏幕显示 AP 名 + 4 位码 + 地址）。
     * 提前返回，底下的仪表盘控件这一帧就不重绘了。 */
    if (provisioning_is_active()) {
        s_prov_shown = true;
        char pbuf[80];
        snprintf(pbuf, sizeof(pbuf), "WiFi  %s", provisioning_ap_ssid());
        lv_label_set_text(s_prov_ssid, pbuf);
        snprintf(pbuf, sizeof(pbuf), "密码  %s", provisioning_ap_pass());
        lv_label_set_text(s_prov_pass, pbuf);
        lv_label_set_text(s_prov_url, PROV_AP_IP);

        const char *res = provisioning_last_result();
        lv_label_set_text(s_prov_result, res[0] ? res : "");
        lv_obj_set_style_text_color(s_prov_result,
                                    lv_color_hex(res[0] ? UI_C_GREEN : UI_C_DIM), 0);
        lv_obj_set_hidden(s_prov_panel, false);
        return;
    }
    if (s_prov_shown) {
        /* 刚从配网切回仪表盘：清掉"只在变化时才写"那几处缓存，
         * 否则若内容恰好与配网前相同，屏幕上会残留配网提示。 */
        s_prov_shown = false;
        s_last_reply[0] = '\0';
        s_last_activity[0] = '\0';
        s_last_abs = UI_NONE;
        s_last_badge = -1;
        s_last_online = false;      /* 强制下一帧重画在线状态 */
        lv_obj_set_hidden(s_prov_panel, true);
    }

    transport_status_t st;
    transport_get_status(&st);

    /* 姿态球/数值条**直接用原始值，不做低通** —— 要跟手。
     *
     * 上一版在这里加了 α=0.25 的一阶低通去压"倾角乱跳"，结果把球也拖慢了
     * （用户 2026-09-23 反馈"球移动速度太慢"）。低通加错了地方：
     *  - 姿态球对噪声本来就不敏感（`bx = x * 26px`，0.03g 只动不到 1px），
     *    它需要的是**快**；
     *  - 真正对噪声敏感的是 `acos(|z|/|a|)` —— 在接近平放时 0.03g 就能算出 14°。
     *    所以**只对角度那一个数**做低通（见下面 s_deg 那段），别碰 x/y/z。
     * 另外 `transport` 现在以 20Hz 刷新状态（原来只有 500ms 一次），球本身就顺了。 */
    const float dx = st.x_g, dy = st.y_g, dz = st.z_g;

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

    /* ---------- 状态胶囊：采样率 / 上报数 ---------- */
    snprintf(buf, sizeof(buf), "%dHz", (int)(1000 / CONFIG_RW1_SAMPLE_PERIOD_MS));
    lv_label_set_text(s_lbl_hz, buf);

    snprintf(buf, sizeof(buf), "↑%u", (unsigned)st.posts_ok);
    lv_label_set_text(s_lbl_posts, buf);

    /* ---------- 远程指令徽标（出现时临时顶替上报数，二者互斥） ---------- */
    int badge = (st.cmd_state <= TRANSPORT_CMD_FAILED) ? (int)st.cmd_state : 0;
    if (badge != s_last_badge) {
        s_last_badge = badge;
        static const char *const badge_text[4] = {"", "...", "OK", "NG"};
        static const uint32_t badge_color[4] = {0, UI_C_AMBER, UI_C_GREEN, UI_C_RED};

        if (badge == TRANSPORT_CMD_IDLE) {
            lv_obj_set_hidden(s_badge, true);
            lv_obj_set_hidden(s_lbl_posts, false);
        } else {
            lv_label_set_text(s_lbl_badge, badge_text[badge]);
            lv_obj_set_style_bg_color(s_badge, lv_color_hex(badge_color[badge]), 0);
            lv_obj_set_hidden(s_badge, false);
            lv_obj_set_hidden(s_lbl_posts, true);
        }
    }

    /* ---------- 活动环：活动词 + 语义色 + 细节行 ---------- */
    const act_kind_t kind = classify(st.activity);
    if (strcmp(st.activity, s_last_activity) != 0) {
        strlcpy(s_last_activity, st.activity, sizeof(s_last_activity));

        uint32_t act_color = ACT_COLOR[kind];

        lv_label_set_text(s_lbl_word, ACT_WORD[kind]);
        lv_obj_set_style_text_color(s_lbl_word, lv_color_hex(act_color), 0);
        lv_obj_set_style_arc_color(s_arc, lv_color_hex(act_color), LV_PART_INDICATOR);
        /* 面板边框淡淡染成活动色，细节行圆点同步——远看也能分辨当前状态 */
        lv_obj_set_style_border_color(s_panel_act, lv_color_hex(act_color), 0);
        lv_obj_set_style_border_opa(s_panel_act, 80, 0);
        lv_obj_set_style_bg_color(s_cap_dot, lv_color_hex(act_color), 0);
        flash_label(s_lbl_word);

        set_fall_alert(kind == ACT_FALL);
    }

    /* 细节行**每次都算**（不再挂在"服务器文案变了"这个条件上）：
     * 它现在含**本地算的**倾斜方向，而方向是随姿态实时变的。
     * 挂在那个条件上的话，离线时（服务器文案一直是空的）方向词永远不更新 ——
     * 而"长按 BOOT 标定方向"恰恰要看着它。只在文本真的变了时才写 + 闪。 */
    {
        char detail[32];
        build_detail(kind, st.activity, dx, dy, detail, sizeof(detail));
        if (strcmp(detail, s_last_detail) != 0) {
            strlcpy(s_last_detail, detail, sizeof(s_last_detail));
            lv_label_set_text(s_lbl_detail, detail);
            flash_label(s_lbl_detail);
        }
    }

    /* ---------- 活动环数值：|a| 映射到 0..100 ---------- */
    float mag = sqrtf(dx * dx + dy * dy + dz * dz);
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
    float bx = dx * UI_BUBBLE_SCALE;
    float by = dy * UI_BUBBLE_SCALE;
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
    float horiz = sqrtf(dx * dx + dy * dy);
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
        /* 倾角小字跟随同一语义色：水平绿 / 倾斜琥珀 / 失重或大角度红 / 无读数灰 */
        uint32_t tilt_c = (ball_color == UI_C_FAINT) ? UI_C_FAINT
                        : (ball_color == UI_C_GREEN) ? UI_C_GREEN
                        : (ball_color == UI_C_AMBER) ? UI_C_AMBER : UI_C_RED;
        lv_obj_set_style_text_color(s_lbl_tilt, lv_color_hex(tilt_c), 0);
    }

    /* 倾角小字里**带上当前方向档位 `oN`**。
     * 为什么必须显示：档位只能在串口日志里看到，于是"长按 BOOT 校准方向"这件事
     * 变成了必须插着电脑开串口才能做 —— 2026-09-23 真机标定时就是这么卡住的
     * （屏幕上没有 oN，用户按了也分不清自己在哪一档）。
     * 显示出来之后，标定就是纯屏上操作：长按换档 → 看方向对不对 → 对了就停。 */
    const int ori = accel_input_get_orientation();

    /* `oN` **只在换档后显示 5 秒**，平时倾角小字回到干净的「倾角 N°」。
     * 理由：这一行宽度是固定的（UI_PANEL_CAP_W = 100），一直挂着 `oN` 会把
     * 本来就紧的排版挤得更满（用户 2026-09-23 反馈要调整排版）。
     * 而 `oN` 只在**标定方向**时才需要看 —— 长按 BOOT 换档后它会自己出现 5 秒，
     * 够看清自己在哪一档了。开机时也显示一次，方便确认当前档位。 */
    static int s_last_ori = -1;
    static uint32_t s_ori_at = 0;
    if (ori != s_last_ori) {
        s_last_ori = ori;
        s_ori_at = lv_tick_get();
    }
    const bool show_ori = (lv_tick_get() - s_ori_at) < 5000;

    /* 只对**角度**做低通：`acos(|z|/|a|)` 在接近平放时对噪声极敏感
     * （0.03g → 14°），不滤的话数字乱跳；但滤 x/y/z 会把姿态球拖慢（上一版的错）。
     * 滤角度本身只影响这一个数字，姿态球完全不受影响。 */
    static float s_deg = 0.0f;
    static bool s_deg_primed = false;
    if (mag < 0.05f) {
        snprintf(buf, sizeof(buf), show_ori ? "无读数 o%d" : "无读数", ori);
    } else if (mag < 0.35f) {
        s_deg_primed = false;          /* 失重后重新起滤，别拿旧值 */
        snprintf(buf, sizeof(buf), show_ori ? "失重 o%d" : "失重", ori);
    } else {
        float c = fabsf(dz) / mag;
        if (c > 1.0f) {
            c = 1.0f;
        }
        const float raw_deg = acosf(c) * 57.29578f;
        if (!s_deg_primed) {
            s_deg = raw_deg;
            s_deg_primed = true;
        } else {
            s_deg += 0.4f * (raw_deg - s_deg);
        }
        int deg = (int)(s_deg + 0.5f);
        /* 去掉「倾角」和数字之间的空格：这个 CJK 字体里 ASCII 是**全角**的，
         * 一个空格就是 12px —— 带上它会顶破 100px 的标签宽度而**换行**，
         * 第二行又被 14px 的行高裁掉（用户拍到的"显示不全"就是这个）。 */
        snprintf(buf, sizeof(buf), show_ori ? "倾角%d° o%d" : "倾角%d°", deg, ori);
    }
    lv_label_set_text(s_lbl_tilt, buf);

    /* ---------- 三轴对称条 ---------- */
    float axis[3] = {dx, dy, dz};
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

        bool has_reply = st.reply[0] != '\0';
        const char *text = has_reply ? st.reply : "按 BOOT 键向电脑服务器的 AI 提问";
        char pagebuf[128];
        s_page_total = reply_page(text, 0, pagebuf, sizeof(pagebuf));
        lv_label_set_text(s_lbl_reply, pagebuf);
        /* 生成中压暗；空引导语用弱化灰，只有真正的 AI 回复才用琥珀色 */
        uint32_t reply_c = st.ai_pending ? UI_C_DIM : has_reply ? UI_C_AMBER : UI_C_FAINT;
        lv_obj_set_style_text_color(s_lbl_reply, lv_color_hex(reply_c), 0);

        lv_obj_set_hidden(s_spinner, !st.ai_pending);
        refresh_pager(0, s_page_total, st.ai_pending);
    } else if (!st.ai_pending && s_page_total > 1) {
        if (++s_page_tick >= UI_PAGE_TICKS) {
            s_page_tick = 0;
            s_page = (s_page + 1) % s_page_total;

            char pagebuf[128];
            reply_page(st.reply, s_page, pagebuf, sizeof(pagebuf));
            lv_label_set_text(s_lbl_reply, pagebuf);
            refresh_pager(s_page, s_page_total, false);
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

    /* ↑ 在 Montserrat 里没有字形（会显示成怪字符），上报数改用 CJK 子集字体：
     * gen_font.py 会把 ui.c 里出现的 ↑ 自动裁进子集，SimHei 的数字也是等宽的。 */
    s_lbl_posts = make_label(bar, UI_POSTS_X - UI_STATUS_X, (UI_STATUS_H - 14) / 2,
                             UI_POSTS_W, 14, cjk_font(12), UI_C_DIM,
                             LV_TEXT_ALIGN_RIGHT);
    lv_label_set_text(s_lbl_posts, "↑0");

    s_badge = make_box(bar, UI_BADGE_X - UI_STATUS_X, (UI_STATUS_H - UI_BADGE_H) / 2,
                       UI_BADGE_W, UI_BADGE_H, UI_C_AMBER, 0, UI_BADGE_H / 2);
    lv_obj_set_hidden(s_badge, true);
    s_lbl_badge = make_label(s_badge, 0, 1, UI_BADGE_W, UI_BADGE_H - 2,
                             &lv_font_montserrat_14, 0x0A0E14, LV_TEXT_ALIGN_CENTER);
    lv_label_set_text(s_lbl_badge, "...");
}

static void build_activity_panel(lv_obj_t *scr)
{
    s_panel_act = make_box(scr, UI_PANEL_LX, UI_PANEL_Y, UI_PANEL_W, UI_PANEL_H,
                           UI_C_CARD, UI_C_LINE, 16);

    /* 跌落呼吸光环：比活动环大一圈，平时隐藏 */
    s_glow = make_box(s_panel_act, (UI_PANEL_W - (UI_RING_D + 14)) / 2, UI_RING_Y - 7,
                      UI_RING_D + 14, UI_RING_D + 14, 0, UI_C_RED, LV_RADIUS_CIRCLE);
    lv_obj_set_style_border_width(s_glow, 2, 0);
    lv_obj_set_style_opa(s_glow, LV_OPA_TRANSP, 0);
    lv_obj_set_hidden(s_glow, true);

    /* 活动环：270° 仪表，量程 |a| 0..2g */
    s_arc = lv_arc_create(s_panel_act);
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

    s_lbl_word = make_label(s_panel_act, (UI_PANEL_W - UI_WORD_W) / 2, UI_WORD_Y,
                            UI_WORD_W, UI_WORD_H,
                            cjk_font(18), UI_C_GREEN, LV_TEXT_ALIGN_CENTER);
    lv_label_set_long_mode(s_lbl_word, LV_LABEL_LONG_CLIP);
    lv_label_set_text(s_lbl_word, "等待");

    s_lbl_abs = make_label(s_panel_act, (UI_PANEL_W - UI_ABS_W) / 2, UI_ABS_Y,
                           UI_ABS_W, UI_ABS_H,
                           &lv_font_montserrat_14, UI_C_DIM, LV_TEXT_ALIGN_CENTER);
    lv_label_set_long_mode(s_lbl_abs, LV_LABEL_LONG_CLIP);
    lv_label_set_text(s_lbl_abs, "0.00g");

    /* 细节行左侧的语义色圆点（与活动色同步） */
    s_cap_dot = make_box(s_panel_act, UI_CAP_DOT_X,
                         UI_PANEL_CAP_Y + (UI_PANEL_CAP_H - UI_CAP_DOT_D) / 2,
                         UI_CAP_DOT_D, UI_CAP_DOT_D, UI_C_FAINT, 0, LV_RADIUS_CIRCLE);

    s_lbl_detail = make_label(s_panel_act, (UI_PANEL_W - UI_PANEL_CAP_W) / 2, UI_PANEL_CAP_Y,
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

    /* 45° 斜辅助线：水平仪刻度感，比主十字更弱（半透明）。
     * lv_line 只保存点表指针，数组必须 static；线对象移到圆盘中心，点相对中心。
     * 点表必须用 lv_point_precise_t（不是 lv_point_t）：本版 LVGL 的
     * lv_line_set_points 形参是 const lv_point_precise_t*，用 lv_point_t 会因
     * 类型不兼容被 -Werror 拦下。字段名同为 x/y，整数字面量两种坐标精度都兼容。 */
    static const lv_point_precise_t diag_pts[2][2] = {
        {{-12, -12}, {12, 12}},
        {{-12,  12}, {12, -12}},
    };
    for (int i = 0; i < 2; i++) {
        lv_obj_t *ln = lv_line_create(disc);
        lv_obj_set_scrollable(ln, false);
        lv_obj_set_pos(ln, cx, cx);
        lv_line_set_points(ln, diag_pts[i], 2);
        lv_obj_set_style_line_width(ln, 1, 0);
        lv_obj_set_style_line_color(ln, lv_color_hex(UI_C_MARK), 0);
        lv_obj_set_style_line_opa(ln, 90, 0);
        lv_obj_set_style_line_rounded(ln, true, 0);
    }

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
    /* **必须禁止换行**：标签默认是 LV_LABEL_LONG_WRAP，而这里的行高只有 14px ——
     * 文字一超宽就换到第二行，而第二行被高度裁掉，看起来就是"显示不全"。
     * 宁可裁（CLIP）也不要换行。 */
    lv_label_set_long_mode(s_lbl_tilt, LV_LABEL_LONG_CLIP);
    lv_label_set_text(s_lbl_tilt, "倾角—");
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

        /* 0 位中轴亮线：比轨道高一个像素，正负两边的刻度尺，始终压在最上层 */
        make_box(scr, UI_AXIS_BAR_X + UI_AXIS_BAR_W / 2,
                 y + (UI_AXIS_ROW_H - (UI_AXIS_BAR_H + 2)) / 2,
                 1, UI_AXIS_BAR_H + 2, UI_C_MARK, 0, 0);

        /* 数值与 X/Y/Z 标签同色，左右呼应，不再是一串灰蒙蒙的数字 */
        s_axis_val[i] = make_label(scr, UI_AXIS_VAL_X, y, UI_AXIS_VAL_W, UI_AXIS_ROW_H,
                                   &lv_font_montserrat_14, axis_color[i], LV_TEXT_ALIGN_RIGHT);
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

    /* 左侧 AI 蓝色竖条：整条消息的语义锚点 */
    make_box(card, 0, 12, UI_STRIPE_W, UI_CARD_H - 24, UI_C_BLUE, 0, 2);

    lv_obj_t *tag = make_box(card, UI_TAG_X, UI_TAG_Y, UI_TAG_W, UI_TAG_H, UI_C_BLUE, 0, 7);
    lv_obj_set_style_bg_opa(tag, 48, 0);        /* 半透明徽标，不抢正文 */
    lv_obj_t *tag_lbl = make_label(tag, 0, 0, UI_TAG_W, UI_TAG_H,
                                   &lv_font_montserrat_14, UI_C_BLUE, LV_TEXT_ALIGN_CENTER);
    lv_label_set_long_mode(tag_lbl, LV_LABEL_LONG_CLIP);
    lv_label_set_text(tag_lbl, "AI");

    /* 生成中：转圈，与右侧分页指示器互斥 */
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

    /* 分页圆点（页少）与文字页码（页多），右缘对齐 */
    int dots_w = UI_PDOTS_N * UI_PDOT_D + (UI_PDOTS_N - 1) * UI_PDOT_GAP;
    for (int i = 0; i < UI_PDOTS_N; i++) {
        s_pdot[i] = make_box(card,
                             UI_PDOTS_RIGHT - dots_w + i * (UI_PDOT_D + UI_PDOT_GAP),
                             UI_PDOTS_Y - UI_PDOT_D / 2,
                             UI_PDOT_D, UI_PDOT_D, UI_C_MARK, 0, LV_RADIUS_CIRCLE);
        lv_obj_set_hidden(s_pdot[i], true);
    }

    s_lbl_page = make_label(card, UI_PAGE_X, UI_TAG_Y, UI_PAGE_W, UI_TAG_H,
                            &lv_font_montserrat_14, UI_C_FAINT, LV_TEXT_ALIGN_RIGHT);
    lv_label_set_long_mode(s_lbl_page, LV_LABEL_LONG_CLIP);
    lv_label_set_text(s_lbl_page, "");

    /* 初始是引导语不是回复，用弱化灰；timer 收到真回复后才换成琥珀色 */
    s_lbl_reply = make_label(card, UI_REPLY_X, UI_REPLY_Y, UI_REPLY_W, UI_REPLY_H,
                             cjk_font(14), UI_C_FAINT, LV_TEXT_ALIGN_LEFT);
    lv_label_set_text(s_lbl_reply, "按 BOOT 键向电脑服务器的 AI 提问");
}

/* 配网覆盖层：整屏盖住仪表盘，把"怎么连"讲清楚。
 * 视觉语言复用本文件的 UI_C_* 配色与三档字号，不引入新的样式体系。 */
static void build_prov_panel(lv_obj_t *scr)
{
    s_prov_panel = make_box(scr, 0, 0, UI_SCR_W, UI_SCR_H, UI_C_BG, 0, 0);
    lv_obj_set_style_bg_grad_color(s_prov_panel, lv_color_hex(UI_C_BG_DEEP), 0);
    lv_obj_set_style_bg_grad_dir(s_prov_panel, LV_GRAD_DIR_VER, 0);
    lv_obj_set_hidden(s_prov_panel, true);

    lv_obj_t *t = make_label(s_prov_panel, 0, 10, UI_SCR_W, 22,
                             cjk_font(18), UI_C_AMBER, LV_TEXT_ALIGN_CENTER);
    lv_label_set_text(t, "配网模式");

    lv_obj_t *sub = make_label(s_prov_panel, 0, 34, UI_SCR_W, 14,
                               cjk_font(12), UI_C_DIM, LV_TEXT_ALIGN_CENTER);
    lv_label_set_text(sub, "用手机连接下面这个热点");

    /* AP 名与密码是这一屏的主角，给最大字号 */
    s_prov_ssid = make_label(s_prov_panel, 6, 58, UI_SCR_W - 12, 20,
                             cjk_font(14), UI_C_GREEN, LV_TEXT_ALIGN_CENTER);
    s_prov_pass = make_label(s_prov_panel, 6, 82, UI_SCR_W - 12, 26,
                             cjk_font(18), UI_C_AMBER, LV_TEXT_ALIGN_CENTER);

    s_prov_url = make_label(s_prov_panel, 6, 118, UI_SCR_W - 12, 26,
                            cjk_font(18), UI_C_BLUE, LV_TEXT_ALIGN_CENTER);

    s_prov_hint = make_label(s_prov_panel, 8, 150, UI_SCR_W - 16, 44,
                             cjk_font(12), UI_C_DIM, LV_TEXT_ALIGN_CENTER);
    lv_label_set_text(s_prov_hint, "浏览器打开上面的地址\n选 WiFi、填服务器地址后点「保存并连接」");

    s_prov_result = make_label(s_prov_panel, 8, 198, UI_SCR_W - 16, 34,
                               cjk_font(12), UI_C_GREEN, LV_TEXT_ALIGN_CENTER);
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
    build_prov_panel(scr);      /* 最后建：盖在所有面板之上 */

    lv_timer_create(ui_timer_cb, 500, NULL);

    bsp_display_unlock();
    ESP_LOGI(TAG, "ui ready (graphical dashboard, %dx%d)", UI_SCR_W, UI_SCR_H);
    return ESP_OK;
}
