/*
 * SPDX-License-Identifier: MIT
 * See provisioning.h.
 */
#include "provisioning.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_http_client.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "net_config.h"
#include "prov_form.h"
#include "provisioning_page.h"
#include "transport.h"
#include "wifi_link.h"

static const char *TAG = "prov";

#define PROV_MAX_SSID_LEN 32

static httpd_handle_t s_server;
static volatile bool  s_active;
static volatile TickType_t s_last_activity;
static char s_ap_ssid[NET_SSID_MAX];
static char s_ap_pass[12];         /* 8 位数字 + NUL（WPA2 要求 8–63 位），留点余量 */
static bool s_netif_ready;         /* AP netif 只建一次，失败重试时不能重复建 */
static char s_last_result[64];

/* ------------------------------------------------------------------ */
/* 小工具                                                              */
/* ------------------------------------------------------------------ */

static void note_activity(void)
{
    s_last_activity = xTaskGetTickCount();
}

static void send_json(httpd_req_t *req, const char *json)
{
    httpd_resp_set_type(req, "application/json; charset=utf-8");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    httpd_resp_sendstr(req, json);
}

/* JSON 字符串转义：SSID 可能带引号/反斜杠/中文 */
static int json_escape(char *dst, size_t cap, const char *src)
{
    int w = 0;
    for (const unsigned char *p = (const unsigned char *)src; *p != '\0' && (size_t)w + 7 < cap; p++) {
        if (*p == '"' || *p == '\\') {
            dst[w++] = '\\';
            dst[w++] = (char)*p;
        } else if (*p < 0x20) {
            w += snprintf(dst + w, cap - (size_t)w, "\\u%04x", *p);
        } else {
            dst[w++] = (char)*p;      /* UTF-8 直接透传，浏览器认得 */
        }
    }
    dst[w] = '\0';
    return w;
}

/* ------------------------------------------------------------------ */
/* 配网页                                                              */
/* ------------------------------------------------------------------ */

static esp_err_t h_root(httpd_req_t *req)
{
    note_activity();
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_send(req, PROV_PAGE_HTML, HTTPD_RESP_USE_STRLEN);
}

/* GET /scan —— 扫周边 AP，返回 {nets:[{ssid,rssi,open}]} */
static esp_err_t h_scan(httpd_req_t *req)
{
    note_activity();

    wifi_scan_config_t sc = {.show_hidden = false};
    esp_err_t err = esp_wifi_scan_start(&sc, true);   /* 阻塞式，最多几秒 */
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "scan failed: %s", esp_err_to_name(err));
        send_json(req, "{\"nets\":[]}");
        return ESP_OK;
    }

    uint16_t n = 0;
    esp_wifi_scan_get_ap_num(&n);
    if (n > 20) {
        n = 20;                                        /* 页面下拉框不需要更多 */
    }
    wifi_ap_record_t *recs = calloc(n ? n : 1, sizeof(wifi_ap_record_t));
    if (recs == NULL) {
        send_json(req, "{\"nets\":[]}");
        return ESP_OK;
    }
    esp_wifi_scan_get_ap_records(&n, recs);

    /* 手工拼 JSON：cJSON 会把 SSID 里的非 ASCII 转义得很难读，这里直接透传 */
    char *out = malloc(64 + (size_t)n * 140);
    if (out == NULL) {
        free(recs);
        send_json(req, "{\"nets\":[]}");
        return ESP_OK;
    }
    int w = snprintf(out, 64, "{\"nets\":[");
    for (uint16_t i = 0; i < n; i++) {
        char esc[PROV_MAX_SSID_LEN * 6 + 8];
        json_escape(esc, sizeof(esc), (const char *)recs[i].ssid);
        w += snprintf(out + w, 140,
                      "%s{\"ssid\":\"%s\",\"rssi\":%d,\"open\":%s}",
                      i ? "," : "", esc, (int)recs[i].rssi,
                      recs[i].authmode == WIFI_AUTH_OPEN ? "true" : "false");
    }
    snprintf(out + w, 8, "]}");
    free(recs);

    send_json(req, out);
    free(out);
    return ESP_OK;
}

