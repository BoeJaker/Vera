/* ============================================================================
 * vera_mesh_node.ino  —  Vera ESP32 Mesh Node (Arduino reference firmware)
 * ============================================================================
 *
 * Enrolls into a Vera mesh and runs configurable modules. Talks to Vera over
 * HTTP long-poll (always), and prints/accepts the same JSON envelopes over the
 * USB serial line so the browser (Web Serial) or the server (pyserial) can also
 * drive it. Templated by GET /mesh/firmware?flavor=arduino — {{SERVER_URL}},
 * {{MESH_TOKEN}} and {{NODE_ID}} are filled in when you download it.
 *
 * Modules (compile-time toggles below): sensor, web_fetch, watch, alert,
 * kiosk (TFT_eSPI), control (GPIO/relay).
 *
 * Libraries: WiFi, HTTPClient (bundled with the ESP32 core), ArduinoJson,
 * Preferences. For the kiosk module: TFT_eSPI (define USE_TFT).
 *
 * Wire protocol: see vera/mesh/PROTOCOL.md.
 * ========================================================================== */

#include <WiFi.h>
#include <HTTPClient.h>
#include <HTTPUpdate.h>      // OTA "program over Wi-Fi"
#include <ArduinoJson.h>
#include <Preferences.h>

// ── Module toggles ──────────────────────────────────────────────────────────
#define MOD_SENSOR    1
#define MOD_WEB_FETCH 1
#define MOD_WATCH     1
#define MOD_ALERT     1
#define MOD_CONTROL   1
// #define USE_TFT          // uncomment + install TFT_eSPI for the kiosk module

// ── Pins (adjust to your board) ─────────────────────────────────────────────
#define LED_PIN     2        // onboard LED (identify / alert)
#define BUZZER_PIN  -1       // set to a GPIO for an active buzzer, else -1
#define SENSOR_ADC  34       // an analog sensor (e.g. LDR / pot) for demo telemetry
#define RELAY_PIN   26       // a relay / output for the control module

// ── Server config (templated on download) ──────────────────────────────────
String SERVER = "{{SERVER_URL}}";          // e.g. http://192.168.0.138:8000
String TOKEN  = "{{MESH_TOKEN}}";          // "open" if no shared token
String NODE   = "{{NODE_ID}}";

// ── State ───────────────────────────────────────────────────────────────────
Preferences prefs;
String wifiSsid, wifiPass;
String nodeToken = "";                       // per-node token issued at enroll
unsigned long lastTelemetry = 0;
unsigned long telemetryEvery = 30000;        // ms — overridden by sensor config
int  watchEvery = 0; String watchUrl = "";   // watch module config
unsigned long lastWatch = 0;
String workerTasksJson = "[]";               // worker module: recurring edge tasks
unsigned long workerLast[8] = {0};

#ifdef USE_TFT
#include <TFT_eSPI.h>
TFT_eSPI tft = TFT_eSPI();
#endif

// ── Helpers ─────────────────────────────────────────────────────────────────
String chipId(){ uint64_t m=ESP.getEfuseMac(); char b[20]; sprintf(b,"%04X%08X",(uint16_t)(m>>32),(uint32_t)m); return String(b); }

void emitSerial(const JsonDocument& doc){ serializeJson(doc, Serial); Serial.println(); }

// POST helper; returns response body ("" on failure)
String httpPost(const String& path, const String& body){
  if(WiFi.status()!=WL_CONNECTED) return "";
  HTTPClient http; http.begin(SERVER+path); http.addHeader("Content-Type","application/json");
  if(TOKEN.length() && TOKEN!="open") http.addHeader("X-Mesh-Token", TOKEN);
  int code = http.POST(body); String out = (code>0)? http.getString() : "";
  http.end(); return out;
}

