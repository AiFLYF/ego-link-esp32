/*
 * SPDX-License-Identifier: MIT
 *
 * 摄像头取帧实现（esp_video / V4L2）。完整调用序列见本文件末尾注释。
 *
 * 只依赖 BSP 的 bsp_camera_start()，其余全部走 POSIX + ioctl。
 * 引脚、I2C、XCLK 都由 BSP 处理，本文件不做任何硬件配置。
 */

#include "camera.h"

#include <fcntl.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/time.h>
#include <unistd.h>

#include "bsp/esp-bsp.h"
/* BSP 把 BSP_CAMERA_DEVICE 定义成 ESP_VIDEO_DVP_DEVICE_NAME，但 bsp/esp32_s3_eye.h
 * **只用了这个宏、没有 include 定义它的头文件**（BSP 自己的 bsp_camera.c 里包含了，
 * 头文件里漏了）。不补这一行，编译会报 'ESP_VIDEO_DVP_DEVICE_NAME' undeclared。 */
#include "driver/i2c_master.h"
#include "esp_http_client.h"
#include "esp_video_device.h"
#include "sd_card.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_video_ioctl.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "linux/v4l2-controls.h"
#include "linux/videodev2.h"

static const char *TAG = "camera";

/* 必须是 Kconfig 里**开过**的档位。当前开的是 JPEG 320x240（见 sdkconfig.bsp.esp32_s3_eye）。
 * 传感器只会输出它配置表里存在的组合，填一个没开过的分辨率/格式，S_FMT 会直接失败。
 * **而且不同型号的可选档位不一样**（OV3660 的 JPEG 只有 1280x720）—— 所以
 * 不要写死分辨率，先 G_FMT 问驱动。 */
#define CAM_WIDTH   320
#define CAM_HEIGHT  240

/* 两个缓冲够用：一个在被驱动填充，一个给上层读。多了纯占 PSRAM。 */
#define CAM_NBUF    2

/* DQBUF 最多等多久。第一帧要等曝光收敛，太短会白白失败。 */
#define CAM_DQBUF_TIMEOUT_S   2

static int       s_fd = -1;
static uint8_t  *s_buf[CAM_NBUF];
static uint32_t  s_buf_len[CAM_NBUF];
static uint32_t  s_width;
static bool      s_is_jpeg;   /* 当前格式是不是硬件 JPEG（GC2145 没有） */
static uint32_t  s_height;
static bool      s_started;
/* BSP 那一层（I2C + XCLK + esp_video_init 注册 /dev/video2）**只能做一次**：
 * XCLK 是 LEDC 独占资源、设备名也只能注册一次，第二次调 bsp_camera_start() 必失败。
 * 自检结束后 camera_deinit() 会关掉 fd，但设备本身还在 —— 重新打开只需要
 * open + 定格式 + 申请缓冲，不用再走 BSP。 */
static bool      s_bsp_started;
static uint32_t  s_seq;

/* ---------------------------------------------------------------- helpers */

static void buf_init(struct v4l2_buffer *b)
{
    memset(b, 0, sizeof(*b));
    b->type   = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    b->memory = V4L2_MEMORY_MMAP;
}

static void close_all(void)
{
    int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;

    if (s_fd < 0) {
        return;
    }

    if (s_started) {
        ioctl(s_fd, VIDIOC_STREAMOFF, &type);
        s_started = false;
    }

    for (int i = 0; i < CAM_NBUF; i++) {
        if (s_buf[i]) {
            munmap(s_buf[i], s_buf_len[i]);
            s_buf[i] = NULL;
            s_buf_len[i] = 0;
        }
    }

    /* count=0 是 V4L2 里释放缓冲的标准写法 */
    struct v4l2_requestbuffers req = {0};
    req.count  = 0;
    req.type   = type;
    req.memory = V4L2_MEMORY_MMAP;
    ioctl(s_fd, VIDIOC_REQBUFS, &req);

    close(s_fd);
    s_fd = -1;
}

/* ------------------------------------------------------------------- API */

