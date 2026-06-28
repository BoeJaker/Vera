/* ============================================================================
 * vera_mesh_main.c  —  Vera ESP32 Mesh Node (ESP-IDF reference firmware)
 * ============================================================================
 *
 * Minimal ESP-IDF node: connects to Wi-Fi, enrolls into the Vera mesh and runs
 * an HTTP long-poll loop dispatching jobs. Reference-grade — extend the module
 * handlers (kiosk via LVGL, ESP-MESH uplink reporting via parent_id, etc.).
 * Templated by GET /mesh/firmware?flavor=esp-idf.
 *
 * Build: an esp-idf project (idf.py set-target esp32 && idf.py build flash).
 * Set WIFI_SSID / WIFI_PASS below (or via menuconfig). Needs cJSON (bundled).
 * Wire protocol: see vera/mesh/PROTOCOL.md
 * ========================================================================== */

#include <string.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_system.h"
#include "esp_http_client.h"
#include "nvs_flash.h"
#include "driver/gpio.h"
#include "cJSON.h"

#define SERVER_URL  "{{SERVER_URL}}"     /* e.g. http://192.168.0.138:8000 */
#define MESH_TOKEN  "{{MESH_TOKEN}}"     /* "open" if no shared token      */
#define NODE_ID     "{{NODE_ID}}"

#define WIFI_SSID   "your-ssid"
#define WIFI_PASS   "your-pass"
#define LED_GPIO    2
#define RELAY_GPIO  26

static const char *TAG = "vera_mesh";
static EventGroupHandle_t s_wifi_eg;
#define WIFI_CONNECTED_BIT BIT0
static char g_token[80] = MESH_TOKEN;

/* ── HTTP helpers ──────────────────────────────────────────────────────────── */
typedef struct { char *buf; int len; int cap; } resp_t;

static esp_err_t _ev(esp_http_client_event_t *e){
    if(e->event_id == HTTP_EVENT_ON_DATA && e->user_data){
        resp_t *r = (resp_t*)e->user_data;
        int n = e->data_len; if(r->len + n >= r->cap) n = r->cap - r->len - 1;
        if(n > 0){ memcpy(r->buf + r->len, e->data, n); r->len += n; r->buf[r->len] = 0; }
    }
    return ESP_OK;
}

static int http_send(const char *method, const char *url, const char *body, char *out, int out_cap){
    resp_t r = { .buf = out, .len = 0, .cap = out_cap };
    if(out && out_cap) out[0] = 0;
    esp_http_client_config_t cfg = { .url = url, .event_handler = _ev, .user_data = &r,
                                     .timeout_ms = 30000 };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    esp_http_client_set_method(c, (strcmp(method,"POST")==0)?HTTP_METHOD_POST:HTTP_METHOD_GET);
    if(body){ esp_http_client_set_header(c, "Content-Type", "application/json");
              if(strcmp(g_token,"open")!=0 && g_token[0]) esp_http_client_set_header(c, "X-Mesh-Token", g_token);
              esp_http_client_set_post_field(c, body, strlen(body)); }
    int code = -1;
    if(esp_http_client_perform(c) == ESP_OK) code = esp_http_client_get_status_code(c);
    esp_http_client_cleanup(c);
    return code;
}

/* ── Telemetry / result ────────────────────────────────────────────────────── */
static void send_result(const char *job_id, const char *status, cJSON *result, const char *err){
    cJSON *d = cJSON_CreateObject();
    cJSON_AddStringToObject(d, "kind", "result");
    cJSON_AddStringToObject(d, "node_id", NODE_ID);
    cJSON_AddStringToObject(d, "job_id", job_id);
    cJSON_AddStringToObject(d, "status", status);
    if(result) cJSON_AddItemToObject(d, "result", result);
    if(err && err[0]) cJSON_AddStringToObject(d, "error", err);
    char *s = cJSON_PrintUnformatted(d);
    char url[256]; snprintf(url, sizeof url, "%s/mesh/result", SERVER_URL);
    http_send("POST", url, s, NULL, 0);
    printf("%s\n", s);                       /* announce on serial too */
    free(s); cJSON_Delete(d);
}

static void send_telemetry(void){
    cJSON *d = cJSON_CreateObject(), *m = cJSON_CreateObject();
    cJSON_AddStringToObject(d, "kind", "telemetry");
    cJSON_AddStringToObject(d, "node_id", NODE_ID);
    cJSON_AddNumberToObject(m, "heap", esp_get_free_heap_size());
    cJSON_AddNumberToObject(m, "uptime", esp_log_timestamp()/1000);
    cJSON_AddItemToObject(d, "metrics", m);
    char *s = cJSON_PrintUnformatted(d);
    char url[256]; snprintf(url, sizeof url, "%s/mesh/telemetry", SERVER_URL);
    http_send("POST", url, s, NULL, 0); printf("%s\n", s);
    free(s); cJSON_Delete(d);
}