// ── Enroll ──────────────────────────────────────────────────────────────────
void enroll(){
  StaticJsonDocument<512> d;
  d["kind"]="hello"; d["node_id"]=NODE; d["name"]=NODE; d["board"]="esp32";
  d["fw"]="1.0"; d["mac"]=WiFi.macAddress(); d["ip"]=WiFi.localIP().toString();
  d["rssi"]=WiFi.RSSI(); if(TOKEN.length()&&TOKEN!="open") d["token"]=TOKEN;
  JsonArray mods=d.createNestedArray("modules");
#if MOD_SENSOR
  mods.add("sensor");
#endif
#if MOD_WEB_FETCH
  mods.add("web_fetch");
#endif
#if MOD_WATCH
  mods.add("watch");
#endif
#if MOD_ALERT
  mods.add("alert");
#endif
#ifdef USE_TFT
  mods.add("kiosk");
#endif
#if MOD_CONTROL
  mods.add("control");
#endif
  mods.add("io"); mods.add("worker");           // always: generic IO + autonomous edge tasks
  JsonArray ch=d.createNestedArray("channels"); ch.add("http"); ch.add("serial");
  String body; serializeJson(d,body);
  emitSerial(d);                                 // also announce over USB serial
  String resp = httpPost("/mesh/hello", body);
  if(resp.length()){
    StaticJsonDocument<1024> r; if(!deserializeJson(r,resp)){
      if(r.containsKey("token")){ nodeToken=r["token"].as<String>(); TOKEN=nodeToken; }
      if(r["heartbeat"].is<int>()) {/* could adjust poll cadence */}
      applyConfig(r["config"]);
    }
  }
}

// ── Apply server config (per-module settings) ───────────────────────────────
void applyConfig(JsonVariant cfg){
  if(cfg.isNull()) return;
  if(cfg["sensor"]["interval_s"].is<int>())
    telemetryEvery = (unsigned long)cfg["sensor"]["interval_s"].as<int>()*1000UL;
  if(cfg["watch"]["url"].is<const char*>()) watchUrl = cfg["watch"]["url"].as<String>();
  if(cfg["watch"]["interval_s"].is<int>()) watchEvery = cfg["watch"]["interval_s"].as<int>()*1000;
  if(cfg["worker"]["tasks"].is<JsonArray>()){ workerTasksJson=""; serializeJson(cfg["worker"]["tasks"], workerTasksJson);
    for(int i=0;i<8;i++) workerLast[i]=0; }
#ifdef USE_TFT
  if(cfg["kiosk"]["brightness"].is<int>()) {/* set backlight PWM here */}
#endif
}

// ── Telemetry ───────────────────────────────────────────────────────────────
void sendTelemetry(){
  StaticJsonDocument<512> d; d["kind"]="telemetry"; d["node_id"]=NODE; d["rssi"]=WiFi.RSSI();
  JsonObject m = d.createNestedObject("metrics");
#if MOD_SENSOR
  m["adc"]    = analogRead(SENSOR_ADC);
  m["heap"]   = (int)ESP.getFreeHeap();
  m["uptime"] = (int)(millis()/1000);
#endif
  String body; serializeJson(d,body);
  emitSerial(d);
  httpPost("/mesh/telemetry", body);
}

// ── Result reporting ────────────────────────────────────────────────────────
void sendResult(const String& jobId, const char* status, JsonVariant result, const String& err){
  StaticJsonDocument<1024> d; d["kind"]="result"; d["node_id"]=NODE; d["job_id"]=jobId;
  d["status"]=status; if(!result.isNull()) d["result"]=result; if(err.length()) d["error"]=err;
  String body; serializeJson(d,body); emitSerial(d); httpPost("/mesh/result", body);
}