/* GET /testurl?url=http://... —— 当场告诉用户服务器通不通。
 * 省掉"配完了没数据、不知道是 WiFi 还是服务器地址错"的经典排查地狱。 */
static esp_err_t h_testurl(httpd_req_t *req)
{
    note_activity();

    char q[NET_URL_MAX * 3];
    char url[NET_URL_MAX + 32];
    if (httpd_req_get_url_query_str(req, q, sizeof(q)) != ESP_OK ||
        httpd_query_key_value(q, "url", url, sizeof(url)) != ESP_OK) {
        send_json(req, "{\"ok\":false,\"reason\":\"缺少 url 参数\"}");
        return ESP_OK;
    }

    char full[NET_URL_MAX + 48];
    snprintf(full, sizeof(full), "%s/api/latest", url);

    esp_http_client_config_t cfg = {
        .url = full,
        .timeout_ms = 4000,
        .method = HTTP_METHOD_GET,
    };
    esp_http_client_handle_t cli = esp_http_client_init(&cfg);
    if (cli == NULL) {
        send_json(req, "{\"ok\":false,\"reason\":\"地址格式不对\"}");
        return ESP_OK;
    }
    esp_err_t err = esp_http_client_perform(cli);
    const int code = esp_http_client_get_status_code(cli);
    esp_http_client_cleanup(cli);

    char body[128];
    if (err == ESP_OK && code > 0) {
        snprintf(body, sizeof(body), "{\"ok\":true,\"code\":%d}", code);
    } else {
        snprintf(body, sizeof(body),
                 "{\"ok\":false,\"reason\":\"%s\"}", esp_err_to_name(err));
    }
    send_json(req, body);
    return ESP_OK;
}

/* POST /clear —— 清 NVS 并重启，回到"首次上电"状态 */
static esp_err_t h_clear(httpd_req_t *req)
{
    ESP_LOGW(TAG, "clearing config on user request");
    net_config_clear();
    send_json(req, "{\"ok\":true}");
    vTaskDelay(pdMS_TO_TICKS(300));      /* 让响应先发出去 */
    esp_restart();
    return ESP_OK;
}

/* POST /save —— 核心：解析 → 校验 → **先试连** → 成功才写 NVS */
static esp_err_t h_save(httpd_req_t *req)
{
    note_activity();

    char body[512];
    int got = 0;
    if (req->content_len > 0) {
        if (req->content_len >= (int)sizeof(body)) {
            send_json(req, "{\"ok\":false,\"reason\":\"提交内容过长\"}");
            return ESP_OK;
        }
        got = httpd_req_recv(req, body, sizeof(body) - 1);
        if (got <= 0) {
            send_json(req, "{\"ok\":false,\"reason\":\"读取提交内容失败\"}");
            return ESP_OK;
        }
    }
    body[got] = '\0';

    net_config_t base;
    net_config_load(&base);

    net_config_t cfg;
    char err[PROV_ERR_MAX] = "";
    if (!prov_parse_form(body, (size_t)got, &base, &cfg, err, sizeof(err))) {
        ESP_LOGW(TAG, "form rejected: %s", err);
        char out[PROV_ERR_MAX + 32];
        snprintf(out, sizeof(out), "{\"ok\":false,\"reason\":\"%s\"}", err);
        send_json(req, out);
        return ESP_OK;                    /* 400 会让浏览器控制台报警，用 200 + ok:false 更好 */
    }

    /* 关键：先试连。连上了才写 NVS —— 这样用户永远知道失败在哪一步，
     * 也不会把一份连不上的凭据存进去导致下次开机直接失联。 */
    ESP_LOGI(TAG, "trying '%s' ...", cfg.ssid);
    char ip[20] = "";
    const bool linked = wifi_link_try_sta(cfg.ssid, cfg.pass, 15000, ip, sizeof(ip));

    char out[192];
    if (!linked) {
        snprintf(s_last_result, sizeof(s_last_result), "连接失败：密码错误或找不到该网络");
        snprintf(out, sizeof(out),
                 "{\"ok\":false,\"reason\":\"连不上「%s」：密码错误 / 找不到该网络 / 超时。"
                 "配置**没有保存**，可以改完重试。\"}", cfg.ssid);
        send_json(req, out);
        return ESP_OK;                    /* AP 保持开着，允许重试 */
    }

    if (!net_config_save(&cfg)) {
        snprintf(s_last_result, sizeof(s_last_result), "保存配置失败");
        send_json(req, "{\"ok\":false,\"reason\":\"WiFi 连上了，但写入配置失败（NVS 异常）\"}");
        return ESP_OK;
    }

    snprintf(s_last_result, sizeof(s_last_result), "已连接 %s", ip);
    snprintf(out, sizeof(out), "{\"ok\":true,\"ip\":\"%s\"}", ip);
    send_json(req, out);

    /* 响应发完再切模式 —— 立刻切会把手机的连接掐断，用户看不到"成功"提示。
     * 交给 wifi_link 回 STA，AP 由超时看护关掉。 */
    ESP_LOGI(TAG, "provisioned: ssid='%s' ip=%s", cfg.ssid, ip);
    transport_reload_config();       /* 新服务器地址立即生效，不用重启 */
    wifi_link_resume_sta();
    s_active = false;
    return ESP_OK;
}