/* ── Job dispatch ──────────────────────────────────────────────────────────── */
static void run_job(cJSON *job){
    const char *id   = cJSON_GetStringValue(cJSON_GetObjectItem(job, "job_id"));
    const char *type = cJSON_GetStringValue(cJSON_GetObjectItem(job, "type"));
    cJSON *p = cJSON_GetObjectItem(job, "payload");
    if(!type) return;
    if(!strcmp(type, "identify")){
        for(int i=0;i<10;i++){ gpio_set_level(LED_GPIO, i&1); vTaskDelay(pdMS_TO_TICKS(120)); }
        send_result(id, "done", NULL, "");
    } else if(!strcmp(type, "reboot")){
        send_result(id, "done", NULL, ""); vTaskDelay(pdMS_TO_TICKS(200)); esp_restart();
    } else if(!strcmp(type, "read_sensor")){
        send_telemetry(); send_result(id, "done", NULL, "");
    } else if(!strcmp(type, "control_set")){
        cJSON *v = p ? cJSON_GetObjectItem(p, "value") : NULL;
        int on = v && (cJSON_IsTrue(v) || (cJSON_IsNumber(v) && v->valueint) ||
                       (cJSON_IsString(v) && (!strcmp(v->valuestring,"1")||!strcmp(v->valuestring,"on"))));
        gpio_set_level(RELAY_GPIO, on);
        send_result(id, "done", NULL, "");
    } else if(!strcmp(type, "io_set")){
        cJSON *pin = p ? cJSON_GetObjectItem(p, "pin") : NULL; int g = pin ? pin->valueint : LED_GPIO;
        cJSON *v = p ? cJSON_GetObjectItem(p, "value") : NULL;
        int on = v && (cJSON_IsTrue(v) || (cJSON_IsNumber(v) && v->valueint) ||
                       (cJSON_IsString(v) && (!strcmp(v->valuestring,"1")||!strcmp(v->valuestring,"on"))));
        gpio_set_direction(g, GPIO_MODE_OUTPUT); gpio_set_level(g, on);
        send_result(id, "done", NULL, "");
    } else if(!strcmp(type, "io_read")){
        cJSON *pin = p ? cJSON_GetObjectItem(p, "pin") : NULL; int g = pin ? pin->valueint : LED_GPIO;
        gpio_set_direction(g, GPIO_MODE_INPUT); int val = gpio_get_level(g);
        cJSON *res = cJSON_CreateObject(); cJSON_AddNumberToObject(res, "pin", g);
        cJSON_AddNumberToObject(res, "value", val);
        send_result(id, "done", res, "");
    } else if(!strcmp(type, "web_fetch")){
        const char *url = p ? cJSON_GetStringValue(cJSON_GetObjectItem(p, "url")) : NULL;
        static char body[512]; int code = url ? http_send("GET", url, NULL, body, sizeof body) : -1;
        cJSON *res = cJSON_CreateObject(); cJSON_AddNumberToObject(res, "status_code", code);
        send_result(id, code>0?"done":"error", res, code>0?"":"fetch failed");
    } else {
        send_result(id, "error", NULL, "unknown type");
    }
}

/* ── Enroll + long-poll ────────────────────────────────────────────────────── */
static void enroll(void){
    cJSON *d = cJSON_CreateObject();
    cJSON_AddStringToObject(d, "kind", "hello");
    cJSON_AddStringToObject(d, "node_id", NODE_ID);
    cJSON_AddStringToObject(d, "name", NODE_ID);
    cJSON_AddStringToObject(d, "board", "esp32");
    cJSON_AddStringToObject(d, "fw", "1.0-idf");
    cJSON *mods = cJSON_AddArrayToObject(d, "modules");
    cJSON_AddItemToArray(mods, cJSON_CreateString("sensor"));
    cJSON_AddItemToArray(mods, cJSON_CreateString("web_fetch"));
    cJSON_AddItemToArray(mods, cJSON_CreateString("control"));
    char *s = cJSON_PrintUnformatted(d);
    char url[256], resp[1024];
    snprintf(url, sizeof url, "%s/mesh/hello", SERVER_URL);
    http_send("POST", url, s, resp, sizeof resp);
    printf("%s\n", s); free(s); cJSON_Delete(d);
    cJSON *r = cJSON_Parse(resp);
    if(r){ cJSON *tok = cJSON_GetObjectItem(r, "token");
           if(cJSON_IsString(tok)) strncpy(g_token, tok->valuestring, sizeof g_token - 1);
           cJSON_Delete(r); }
}

static void poll_task(void *arg){
    char *resp = malloc(4096);
    uint32_t last_tele = 0;
    enroll();
    while(1){
        char url[256];
        snprintf(url, sizeof url, "%s/mesh/poll?node_id=%s&wait=25&token=%s", SERVER_URL, NODE_ID, g_token);
        if(http_send("GET", url, NULL, resp, 4096) == 200){
            cJSON *d = cJSON_Parse(resp);
            if(d){ cJSON *jobs = cJSON_GetObjectItem(d, "jobs"), *job;
                   cJSON_ArrayForEach(job, jobs) run_job(job);
                   cJSON_Delete(d); }
        }
        if(esp_log_timestamp()/1000 - last_tele > 30){ send_telemetry(); last_tele = esp_log_timestamp()/1000; }
        vTaskDelay(pdMS_TO_TICKS(200));
    }
}

/* ── Wi-Fi ─────────────────────────────────────────────────────────────────── */
static void wifi_ev(void *a, esp_event_base_t base, int32_t id, void *data){
    if(base == WIFI_EVENT && id == WIFI_EVENT_STA_START) esp_wifi_connect();
    else if(base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) esp_wifi_connect();
    else if(base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) xEventGroupSetBits(s_wifi_eg, WIFI_CONNECTED_BIT);
}

static void wifi_init(void){
    s_wifi_eg = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_ev, NULL, NULL);
    esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_ev, NULL, NULL);
    wifi_config_t wc = { .sta = { .ssid = WIFI_SSID, .password = WIFI_PASS } };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    xEventGroupWaitBits(s_wifi_eg, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
    ESP_LOGI(TAG, "wifi connected");
}

void app_main(void){
    ESP_ERROR_CHECK(nvs_flash_init());
    gpio_set_direction(LED_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_direction(RELAY_GPIO, GPIO_MODE_OUTPUT);
    wifi_init();
    xTaskCreate(poll_task, "vera_poll", 8192, NULL, 5, NULL);
}
