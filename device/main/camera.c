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
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/time.h>
#include <unistd.h>

#include "bsp/esp-bsp.h"
/* BSP 把 BSP_CAMERA_DEVICE 定义成 ESP_VIDEO_DVP_DEVICE_NAME，但 bsp/esp32_s3_eye.h
 * **只用了这个宏、没有 include 定义它的头文件**（BSP 自己的 bsp_camera.c 里包含了，
 * 头文件里漏了）。不补这一行，编译会报 'ESP_VIDEO_DVP_DEVICE_NAME' undeclared。 */
#include "esp_video_device.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_video_ioctl.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "linux/v4l2-controls.h"
#include "linux/videodev2.h"

static const char *TAG = "camera";

/* 必须是 Kconfig 里**开过**的档位。当前开的是 JPEG 320x240（见 sdkconfig.bsp.esp32_s3_eye）。
 * OV2640 只会输出它配置表里存在的组合，填一个没开过的分辨率，S_FMT 会直接失败。 */
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
static uint32_t  s_height;
static bool      s_started;
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

    /* ① BSP 负责 I2C + 16MHz XCLK + esp_video_init()（注册 /dev/video2） */
    bsp_camera_cfg_t cfg = {0};
    esp_err_t ret = bsp_camera_start(&cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "bsp_camera_start 失败: %s", esp_err_to_name(ret));
        return ret;
    }

    s_fd = open(BSP_CAMERA_DEVICE, O_RDONLY);
    if (s_fd < 0) {
        /* 两种原因长得很像，但排查方向完全相反，所以这里要把话分开说清：
         *   上面有 "failed to detect DVP camera" → 驱动是开着的，是**传感器没响应**
         *     （硬件：FPC 排线没插紧 / 模组故障）。判据：同一 I2C 总线上加速度计
         *     正常的话，总线本身没问题，问题在摄像头这一端。
         *   上面什么都没有 → CONFIG_CAMERA_OV2640 没开，驱动压根没编进来。 */
        ESP_LOGE(TAG, "open %s 失败：若上面有 'failed to detect DVP camera'，"
                      "是传感器没响应（检查摄像头 FPC 排线/模组）；"
                      "否则检查 CONFIG_CAMERA_OV2640 是否开启", BSP_CAMERA_DEVICE);
        return ESP_FAIL;
    }

    /* ② 定格式：JPEG 320x240。驱动可能调整请求值，所以要读回实际值 */
    struct v4l2_format fmt = {0};
    fmt.type                = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    fmt.fmt.pix.width       = CAM_WIDTH;
    fmt.fmt.pix.height      = CAM_HEIGHT;
    fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_JPEG;
    if (ioctl(s_fd, VIDIOC_S_FMT, &fmt) != 0) {
        ESP_LOGE(TAG, "VIDIOC_S_FMT 失败（JPEG %dx%d 这个档位开了吗？）", CAM_WIDTH, CAM_HEIGHT);
        close_all();
        return ESP_FAIL;
    }
    s_width  = fmt.fmt.pix.width;
    s_height = fmt.fmt.pix.height;

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
    out->len    = b.bytesused;
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