/* ------------------------------------------------------------------ */
/* 启停                                                                */
/* ------------------------------------------------------------------ */

static esp_err_t start_httpd(void)
{
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.max_uri_handlers = 8;
    cfg.lru_purge_enable = true;          /* 手机可能开多个连接，别把池子占满 */
    cfg.stack_size = 6144;

    esp_err_t err = httpd_start(&s_server, &cfg);
    if (err != ESP_OK) {
        return err;
    }
    const httpd_uri_t uris[] = {
        {.uri = "/",       .method = HTTP_GET,  .handler = h_root},
        {.uri = "/scan",   .method = HTTP_GET,  .handler = h_scan},
        {.uri = "/testurl",.method = HTTP_GET,  .handler = h_testurl},
        {.uri = "/save",   .method = HTTP_POST, .handler = h_save},
        {.uri = "/clear",  .method = HTTP_POST, .handler = h_clear},
    };
    for (size_t i = 0; i < sizeof(uris) / sizeof(uris[0]); i++) {
        err = httpd_register_uri_handler(s_server, &uris[i]);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "register %s failed: %s", uris[i].uri, esp_err_to_name(err));
            return err;
        }
    }
    return ESP_OK;
}

/* AP 名带 MAC 后两字节：一个班 20 块板同时开热点也不撞名 */
static void build_ap_identity(void)
{
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP);
    snprintf(s_ap_ssid, sizeof(s_ap_ssid), "EGO-LINK-%02X%02X", mac[4], mac[5]);

    /* **必须是 8–63 位**：WPA2 的下限是 8，4 位会被 esp_wifi_set_config() 拒绝
     * （ESP_ERR_WIFI_PASSWORD）。2026-09-22 真机实测：原来这里是 "%04u"，
     * 于是配网一启动就 abort → 板子陷入"开机就重启"的死循环，
     * 屏幕都来不及显示，双击 BOOT 也救不回来 —— 等于变砖。
     * 改 8 位数字：手机端还是纯数字键盘、读屏一样快，而且 10^8 比原来的 10^4 更抗猜。
     * （配网页只在 AP 网段内可达，这道密码的作用是"别让同教室的人随手改你板子"，
     *   不是真安全边界。） */
    snprintf(s_ap_pass, sizeof(s_ap_pass), "%08u", (unsigned)(esp_random() % 100000000u));
}