/* 把 BSP 那条 I2C（GPIO4/5）整个扫一遍，把应答的地址打出来。
 *
 * 为什么值得单独做这一步：摄像头探测失败时，**"没应答"和"地址不对"长得一模一样**
 * —— 前者是排线/模组，后者是型号选错，排查方向完全相反。扫一遍就能分开：
 *   只有 0x18（加速度计）      → 摄像头这一端没应答 → FPC 排线 / 模组
 *   有 0x30                    → 传感器在，是驱动/时序问题
 *   有别的地址（0x3C）         → 模组不是 OV2640（实测这块是 OV3660）
 *
 * 注意要在 XCLK 已经跑起来之后调 —— 传感器没有时钟不会应答 SCCB。
 * （bsp_camera_start() 里先起 XCLK 再探测，失败时不会释放它，所以这里时钟还在。） */
static void camera_scan_i2c(void)
{
    i2c_master_bus_handle_t bus = bsp_i2c_get_handle();
    if (bus == NULL) {
        ESP_LOGW(TAG, "I2C 扫描：拿不到 BSP 的 I2C 句柄");
        return;
    }
    char found[128];
    int off = 0, n = 0;
    for (uint8_t a = 0x03; a <= 0x77; a++) {
        if (i2c_master_probe(bus, a, 50) == ESP_OK) {
            n++;
            if (off < (int)sizeof(found) - 8) {
                off += snprintf(found + off, sizeof(found) - (size_t)off, "%02X ", a);
            }
        }
    }
    if (n == 0) {
        ESP_LOGE(TAG, "I2C 扫描：**一个设备都没有** —— 总线本身的问题，不是摄像头");
    } else {
        ESP_LOGI(TAG, "I2C 扫描到 %d 个设备: %s", n, found);
        ESP_LOGI(TAG, "  ↑ 0x18 是加速度计(SC7A20)；OV2640 在 0x30，"
                      "OV3660/GC2145 在 0x3C。都扫不到才轮到怀疑排线");
    }
}

