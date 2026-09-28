/*
 * SPDX-License-Identifier: MIT
 *
 * 板载摄像头（ESP32-S3-EYE 上的 **OV3660**，8-bit DVP 并口）取帧封装。
 *
 * ⚠️ 这块板的摄像头走的是 esp_video —— 一套 Linux V4L2 风格的接口
 * （open("/dev/video2") + ioctl + mmap），**不是**老的 esp_camera 组件。
 * 网上大部分 ESP32 摄像头教程用的是 esp_camera（esp_camera_init / esp_camera_fb_get），
 * 在这块板上照抄会连头文件都找不到。
 *
 * 传感器型号是**实测**出来的（2026-09-24）：`ov3660: Detected Camera sensor PID=0x3660`。
 * 早期文档按 OV2640 写是错的 —— 所以 Kconfig 里三个驱动全开着，让板子自己认：
 *   CONFIG_CAMERA_OV2640 / OV3660 / GC2145 =y，且各自 DVP + AUTO_DETECT 都开
 * 另外**别写死分辨率**：不同型号的档位不一样（OV3660 的 JPEG 只有 1280x720），
 * 要先 G_FMT 问驱动（见 camera.c 里 ② 那段）。
 *
 * 语义与 LED / SD 卡一致：**可选外设**。摄像头不可用只该少一路数据，
 * 绝不能拦住开机，所以调用方一律用 ESP_ERROR_CHECK_WITHOUT_ABORT 接返回值。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 一帧图像。data 指向驱动内部的 mmap 缓冲，**不是**你的内存，别 free。 */
typedef struct {
    uint8_t  *data;   /* JPEG 字节流起点 */
    uint32_t  len;    /* 实际字节数（buf.bytesused，JPEG 是变长的） */
    uint32_t  width;  /* 驱动回读的实际宽度（可能与请求值不同） */
    uint32_t  height;
    uint32_t  seq;    /* 本次上电以来的第几帧，从 1 开始 */
    int       slot;   /* 内部缓冲编号，camera_release() 要用 */
} camera_frame_t;

/**
 * @brief 打开摄像头、定格式、申请并 mmap 缓冲、开始出图。可重复调用（已开则直接返回）。
 *
 * @param[out] out_w 驱动回读的实际宽度，可为 NULL
 * @param[out] out_h 驱动回读的实际高度，可为 NULL
 */
esp_err_t camera_init(uint32_t *out_w, uint32_t *out_h);

/** 摄像头是否处于可用状态（init 成功且未 deinit）。 */
bool camera_ready(void);

/**
 * @brief 取一帧。阻塞等待，超时由内部设定（2 秒）。
 *
 * ⚠️ 用完必须调 camera_release() 归还缓冲，否则缓冲耗尽后永远取不到帧。
 */
esp_err_t camera_capture(camera_frame_t *out);

/** 归还 camera_capture() 取到的帧缓冲。 */
void camera_release(const camera_frame_t *frame);

/** 停止出图、释放缓冲、关闭设备。可重复调用。 */
void camera_deinit(void);

/**
 * @brief 是否让摄像头一直开着。**默认关（用完就关）**。
 *
 * 只有"实时画面"开着时才该置 true —— 那时本来就每秒要取两帧，反复开关更亏。
 * 传 false 会**立刻** deinit，不等下一次取帧。
 *
 * 为什么默认要关：DVP 一旦出图就会持续往 PSRAM 写帧，和 LVGL 显存、IMU 采样、
 * WiFi 抢内存带宽。实测（2026-09-27）拍过一次照之后板子屏幕就永久变卡、重启才恢复，
 * 就是因为 fd 和 STREAMON 一直留着。
 */
void camera_set_keep_open(bool on);

/**
 * @brief 设置 JPEG 画质（1..100，越大越清晰、帧越大）。
 *
 * **这是唯一能改变帧大小的旋钮** —— OV3660 的 JPEG 分辨率只有 1280x720 一档
 * （驱动格式表里 JPEG 仅此一档），所以"调分辨率"做不到，只能调画质。
 * 调低会让每帧字节数变少，上传耗时与带宽同步下降 —— 帧率和流量一起受益。
 * 已打开的设备会**立刻生效**，不必等下一次 init。
 */
void camera_set_quality(int q);

/**
 * @brief 开机自检：拍一帧，把分辨率/字节数/耗时/JPEG 合法性打进串口日志，然后完全关闭。
 *
 * 拍完会 deinit —— 目的是证明硬件和配置可用，不长期占用 DVP 带宽
 * （否则会跟 LVGL 刷新、IMU 上报抢资源）。将来做「按需取图」时再 init 一次即可。
 */
esp_err_t camera_selftest(void);

/**
 * @brief 拍一帧 JPEG 并**推给服务器**（板子是 HTTP 客户端，网页连不到板子本身，
 *        所以实时画面只能由板子 POST 上来、服务器转给网页）。
 *
 * @param base_url  服务器根地址，如 "http://192.168.1.20:8000"
 * @param device    设备名（服务器按它区分多台的画面）
 * @param save      true 表示这一帧要留档（服务端会另存一份给网页的照片列表）
 * @return ESP_OK 成功。失败只记日志，调用方不必处理（画面丢一帧没关系）。
 */
esp_err_t camera_post_frame(const char *base_url, const char *device, bool save);

/**
 * @brief 拍一帧并写到 SD 卡根目录（8.3 文件名，如 CAM0007.JPG）。
 * @param name_out 成功时写入文件名（可传 NULL）
 * @return ESP_OK 成功；没插卡/没开摄像头/写失败都返回错误。
 */
esp_err_t camera_save_to_sd(char *name_out, size_t name_len);

#ifdef __cplusplus
}
#endif