esp_err_t provisioning_start(void)
{
    if (s_active) {
        return ESP_OK;
    }

    build_ap_identity();
    s_last_result[0] = '\0';

    /* netif 只能建一次：失败后用户双击 BOOT 重试时不能再建一遍（会 abort）。 */
    if (!s_netif_ready) {
        esp_netif_create_default_wifi_ap();
        s_netif_ready = true;
    }

    wifi_config_t ap = {0};
    strlcpy((char *)ap.ap.ssid, s_ap_ssid, sizeof(ap.ap.ssid));
    ap.ap.ssid_len = (uint8_t)strlen(s_ap_ssid);
    strlcpy((char *)ap.ap.password, s_ap_pass, sizeof(ap.ap.password));
    ap.ap.max_connection = 4;
    ap.ap.authmode = WIFI_AUTH_WPA2_PSK;
    ap.ap.channel = 1;                    /* 1/6/11 里挑最不挤的；教学场景 1 够用 */

    /* 刻意**不用** ESP_ERROR_CHECK（REVIEW P2-5 的同一类问题）：AP 参数一旦不被
     * 接受，ESP_ERROR_CHECK 会直接 abort，而 provisioning 正是"配置本身有问题时"
     * 才走到的路径 —— 在这里 abort 等于把"参数错"升级成"板子变砖"。
     * 现在改成：记下原因、返回失败、板子保持可操作（双击 BOOT 可重试）。 */
    esp_err_t err = esp_wifi_set_mode(WIFI_MODE_AP);
    if (err == ESP_OK) {
        err = esp_wifi_set_config(WIFI_IF_AP, &ap);
    }
    if (err == ESP_OK) {
        err = esp_wifi_start();
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SoftAP 启动失败: %s（ssid='%s' pass_len=%d）—— 板子保持可操作，"
                 "双击 BOOT 可重试", esp_err_to_name(err), s_ap_ssid,
                 (int)strlen(s_ap_pass));
        snprintf(s_last_result, sizeof(s_last_result), "热点启动失败：%s",
                 esp_err_to_name(err));
        return ESP_FAIL;
    }

    if (start_httpd() != ESP_OK) {
        ESP_LOGE(TAG, "httpd start failed");
        return ESP_FAIL;
    }

    note_activity();
    s_active = true;
    ESP_LOGI(TAG, "=== 配网模式 ===");
    ESP_LOGI(TAG, "AP: %s  密码: %s  打开 http://%s", s_ap_ssid, s_ap_pass, PROV_AP_IP);
    return ESP_OK;
}

void provisioning_stop(void)
{
    if (s_server != NULL) {
        httpd_stop(s_server);
        s_server = NULL;
    }
    s_active = false;
}

bool provisioning_is_active(void)
{
    return s_active;
}

const char *provisioning_ap_ssid(void)
{
    return s_ap_ssid;
}

const char *provisioning_ap_pass(void)
{
    return s_ap_pass;
}

const char *provisioning_last_result(void)
{
    return s_last_result;
}

/* 由 main.c 的看护任务调用：5 分钟没人动就关 AP 回 STA。
 * 避免"忘记关热点"长期占道 + 耗电（PROPOSAL §1.4）。 */
void provisioning_poll_timeout(void)
{
    if (!s_active) {
        return;
    }
    if ((xTaskGetTickCount() - s_last_activity) > pdMS_TO_TICKS(PROV_IDLE_TIMEOUT_MS)) {
        ESP_LOGW(TAG, "配网 5 分钟无操作，自动关闭热点");
        snprintf(s_last_result, sizeof(s_last_result), "配网超时");
        provisioning_stop();
        wifi_link_resume_sta();
    }
}

/* ------------------------------------------------------------------ */
/* 设备端自检                                                          */
/* ------------------------------------------------------------------ */
/*
 * 本环境没有 host C 编译器（clang 是纯交叉、无 wasm 目标、无 host libc），
 * 所以表单解析的边界用例做成**开机自检**在目标板上跑。
 * prov_form.c 本身不含任何 IDF 依赖，有 host 编译器时也可以直接：
 *     cc -I device/main device/main/prov_form.c your_test.c -o t && ./t
 */
typedef struct {
    const char *body;
    const char *base_ssid;
    bool        want_ok;
    const char *want_ssid;
    const char *want_err_kw;   /* want_ok=false 时，err 里应包含的关键字 */
    const char *want_device;   /* want_ok=true 时校验生效设备名；NULL = 不校验 */
} selftest_case_t;