esp_err_t camera_init(uint32_t *out_w, uint32_t *out_h)
{
    if (s_fd >= 0) {
        if (out_w) {
            *out_w = s_width;
        }
        if (out_h) {
            *out_h = s_height;
        }
        return ESP_OK;
    }

    /* ① BSP 负责 I2C + 16MHz XCLK + esp_video_init()（注册 /dev/video2）
     *    **只做一次**：自检拍完会 camera_deinit() 关掉 fd，之后拍照/推流要重新打开 ——
     *    那时设备还在，只需要 open + 定格式 + 申请缓冲。再调一次 bsp_camera_start()
     *    会因为 XCLK 已被占用而失败，表现成"自检能过、但拍照一直失败"（踩过）。 */
    if (!s_bsp_started) {
        bsp_camera_cfg_t cfg = {0};
        esp_err_t ret = bsp_camera_start(&cfg);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "bsp_camera_start 失败: %s", esp_err_to_name(ret));
            /* 失败时**顺手把 I2C 扫一遍**：这一步能把"排线没插"和"型号选错"分开，
             * 否则两种情况的报错长得一模一样，只能靠猜。 */
            camera_scan_i2c();
            return ret;
        }
        s_bsp_started = true;
    }

    s_fd = open(BSP_CAMERA_DEVICE, O_RDONLY);
    if (s_fd < 0) {
        /* 两种原因长得很像，但排查方向完全相反，所以这里要把话分开说清：
         *   上面有 "failed to detect DVP camera" → 驱动是开着的，是**传感器没响应**
         *     （硬件：FPC 排线没插紧 / 模组故障）。判据：同一 I2C 总线上加速度计
         *     正常的话，总线本身没问题，问题在摄像头这一端。
         *   上面什么都没有 → CONFIG_CAMERA_* 没开，驱动压根没编进来。 */
        ESP_LOGE(TAG, "open %s 失败：若上面有 'failed to detect DVP camera'，"
                      "是传感器没响应（检查摄像头 FPC 排线/模组）；"
                      "否则检查 CONFIG_CAMERA_* 是否开启（三个驱动都该开）",
                      BSP_CAMERA_DEVICE);
        return ESP_FAIL;
    }

    /* ② 定格式 —— **不能写死**。
     *
     * 踩过的坑（2026-09-24）：原来写死请求 JPEG 320x240。当时以为板载是 OV2640，
     * 而实测这块板子是 **OV3660** —— 它的 JPEG 只有 1280x720 这一个档位，
     * 于是 VIDIOC_S_FMT 直接失败，自检报"这个档位开了吗"，看着像配置问题，
     * 其实是**型号不同、可用档位就不同**。
     *
     * 现在改成：先问驱动"你现在是什么格式"（由 Kconfig 的 *_DVP_DEFAULT_FMT_* 决定，
     * 而那个是跟着**实际探测到的传感器**走的），只有当它不是 JPEG 时才尝试改成 JPEG。
     * 这样 OV2640 / OV3660 / GC2145 三种模组都不用改代码。 */
    struct v4l2_format fmt = {0};
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (ioctl(s_fd, VIDIOC_G_FMT, &fmt) != 0) {
        ESP_LOGE(TAG, "VIDIOC_G_FMT 失败");
        close_all();
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "驱动默认格式: %c%c%c%c %ux%u", (char)(fmt.fmt.pix.pixelformat & 0xFF),
             (char)((fmt.fmt.pix.pixelformat >> 8) & 0xFF),
             (char)((fmt.fmt.pix.pixelformat >> 16) & 0xFF),
             (char)((fmt.fmt.pix.pixelformat >> 24) & 0xFF),
             (unsigned)fmt.fmt.pix.width, (unsigned)fmt.fmt.pix.height);

    if (fmt.fmt.pix.pixelformat != V4L2_PIX_FMT_JPEG) {
        /* 不是 JPEG 就试着请求一个 —— 请求值仍可能被驱动改（它会给最接近的档位），
         * 所以无论成功失败都以读回的值为准。失败也不致命：有的传感器（GC2145）
         * 压根没有硬件 JPEG，那就用它原本的 YUV422 跑。 */
        struct v4l2_format want = {0};
        want.type                = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        want.fmt.pix.width       = CAM_WIDTH;
        want.fmt.pix.height      = CAM_HEIGHT;
        want.fmt.pix.pixelformat = V4L2_PIX_FMT_JPEG;
        if (ioctl(s_fd, VIDIOC_S_FMT, &want) == 0) {
            fmt = want;
        } else {
            ESP_LOGW(TAG, "这颗传感器没有 JPEG 档位，改用它的默认格式（帧会大很多，"
                          "推流帧率要相应降低）");
        }
    }
    s_width  = fmt.fmt.pix.width;
    s_height = fmt.fmt.pix.height;
    s_is_jpeg = (fmt.fmt.pix.pixelformat == V4L2_PIX_FMT_JPEG);
    ESP_LOGI(TAG, "最终格式: %s %ux%u", s_is_jpeg ? "JPEG" : "非 JPEG(原始)",
             (unsigned)s_width, (unsigned)s_height);

    /* ③ BSP 定义了 BSP_CAMERA_VFLIP=1 却**没有应用它**（bsp_camera_start 里只有
     *    xclk + esp_video_init）。不补这一下，取到的图是上下颠倒的。 */
    struct v4l2_ext_control flip = {0};
    flip.id    = V4L2_CID_VFLIP;
    flip.value = 1;
    struct v4l2_ext_controls ctrls = {0};
    ctrls.ctrl_class = V4L2_CTRL_CLASS_USER;
    ctrls.count      = 1;
    ctrls.controls   = &flip;
    if (ioctl(s_fd, VIDIOC_S_EXT_CTRLS, &ctrls) != 0) {
        /* 翻转失败不致命，只是画面方向可能不对，继续跑 */
        ESP_LOGW(TAG, "VFLIP 设置失败，画面可能上下颠倒");
    }

    /* ④ 申请缓冲并映射到用户空间（DVP 的缓冲本身就在 PSRAM，不占内部 RAM） */
    struct v4l2_requestbuffers req = {0};
    req.count  = CAM_NBUF;
    req.type   = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    req.memory = V4L2_MEMORY_MMAP;
    if (ioctl(s_fd, VIDIOC_REQBUFS, &req) != 0) {
        ESP_LOGE(TAG, "VIDIOC_REQBUFS 失败");
        close_all();
        return ESP_FAIL;
    }

    for (int i = 0; i < CAM_NBUF; i++) {
        struct v4l2_buffer b;
        buf_init(&b);
        b.index = (uint32_t)i;
        if (ioctl(s_fd, VIDIOC_QUERYBUF, &b) != 0) {
            ESP_LOGE(TAG, "VIDIOC_QUERYBUF(%d) 失败", i);
            close_all();
            return ESP_FAIL;
        }

        s_buf[i] = mmap(NULL, b.length, PROT_READ | PROT_WRITE, MAP_SHARED, s_fd, b.m.offset);
        if (s_buf[i] == MAP_FAILED) {
            ESP_LOGE(TAG, "mmap(%d) 失败", i);
            s_buf[i] = NULL;
            close_all();
            return ESP_FAIL;
        }
        s_buf_len[i] = b.length;

        if (ioctl(s_fd, VIDIOC_QBUF, &b) != 0) {
            ESP_LOGE(TAG, "VIDIOC_QBUF(%d) 失败", i);
            close_all();
            return ESP_FAIL;
        }
    }

    /* ⑤ 开始出图 */
    int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (ioctl(s_fd, VIDIOC_STREAMON, &type) != 0) {
        ESP_LOGE(TAG, "VIDIOC_STREAMON 失败");
        close_all();
        return ESP_FAIL;
    }
    s_started = true;

    struct timeval tv = {0};
    tv.tv_sec = CAM_DQBUF_TIMEOUT_S;
    ioctl(s_fd, VIDIOC_S_DQBUF_TIMEOUT, &tv);

    ESP_LOGI(TAG, "就绪: %s JPEG %" PRIu32 "x%" PRIu32 ", %d 缓冲 x %" PRIu32 " B",
             BSP_CAMERA_DEVICE, s_width, s_height, CAM_NBUF, s_buf_len[0]);

    if (out_w) {
        *out_w = s_width;
    }
    if (out_h) {
        *out_h = s_height;
    }
    return ESP_OK;
}