// ── Job dispatch ────────────────────────────────────────────────────────────
void runJob(JsonObject job){
  String id   = job["job_id"].as<String>();
  String type = job["type"].as<String>();
  JsonObject p = job["payload"].as<JsonObject>();
  StaticJsonDocument<512> res; JsonVariant rv = res.to<JsonVariant>();

  if(type=="identify"){
    for(int i=0;i<10;i++){ digitalWrite(LED_PIN,!digitalRead(LED_PIN)); delay(120);}
    sendResult(id,"done",rv,"");
  }
  else if(type=="reboot"){ sendResult(id,"done",rv,""); delay(200); ESP.restart(); }
  else if(type=="read_sensor"){ sendTelemetry(); sendResult(id,"done",rv,""); }
  else if(type=="config"){ applyConfig(p["config"]); sendResult(id,"done",rv,""); }
  else if(type=="io_set"){
    int pin = p["pin"].as<int>(); String mode = p["mode"].as<String>();
    if(mode=="pwm"){ analogWrite(pin, p["value"].as<int>()); }
    else { pinMode(pin, OUTPUT);
      String v = p["value"].as<String>(); bool on = (v=="1"||v=="on"||v=="true");
      if(v=="toggle") on = !digitalRead(pin);
      digitalWrite(pin, on?HIGH:LOW); }
    res["pin"]=pin; sendResult(id,"done",rv,"");
  }
  else if(type=="io_read"){
    int pin = p["pin"].as<int>(); bool analog = p["analog"].as<bool>();
    int val = analog ? analogRead(pin) : (pinMode(pin,INPUT), digitalRead(pin));
    res["pin"]=pin; res["value"]=val;
    // also push as telemetry so it charts
    StaticJsonDocument<128> td; td["kind"]="telemetry"; td["node_id"]=NODE;
    td["metrics"][String("pin")+pin]=val; String tb; serializeJson(td,tb); httpPost("/mesh/telemetry",tb);
    sendResult(id,"done",rv,"");
  }
  else if(type=="ota"){
    String mode = p["mode"].as<String>(); String url = p["url"].as<String>();
    if(url.startsWith("/")) url = SERVER + url;
    if(mode=="file"){ sendResult(id,"error",rv,"file OTA is MicroPython-only; use a .bin for Arduino"); }
    else {
      sendResult(id,"done",rv,"OTA starting");           // report before we reboot
      WiFiClient client; t_httpUpdate_return r = httpUpdate.update(client, url);
      if(r==HTTP_UPDATE_FAILED) sendResult(id,"error",rv,String("OTA failed: ")+httpUpdate.getLastErrorString());
      // HTTP_UPDATE_OK reboots automatically into the new image
    }
  }
#if MOD_WEB_FETCH
  else if(type=="web_fetch"){
    String url=p["url"].as<String>(); HTTPClient h; h.begin(url);
    int code=h.GET(); String b=(code>0)?h.getString():""; h.end();
    res["status_code"]=code; res["len"]=b.length(); res["body"]=b.substring(0,400);
    sendResult(id, code>0?"done":"error", rv, code>0?"":"fetch failed");
  }
#endif
#if MOD_CONTROL
  else if(type=="control_set"){
    String v=p["value"].as<String>();
    bool on = (v=="1"||v=="on"||v=="true");
    if(v=="toggle") on=!digitalRead(RELAY_PIN);
    digitalWrite(RELAY_PIN, on?HIGH:LOW); res["channel"]=p["channel"]; res["value"]=on;
    sendResult(id,"done",rv,"");
  }
#endif
#if MOD_ALERT
  else if(type=="alert"){
    String msg=p["message"].as<String>();
    for(int i=0;i<6;i++){ digitalWrite(LED_PIN,HIGH);
      if(BUZZER_PIN>=0) digitalWrite(BUZZER_PIN,HIGH); delay(90);
      digitalWrite(LED_PIN,LOW); if(BUZZER_PIN>=0) digitalWrite(BUZZER_PIN,LOW); delay(90);}
#ifdef USE_TFT
    tft.fillScreen(TFT_RED); tft.setCursor(6,10); tft.setTextColor(TFT_WHITE); tft.print(msg);
#endif
    sendResult(id,"done",rv,"");
  }
#endif
#ifdef USE_TFT
  else if(type=="kiosk_set"){
    tft.fillScreen(TFT_BLACK); tft.setCursor(6,10); tft.setTextColor(TFT_GREEN);
    if(p.containsKey("text")) tft.print(p["text"].as<String>());
    else if(p.containsKey("url")){ tft.print("URL:\n"); tft.print(p["url"].as<String>()); }
    sendResult(id,"done",rv,"");
  }
#endif
  else { sendResult(id,"error",rv,String("unknown type ")+type); }
}

// ── Long-poll ───────────────────────────────────────────────────────────────
void pollOnce(){
  if(WiFi.status()!=WL_CONNECTED) return;
  HTTPClient http;
  String url = SERVER+"/mesh/poll?node_id="+NODE+"&wait=25";
  if(TOKEN.length()&&TOKEN!="open") url += "&token="+TOKEN;
  http.begin(url); http.setTimeout(30000);
  int code=http.GET();
  if(code==200){
    String body=http.getString(); StaticJsonDocument<4096> d;
    if(!deserializeJson(d,body)){
      for(JsonObject job : d["jobs"].as<JsonArray>()) runJob(job);
    }
  }
  http.end();
}