void prov_form_selftest(void)
{
    const selftest_case_t cases[] = {
        /* 正常 */
        {"ssid=Home&pass=12345678&url=http%3A%2F%2F192.168.1.5%3A8000&device=rw1&period=500",
         "", true, "Home", NULL, "rw1"},
        /* 开放网络（密码留空）*/
        {"ssid=Cafe&pass=&url=http://a.b:1", "", true, "Cafe", NULL, NULL},
        /* URL 解码：%2B 应还原成 '+'，'+' 应还原成空格 */
        {"ssid=A%2BB+C&pass=12345678&url=http://h:1", "", true, "A+B C", NULL, NULL},
        /* 缺 SSID（基线也为空）→ 拒绝 */
        {"pass=12345678&url=http://h:1", "", false, NULL, "WiFi 名称", NULL},
        /* 缺 URL → 拒绝 */
        {"ssid=A&pass=12345678&url=", "", false, NULL, "服务器地址", NULL},
        /* URL 没有 scheme → 拒绝 */
        {"ssid=A&pass=12345678&url=192.168.1.5:8000", "", false, NULL, "http", NULL},
        /* 密码太短 → 拒绝 */
        {"ssid=A&pass=123&url=http://h:1", "", false, NULL, "8", NULL},
        /* 周期越界 → 拒绝 */
        {"ssid=A&pass=12345678&url=http://h:1&period=5", "", false, NULL, "100", NULL},
        /* 缺省字段沿用基线（配网页只提交改动项也能工作）。
         * 注意 want_ok 必须是 true：基线里 URL/密码都在，只改 SSID 当然应当通过。
         * 2026-09-22 之前这里写的是 false，于是每次开机都打一行
         * "selftest[8] FAIL" —— 一个纯粹的假警报（真机接上后才看见）。 */
        {"ssid=New", "Old", true, "New", NULL, NULL},
        /* 设备名留空是**允许**的：交给 net_config_device_id() 按 MAC 自动命名。
         * 以前这里兜底填 "rw1"，结果 20 块没改过名的板子在服务端是同一台设备、
         * 姿态球互相覆盖（PROPOSAL §4.1）。 */
        {"ssid=A&pass=12345678&url=http://h:1&device=", "", true, "A", NULL, ""},
        /* 配网页填了名字就用它 */
        {"ssid=A&pass=12345678&url=http://h:1&device=rw1-07", "", true, "A", NULL, "rw1-07"},
    };

    int pass = 0;
    const int total = (int)(sizeof(cases) / sizeof(cases[0]));

    for (int i = 0; i < total; i++) {
        const selftest_case_t *c = &cases[i];
        net_config_t base;
        memset(&base, 0, sizeof(base));
        strlcpy(base.ssid, c->base_ssid, sizeof(base.ssid));
        if (c->base_ssid[0] != '\0') {
            strlcpy(base.url, "http://base:1", sizeof(base.url));
            strlcpy(base.pass, "12345678", sizeof(base.pass));
        }

        net_config_t out;
        char err[PROV_ERR_MAX] = "";
        const bool ok = prov_parse_form(c->body, strlen(c->body), &base, &out, err, sizeof(err));

        bool good = (ok == c->want_ok);
        if (good && ok && c->want_ssid != NULL) {
            good = (strcmp(out.ssid, c->want_ssid) == 0);
        }
        if (good && ok && c->want_device != NULL) {
            good = (strcmp(out.device, c->want_device) == 0);
        }
        if (good && !ok && c->want_err_kw != NULL) {
            good = (strstr(err, c->want_err_kw) != NULL);
        }

        if (good) {
            pass++;
        } else {
            ESP_LOGE(TAG, "selftest[%d] FAIL: body='%s' got ok=%d ssid='%s' device='%s' err='%s'",
                     i, c->body, (int)ok, out.ssid, out.device, err);
        }
    }
    ESP_LOGI(TAG, "prov_form selftest: %d/%d %s", pass, total,
             pass == total ? "PASS" : "FAIL");
}