bool camera_ready(void)
{
    return (s_fd >= 0) && s_started;
}

/* 按 JPEG 结构自己找帧尾，返回整帧字节数（失败返回 0）。
 *
 * 为什么不能只信驱动的 bytesused（2026-09-24 实测）：
 * esp_video 把 bytesused 填成 element->valid_size，而 **DVP 传 JPEG 时长度不在
 * 时序里**，valid_size 是未初始化值 —— 实测 OV3660 上报 4294193410（0xFFFF4A02），
 * 拿它去 POST/写文件会把整个缓冲甚至越界内存发出去。
 *
 * 算法：JPEG 的熵编码数据里，FF 后面只可能是 00（字节填充）或标记，
 * 所以 **SOS(FF DA) 之后第一个 FF D9 就是真正的结尾**。
 * 先跳过文件头（找 SOS），再找 EOI —— 这样不会把缩略图里的 FFD9 当成结尾。 */
static uint32_t jpeg_scan_len(const uint8_t *d, uint32_t cap)
{
    if (d == NULL || cap < 4 || d[0] != 0xFF || d[1] != 0xD8) {
        return 0;
    }
    uint32_t i = 2;
    while (i + 1 < cap) {                       /* 跳过头部，找 SOS */
        if (d[i] == 0xFF && d[i + 1] == 0xDA) {
            break;
        }
        i++;
    }
    while (i + 1 < cap) {                       /* SOS 之后找 EOI */
        if (d[i] == 0xFF && d[i + 1] == 0xD9) {
            return i + 2;
        }
        i++;
    }
    return 0;
}