// ── Worker module — run recurring edge tasks set via mesh.worker.assign ─────
void runWorkerTasks(){
  StaticJsonDocument<1024> d;
  if(deserializeJson(d, workerTasksJson)) return;
  int i=0;
  for(JsonObject t : d.as<JsonArray>()){
    if(i>=8) break;
    unsigned long iv=(unsigned long)t["interval_s"].as<int>()*1000UL; if(iv==0) iv=60000;
    if(millis()-workerLast[i]>=iv){
      workerLast[i]=millis();
      StaticJsonDocument<256> job;
      job["job_id"]=String("w")+i+String(millis());
      job["type"]=t["type"]; job["payload"]=t["payload"];
      runJob(job.as<JsonObject>());                 // reuse the dispatcher
    }
    i++;
  }
}

// ── Serial provisioning (Web Serial / pyserial) ─────────────────────────────
void handleSerialInput(){
  if(!Serial.available()) return;
  String line=Serial.readStringUntil('\n'); line.trim(); if(!line.length()) return;
  StaticJsonDocument<512> d; if(deserializeJson(d,line)) return;
  if(d["cmd"]=="provision"){
    wifiSsid=d["ssid"].as<String>(); wifiPass=d["pass"].as<String>();
    if(d.containsKey("server")) SERVER=d["server"].as<String>();
    if(d.containsKey("node_id")) NODE=d["node_id"].as<String>();
    prefs.begin("vera",false); prefs.putString("ssid",wifiSsid); prefs.putString("pass",wifiPass);
    prefs.putString("server",SERVER); prefs.putString("node",NODE); prefs.end();
    WiFi.begin(wifiSsid.c_str(), wifiPass.c_str());
  } else if(d["kind"]=="poll" || d["jobs"].is<JsonArray>()){
    for(JsonObject job : d["jobs"].as<JsonArray>()) runJob(job);   // jobs delivered over serial
  }
}

// ── Setup / loop ────────────────────────────────────────────────────────────
void setup(){
  Serial.begin(115200); delay(200);
  pinMode(LED_PIN,OUTPUT);
  if(BUZZER_PIN>=0) pinMode(BUZZER_PIN,OUTPUT);
#if MOD_CONTROL
  pinMode(RELAY_PIN,OUTPUT);
#endif
#ifdef USE_TFT
  tft.init(); tft.setRotation(1); tft.fillScreen(TFT_BLACK);
#endif
  prefs.begin("vera",true);
  wifiSsid=prefs.getString("ssid",""); wifiPass=prefs.getString("pass","");
  if(prefs.getString("server","").length()) SERVER=prefs.getString("server",SERVER);
  if(prefs.getString("node","").length())   NODE=prefs.getString("node",NODE);
  prefs.end();
  if(NODE=="{{NODE_ID}}"||NODE.length()==0) NODE="esp32-"+chipId();

  if(wifiSsid.length()) WiFi.begin(wifiSsid.c_str(), wifiPass.c_str());
  unsigned long t0=millis();
  while(WiFi.status()!=WL_CONNECTED && millis()-t0<12000){ handleSerialInput(); delay(200); }
  if(WiFi.status()==WL_CONNECTED) enroll();
}

void loop(){
  handleSerialInput();
  if(WiFi.status()==WL_CONNECTED){
    pollOnce();                                  // blocks up to ~25s (long-poll)
    if(millis()-lastTelemetry>telemetryEvery){ sendTelemetry(); lastTelemetry=millis(); }
#if MOD_WATCH
    if(watchEvery>0 && watchUrl.length() && millis()-lastWatch>(unsigned long)watchEvery){
      HTTPClient h; h.begin(watchUrl); int c=h.GET(); h.end(); lastWatch=millis();
      if(c<200||c>=400){ StaticJsonDocument<128> a; a["kind"]="telemetry"; a["node_id"]=NODE;
        a["metrics"]["watch_fail"]=c; String b; serializeJson(a,b); httpPost("/mesh/telemetry",b); }
    }
#endif
    runWorkerTasks();                            // autonomous edge tasks
  } else {
    if(wifiSsid.length()) WiFi.begin(wifiSsid.c_str(), wifiPass.c_str());
    delay(1500);
  }
}