esp_err_t camera_capture(camera_frame_t *out)
{
    if (!out || !camera_ready()) {
        return ESP_ERR_INVALID_STATE;
    }

    struct v4l2_buffer b;
    buf_init(&b);
    if (ioctl(s_fd, VIDIOC_DQBUF, &b) != 0) {
        return ESP_FAIL;
    }

    /* 坏帧（V4L2_BUF_FLAG_ERROR）也要还回去，否则缓冲会越来越少 */
    if (!(b.flags & V4L2_BUF_FLAG_DONE)) {
        ioctl(s_fd, VIDIOC_QBUF, &b);
        return ESP_ERR_NOT_FOUND;
    }

    out->data   = s_buf[b.index];
    /* 帧长：JPEG 时**以自己扫出来的为准**（见 jpeg_scan_len 的注释）；
     * 扫不出来（不是 JPEG / 数据坏）才退回驱动的 bytesused，
     * 而且只在它没超过缓冲区大小时才敢用 —— 否则就是把越界内存当帧长。 */
    uint32_t cap = s_buf_len[b.index];
    uint32_t len = 0;
    if (s_is_jpeg) {
        len = jpeg_scan_len(out->data, cap);
    }
    if (len == 0 && b.bytesused > 0 && b.bytesused <= cap) {
        len = b.bytesused;
    }
    if (len == 0) {
        ioctl(s_fd, VIDIOC_QBUF, &b);           /* 坏帧还回去，别丢缓冲 */
        return ESP_ERR_INVALID_SIZE;
    }
    out->len    = len;
    out->width  = s_width;
    out->height = s_height;
    out->slot   = (int)b.index;
    out->seq    = ++s_seq;
    return ESP_OK;
}

void camera_release(const camera_frame_t *frame)
{
    if (!frame || s_fd < 0) {
        return;
    }
    struct v4l2_buffer b;
    buf_init(&b);
    b.index = (uint32_t)frame->slot;
    ioctl(s_fd, VIDIOC_QBUF, &b);
}

void camera_deinit(void)
{
    close_all();
    s_seq = 0;
}

/* ---------------------------------------------------------------------------
 * 推流 / 存卡
 *
 * 板子没有自己的 HTTP 服务端，所以"网页看实时画面"只能这么走：
 *     板子 --POST /api/frame--> 服务器 --GET /api/frame--> 网页 <img>
 * 这里只负责第一段。帧率由调用方（transport 的采样循环）控制。
 * ------------------------------------------------------------------------- */
esp_err_t camera_post_frame(const char *base_url, const char *device, bool save)
{
    if (base_url == NULL || base_url[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    if (!camera_ready() && camera_init(NULL, NULL) != ESP_OK) {
        return ESP_ERR_INVALID_STATE;
    }

    camera_frame_t fr = {0};
    esp_err_t ret = camera_capture(&fr);
    if (ret != ESP_OK) {
        return ret;
    }

    /* 拼 URL：`<base>/api/frame?device=<dev>[&save=1]`。
     * device 由网页/服务器校验，这里只做长度限制，不做 URL 转义
     * （设备名来自 NVS，是我们自己写进去的，不含特殊字符）。 */
    char url[192];
    snprintf(url, sizeof(url), "%s/api/frame?device=%s%s",
             base_url, (device && device[0]) ? device : "-", save ? "&save=1" : "");

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 4000,
        .buffer_size = 1024,
    };
    esp_http_client_handle_t cli = esp_http_client_init(&cfg);
    if (cli == NULL) {
        camera_release(&fr);
        return ESP_FAIL;
    }
    /* 直接用字节流 POST，不套 JSON —— JPEG 是二进制，套 base64 要多花 33% 带宽 */
    esp_http_client_set_header(cli, "Content-Type", "image/jpeg");
    esp_http_client_set_post_field(cli, (const char *)fr.data, (int)fr.len);
    ret = esp_http_client_perform(cli);
    int code = esp_http_client_get_status_code(cli);
    esp_http_client_cleanup(cli);
    camera_release(&fr);

    if (ret != ESP_OK || code != 200) {
        /* 推流失败不该刷屏：画面丢一帧而已，5 帧/秒下用户根本看不出来 */
        return ESP_FAIL;
    }
    return ESP_OK;
}

esp_err_t camera_save_to_sd(char *name_out, size_t name_len)
{
    if (!sd_card_mounted()) {
        ESP_LOGW(TAG, "没挂载 SD 卡，拍的照片没地方放");
        return ESP_ERR_INVALID_STATE;
    }
    if (!camera_ready() && camera_init(NULL, NULL) != ESP_OK) {
        return ESP_ERR_INVALID_STATE;
    }
    camera_frame_t fr = {0};
    esp_err_t ret = camera_capture(&fr);
    if (ret != ESP_OK) {
        return ret;
    }

    /* 8.3 文件名：CAM0007.JPG。序号递增，重启后从 1 开始 ——
     * 重名就直接覆盖，比"为了不重名去扫描整个目录"省事得多。 */
    static unsigned s_seq;
    char path[64], name[16];
    for (int i = 0; i < 1000; i++) {
        s_seq++;
        snprintf(name, sizeof(name), "CAM%04u.JPG", s_seq % 10000);
        snprintf(path, sizeof(path), "%s/%s", BSP_SD_MOUNT_POINT, name);
        FILE *f = fopen(path, "wb");
        if (f == NULL) {
            continue;               /* 大概率是重名，换个号再试 */
        }
        size_t wrote = fwrite(fr.data, 1, fr.len, f);
        fclose(f);
        camera_release(&fr);
        if (wrote != fr.len) {
            ESP_LOGE(TAG, "写 %s 只成功 %u/%u 字节", path, (unsigned)wrote, (unsigned)fr.len);
            return ESP_FAIL;
        }
        if (name_out && name_len) {
            strlcpy(name_out, name, name_len);
        }
        ESP_LOGI(TAG, "已存 %s（%u 字节）", path, (unsigned)fr.len);
        return ESP_OK;
    }
    camera_release(&fr);
    ESP_LOGE(TAG, "找不到可用文件名");
    return ESP_FAIL;
}

esp_err_t camera_selftest(void)
{
    uint32_t w = 0, h = 0;
    int64_t t0 = esp_timer_get_time();

    esp_err_t ret = camera_init(&w, &h);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "自检失败: 初始化 %s", esp_err_to_name(ret));
        return ret;
    }

    /* 第一帧要等曝光收敛，给几次机会 */
    camera_frame_t f = {0};
    for (int attempt = 0; attempt < 5; attempt++) {
        ret = camera_capture(&f);
        if (ret == ESP_OK) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "自检失败: 取帧 %s（%d 次尝试）", esp_err_to_name(ret), 5);
        camera_deinit();
        return ret;
    }

    int64_t elapsed_ms = (esp_timer_get_time() - t0) / 1000;

    /* JPEG 合法性：头必须是 FF D8，尾必须是 FF D9。
     * 只验头不够——截断的帧也有头，但服务端拿到会解码失败。 */
    bool head_ok = (f.len >= 2) && (f.data[0] == 0xFF) && (f.data[1] == 0xD8);
    bool tail_ok = (f.len >= 4) && (f.data[f.len - 2] == 0xFF) && (f.data[f.len - 1] == 0xD9);

    ESP_LOGI(TAG, "自检: JPEG %" PRIu32 "x%" PRIu32 ", 第 %" PRIu32 " 帧, %" PRIu32 " B, 耗时 %" PRId64 " ms",
             f.width, f.height, f.seq, f.len, elapsed_ms);
    ESP_LOGI(TAG, "自检: JPEG 头 %s / 尾 %s", head_ok ? "OK" : "坏", tail_ok ? "OK" : "坏");

    camera_release(&f);
    camera_deinit();

    if (!head_ok || !tail_ok) {
        ESP_LOGE(TAG, "自检失败: 帧不是完整 JPEG");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "自检 PASS —— 摄像头可用（已关闭，未占用带宽）");
    return ESP_OK;
}

/*
 * ---------------------------------------------------------------------------
 * 完整调用序列备忘（下次别再从头翻 example）：
 *
 *   bsp_camera_start()            I2C + XCLK + esp_video_init()
 *   open("/dev/video2")           BSP_CAMERA_DEVICE
 *   VIDIOC_S_FMT                  定 JPEG + 分辨率，读回实际值
 *   VIDIOC_S_EXT_CTRLS            V4L2_CID_VFLIP —— BSP 没替你做
 *   VIDIOC_REQBUFS                count=2, MMAP
 *   VIDIOC_QUERYBUF + mmap        拿到缓冲地址
 *   VIDIOC_QBUF                   全部入队
 *   VIDIOC_STREAMON
 *      ├─ VIDIOC_DQBUF            取帧，看 buf.flags & V4L2_BUF_FLAG_DONE
 *      └─ VIDIOC_QBUF             用完必须还
 *   VIDIOC_STREAMOFF
 *   munmap + REQBUFS(count=0) + close
 *
 * 参考实现在 device/managed_components/espressif__esp_video/examples/capture_stream。
 * ---------------------------------------------------------------------------
 */
