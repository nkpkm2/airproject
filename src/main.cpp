#include <Arduino.h>
#include <Preferences.h>
#include <WiFi.h>
#include <mqtt_client.h>
#include <esp_chip_info.h>
#include <esp_flash.h>
#include <esp_system.h>

#include "board_config.h"
#include "device_identity.h"
#include "private_config.h"

namespace {

constexpr uint32_t kSerialBaud = 115200;
constexpr uint32_t kWifiStatusIntervalMs = 200;
constexpr uint32_t kAdcSampleIntervalUs = 5000;
constexpr uint32_t kAdcFrameIntervalMs = 20;
constexpr uint32_t kAdcTestDurationMs = 10000;
String commandBuffer;
Preferences preferences;
uint32_t lastWifiStatusMs = 0;
uint32_t wifiRetryAtMs = 300;
uint8_t wifiRetryFailures = 0;
uint32_t wifiAttemptNumber = 0;
volatile bool wifiDisconnectPending = false;
volatile uint8_t wifiDisconnectReason = 0;
bool wifiBssidRecovery = false;
struct WifiCandidate {
  uint8_t bssid[6];
  int32_t rssi;
  int32_t channel;
};
enum class WifiPhase : uint8_t { Idle, Scanning, Roaming, BssidFallback, Backoff, Online };
WifiPhase wifiPhase = WifiPhase::Idle;
WifiCandidate wifiCandidates[4]{};
size_t wifiCandidateCount = 0;
size_t wifiCandidateIndex = 0;
uint8_t wifiScanRetries = 0;
uint32_t wifiPhaseStartedMs = 0;
String wifiTargetSsid;
String wifiTargetPassword;
uint32_t lastHeartbeatMs = 0;
uint32_t lastMqttTelemetryMs = 0;
uint32_t lastStatusLedMs = 0;
bool statusLedOn = false;
uint8_t ledPatternEdgesRemaining = 0;
bool ledPatternOn = false;
uint16_t ledPatternOnMs = 100;
uint16_t ledPatternOffMs = 100;
uint32_t ledPatternNextMs = 0;
uint32_t telemetryIntervalMs = 100;
esp_mqtt_client_handle_t mqttClient = nullptr;
bool mqttOnline = false;
volatile uint32_t mqttRecoveryAtMs = 0;
uint32_t lastMqttStartMs = 0;
volatile bool mqttAnnouncePending = false;
bool telemetryEnabled = false;
uint32_t telemetryExpiresAtMs = 0;
String mqttUri;
String mqttUser;
String mqttPassword;
String mqttClientId;
String mqttAvailabilityTopic;
String mqttCommandTopic;
uint32_t lastAdcSampleUs = 0;
uint32_t lastAdcFrameMs = 0;
uint32_t adcMillivolts[kAdcChannelCount]{};
uint32_t adcSampleCount = 0;
uint32_t adcReadTimeUs = 0;
uint32_t adcOverrunCount = 0;
uint32_t adcFrameBytes = 0;
bool adcTestActive = false;
uint32_t adcTestStartedMs = 0;
uint32_t adcTestStartSamples = 0;
uint32_t adcTestStartReadTimeUs = 0;
uint32_t adcTestStartOverruns = 0;
uint32_t adcTestStartFrameBytes = 0;
bool optocouplerActive[] = {false, false};
uint32_t optocouplerPulseOffAtMs[] = {0, 0};

void onWifiEvent(arduino_event_id_t event, arduino_event_info_t info) {
  if (event != ARDUINO_EVENT_WIFI_STA_DISCONNECTED) return;
  const auto reason = static_cast<wifi_err_reason_t>(info.wifi_sta_disconnected.reason);
  wifiDisconnectReason = static_cast<uint8_t>(reason);
  wifiDisconnectPending = true;
  Serial.printf("WIFI_DISCONNECT reason=%u|%s\n",
                static_cast<unsigned>(reason), WiFi.disconnectReasonName(reason));
  // Reconnect is scheduled from loop(), never from the Wi-Fi event callback.
  // This keeps the callback fast and prevents overlapping connection attempts.
  if (wifiPhase == WifiPhase::Online && !preferences.getString("ssid", "").isEmpty()) {
    wifiPhase = WifiPhase::Backoff;
    wifiRetryAtMs = millis() + 2000;
  }
}

constexpr int kOptocouplerPins[] = {kPowerOptocouplerGpio, kModeOptocouplerGpio};
constexpr const char* kOptocouplerLabels[] = {"power", "mode"};
constexpr size_t kOptocouplerCount = sizeof(kOptocouplerPins) / sizeof(kOptocouplerPins[0]);
void blinkLed(uint8_t count, uint16_t onMs, uint16_t offMs);
void setOptocoupler(size_t index, bool active);
void pulseOptocoupler(size_t index);
void connectSavedWifi();

String topicFor(const char* suffix) { return "air/v1/" + deviceId() + "/" + suffix; }
void publishMqtt(const String& topic, const String& payload, int qos = 0, bool retain = false) {
  if (mqttOnline && mqttClient) {
    esp_mqtt_client_enqueue(mqttClient, topic.c_str(), payload.c_str(), payload.length(), qos, retain, true);
  }
}
void publishAck(const String& requestId, const char* result) {
  publishMqtt(topicFor("ack"), "{\"request_id\":\"" + requestId + "\",\"result\":\"" + result + "\"}", 1);
}
void publishState() {
  if (!mqttOnline) return;
  char body[256];
  snprintf(body, sizeof(body), "{\"schema\":1,\"device_id\":\"%s\",\"powered\":false,\"mode\":\"unknown\",\"uptime_ms\":%lu,\"adc_mv\":[%lu,%lu,%lu,%lu],\"opto\":{\"power\":%s,\"mode\":%s},\"wifi\":{\"ssid\":\"%s\",\"rssi_dbm\":%d,\"ip\":\"%s\"}}",
           deviceId().c_str(), static_cast<unsigned long>(millis()), static_cast<unsigned long>(adcMillivolts[0]), static_cast<unsigned long>(adcMillivolts[1]), static_cast<unsigned long>(adcMillivolts[2]), static_cast<unsigned long>(adcMillivolts[3]), optocouplerActive[0] ? "true" : "false", optocouplerActive[1] ? "true" : "false", WiFi.SSID().c_str(), WiFi.RSSI(), WiFi.localIP().toString().c_str());
  publishMqtt(topicFor("state"), body, 1, true);
}
void publishHeartbeat() {
  publishMqtt(topicFor("heartbeat"), "{\"uptime_ms\":" + String(millis()) +
    ",\"rssi_dbm\":" + String(WiFi.RSSI()) + ",\"ip\":\"" + WiFi.localIP().toString() + "\"}");
}
void publishDiagnostic(const String& event) {
  publishMqtt(topicFor("diagnostic"), "{\"uptime_ms\":" + String(millis()) + ",\"event\":\"" + event + "\"}");
}
void emitMqttState() {
  if (mqttOnline) Serial.println("MQTT_STATE online");
  else if (preferences.getString("mqtt-uri", "").isEmpty()) Serial.println("MQTT_STATE unconfigured");
  else Serial.println("MQTT_STATE offline");
}
String jsonStringField(const String& json, const char* name) {
  const String key = "\"" + String(name) + "\"";
  const int keyStart = json.indexOf(key);
  if (keyStart < 0) return "";
  const int colon = json.indexOf(':', keyStart + key.length());
  if (colon < 0) return "";
  const int valueStart = json.indexOf('"', colon + 1);
  if (valueStart < 0) return "";
  const int valueEnd = json.indexOf('"', valueStart + 1);
  return valueEnd < 0 ? "" : json.substring(valueStart + 1, valueEnd);
}
int jsonIntField(const String& json, const char* name, int fallback) {
  const String key = "\"" + String(name) + "\"";
  const int keyStart = json.indexOf(key);
  if (keyStart < 0) return fallback;
  const int colon = json.indexOf(':', keyStart + key.length());
  if (colon < 0) return fallback;
  int start = colon + 1;
  while (start < static_cast<int>(json.length()) && (json[start] == ' ' || json[start] == '\t')) ++start;
  int end = start;
  if (json[end] == '-') ++end;
  while (end < static_cast<int>(json.length()) && isDigit(json[end])) ++end;
  return end == start ? fallback : json.substring(start, end).toInt();
}
void handleMqttCommand(const char* data, int length) {
  String command(data, length);
  const String requestId = jsonStringField(command, "request_id");
  const String type = jsonStringField(command, "type");
  if (type == "led.blink") { blinkLed(3, 200, 200); publishAck(requestId, "ok"); }
  else if (type == "opto.power.on") { setOptocoupler(0, true); publishAck(requestId, "ok"); }
  else if (type == "opto.power.off") { setOptocoupler(0, false); publishAck(requestId, "ok"); }
  else if (type == "opto.mode.pulse") { pulseOptocoupler(1); publishAck(requestId, "ok"); }
  else if (type == "debug.configure") {
    const int requestedHz = constrain(jsonIntField(command, "adc_stream_hz", 0), 0, 20);
    const int requestedDuration = constrain(jsonIntField(command, "expires_in_s", 60), 1, 600);
    telemetryEnabled = requestedHz > 0;
    telemetryIntervalMs = requestedHz ? 1000 / requestedHz : 1000;
    telemetryExpiresAtMs = millis() + static_cast<uint32_t>(requestedDuration) * 1000UL;
    publishDiagnostic(telemetryEnabled ? "remote diagnostics enabled" : "remote diagnostics disabled");
    publishAck(requestId, "ok");
  }
  else publishAck(requestId, "unsupported");
}
esp_err_t mqttEvent(esp_mqtt_event_handle_t event) {
  if (event->event_id == MQTT_EVENT_CONNECTED) { mqttOnline = true; mqttRecoveryAtMs = 0; mqttAnnouncePending = true; esp_mqtt_client_subscribe(event->client, mqttCommandTopic.c_str(), 1); Serial.println("MQTT_STATE online"); }
  else if (event->event_id == MQTT_EVENT_DISCONNECTED) { mqttOnline = false; mqttRecoveryAtMs = millis() + 8000; emitMqttState(); }
  else if (event->event_id == MQTT_EVENT_ERROR) { mqttOnline = false; mqttRecoveryAtMs = millis() + 5000; Serial.println("MQTT_STATE error"); }
  else if (event->event_id == MQTT_EVENT_DATA && event->topic && String(event->topic, event->topic_len) == mqttCommandTopic) handleMqttCommand(event->data, event->data_len);
  return ESP_OK;
}
void startMqttIfConfigured() {
  if (mqttClient || WiFi.status() != WL_CONNECTED) return;
  mqttUri = preferences.getString("mqtt-uri", ""); mqttUser = preferences.getString("mqtt-user", ""); mqttPassword = preferences.getString("mqtt-pass", "");
  if (mqttUri.isEmpty()) return;
  mqttAvailabilityTopic = topicFor("availability"); mqttCommandTopic = topicFor("command");
  // esp-mqtt retains the client-id pointer. Do not point it at deviceId(),
  // which returns a temporary String that vanishes at the end of this line.
  mqttClientId = deviceId();
  esp_mqtt_client_config_t config{}; config.uri = mqttUri.c_str(); config.client_id = mqttClientId.c_str(); config.username = mqttUser.c_str(); config.password = mqttPassword.c_str(); config.lwt_topic = mqttAvailabilityTopic.c_str(); config.lwt_msg = "offline"; config.lwt_qos = 1; config.lwt_retain = 1; config.keepalive = 10; config.disable_auto_reconnect = false; config.event_handle = mqttEvent;
  mqttClient = esp_mqtt_client_init(&config);
  if (mqttClient) {
    lastMqttStartMs = millis();
    esp_mqtt_client_start(mqttClient);
    Serial.println("MQTT_STATE connecting");
  }
}

void rebuildMqttClient() {
  mqttOnline = false;
  mqttAnnouncePending = false;
  mqttRecoveryAtMs = 0;
  if (mqttClient) {
    esp_mqtt_client_stop(mqttClient);
    esp_mqtt_client_destroy(mqttClient);
    mqttClient = nullptr;
  }
  startMqttIfConfigured();
}

void setLed(bool on) {
  const uint8_t level = (on != kBoardLedActiveLow) ? HIGH : LOW;
  digitalWrite(kBoardLedGpio, level);
}

void blinkLed(uint8_t count, uint16_t onMs = 100, uint16_t offMs = 100) {
  ledPatternEdgesRemaining = count * 2;
  ledPatternOn = false;
  ledPatternOnMs = onMs;
  ledPatternOffMs = offMs;
  ledPatternNextMs = millis();
}

void tickStatusLed() {
  const uint32_t now = millis();
  if (ledPatternEdgesRemaining) {
    if (static_cast<int32_t>(now - ledPatternNextMs) >= 0) {
      ledPatternOn = !ledPatternOn;
      setLed(ledPatternOn);
      --ledPatternEdgesRemaining;
      ledPatternNextMs = now + (ledPatternOn ? ledPatternOnMs : ledPatternOffMs);
      if (!ledPatternEdgesRemaining) { setLed(false); statusLedOn = false; lastStatusLedMs = now; }
    }
    return;
  }
  const bool connecting = kFixedWifiSsid[0] != '\0';
  if (!connecting) { if (statusLedOn) { statusLedOn = false; setLed(false); } return; }
  // Slow blue blink = scanning / associating. Fast blue blink = Wi-Fi ready.
  // The LED deliberately does not encode purifier mode.
  const uint32_t intervalMs = WiFi.status() == WL_CONNECTED ? 110 : 700;
  if (now - lastStatusLedMs >= intervalMs) {
    lastStatusLedMs = now;
    statusLedOn = !statusLedOn;
    setLed(statusLedOn);
  }
}

void emitOptocouplerStatus() {
  for (size_t i = 0; i < kOptocouplerCount; ++i) {
    Serial.printf("OPTO_STATUS %s|%d|%d\n", kOptocouplerLabels[i],
                  kOptocouplerPins[i], optocouplerActive[i] ? 1 : 0);
  }
}

void setOptocoupler(size_t index, bool active) {
  if (index >= kOptocouplerCount) return;
  optocouplerActive[index] = active;
  const uint8_t level = (active == kOptocouplerActiveHigh) ? HIGH : LOW;
  digitalWrite(kOptocouplerPins[index], level);
  emitOptocouplerStatus();
  publishState();
}

void pulseOptocoupler(size_t index) {
  if (index >= kOptocouplerCount) return;
  setOptocoupler(index, true);
  optocouplerPulseOffAtMs[index] = millis() + 300;
}

void updateOptocouplerPulses() {
  const uint32_t now = millis();
  for (size_t i = 0; i < kOptocouplerCount; ++i) {
    if (optocouplerPulseOffAtMs[i] && static_cast<int32_t>(now - optocouplerPulseOffAtMs[i]) >= 0) {
      optocouplerPulseOffAtMs[i] = 0;
      setOptocoupler(i, false);
    }
  }
}

const char* resetReasonName(esp_reset_reason_t reason) {
  switch (reason) {
    case ESP_RST_POWERON: return "power-on";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "interrupt-watchdog";
    case ESP_RST_TASK_WDT: return "task-watchdog";
    case ESP_RST_WDT: return "other-watchdog";
    case ESP_RST_DEEPSLEEP: return "deep-sleep";
    case ESP_RST_BROWNOUT: return "brownout";
    default: return "other";
  }
}

void printBoardInfo() {
  esp_chip_info_t chip{};
  esp_chip_info(&chip);
  uint32_t flashBytes = 0;
  const esp_err_t flashResult = esp_flash_get_size(nullptr, &flashBytes);

  Serial.println();
  Serial.println("=== ESP32-C3 SUPER MINI BOARD CHECK ===");
  Serial.printf("Firmware       : %s %s\n", __DATE__, __TIME__);
  Serial.printf("Chip model     : %s, revision %d\n", ESP.getChipModel(), chip.revision);
  Serial.printf("CPU frequency  : %u MHz\n", ESP.getCpuFreqMHz());
  Serial.printf("Cores          : %u\n", chip.cores);
  Serial.printf("Flash          : %s%lu bytes\n",
                flashResult == ESP_OK ? "" : "read-error, ",
                static_cast<unsigned long>(flashBytes));
  Serial.printf("Heap free/min  : %lu / %lu bytes\n",
                static_cast<unsigned long>(ESP.getFreeHeap()),
                static_cast<unsigned long>(ESP.getMinFreeHeap()));
  Serial.printf("Sketch used    : %lu bytes\n", static_cast<unsigned long>(ESP.getSketchSize()));
  Serial.printf("Reset reason   : %s (%d)\n", resetReasonName(esp_reset_reason()),
                static_cast<int>(esp_reset_reason()));
  Serial.printf("MAC address    : %s\n", WiFi.macAddress().c_str());
  Serial.printf("Device ID      : %s\n", deviceId().c_str());
  Serial.printf("LED config     : GPIO%d, active %s\n", kBoardLedGpio,
                kBoardLedActiveLow ? "LOW" : "HIGH");
  Serial.printf("Uptime         : %lu ms\n", static_cast<unsigned long>(millis()));
  for (size_t i = 0; i < kAdcChannelCount; ++i) {
    Serial.printf("ADC %-10s : GPIO%d, %lu mV (un-calibrated)\n", kAdcLabels[i],
                  kAdcPins[i], static_cast<unsigned long>(analogReadMilliVolts(kAdcPins[i])));
  }
  Serial.println("=====================================");
}

void scanWifi() {
  Serial.println("[TEST] Wi-Fi scan started...");
  WiFi.mode(WIFI_STA);

  const int count = WiFi.scanNetworks(false, true);
  if (count < 0) {
    Serial.printf("[FAIL] Wi-Fi scan error: %d\n", count);
    blinkLed(6, 60, 60);
    return;
  }

  Serial.printf("[PASS] Wi-Fi radio works; %d network(s) visible.\n", count);
  for (int i = 0; i < count; ++i) {
    const String ssid = WiFi.SSID(i).isEmpty() ? "<hidden>" : WiFi.SSID(i);
    Serial.printf("  %2d. RSSI %4d dBm  CH %2d  %s\n", i + 1, WiFi.RSSI(i),
                  WiFi.channel(i), ssid.c_str());
  }
  WiFi.scanDelete();
  blinkLed(2, 150, 150);
}

String urlDecode(const String& encoded) {
  String decoded;
  for (size_t i = 0; i < encoded.length(); ++i) {
    if (encoded[i] == '%' && i + 2 < encoded.length()) {
      const char hex[] = {encoded[i + 1], encoded[i + 2], '\0'};
      decoded += static_cast<char>(strtol(hex, nullptr, 16));
      i += 2;
    } else if (encoded[i] == '+') {
      decoded += ' ';
    } else {
      decoded += encoded[i];
    }
  }
  return decoded;
}

void emitWifiStatus() {
  if (WiFi.status() == WL_CONNECTED) {
    // This line is consumed by the local graphical test bench. It never includes credentials.
    Serial.printf("WIFI_STATUS %s|%s|%d\n", WiFi.SSID().c_str(),
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
  }
}

void emitWifiState() {
  Serial.printf("WIFI_STATE %d|%s\n", static_cast<int>(WiFi.status()), WiFi.SSID().c_str());
}

const char* wifiPhaseName() {
  switch (wifiPhase) {
    case WifiPhase::Idle: return "waiting";
    case WifiPhase::Scanning: return "scanning";
    case WifiPhase::Roaming: return "associating";
    case WifiPhase::BssidFallback: return "associating-fallback";
    case WifiPhase::Backoff: return "retry-wait";
    case WifiPhase::Online: return "online";
  }
  return "unknown";
}
void emitWifiProgress() {
  const String ssid = WiFi.status() == WL_CONNECTED ? WiFi.SSID() : String(kFixedWifiSsid);
  Serial.printf("WIFI_PROGRESS %s|%s\n", wifiPhaseName(), ssid.c_str());
}

void sampleAdcs() {
  const uint32_t startedUs = micros();
  for (size_t i = 0; i < kAdcChannelCount; ++i) {
    adcMillivolts[i] = analogReadMilliVolts(kAdcPins[i]);
  }
  const uint32_t elapsedUs = micros() - startedUs;
  ++adcSampleCount;
  adcReadTimeUs += elapsedUs;
  if (elapsedUs > kAdcSampleIntervalUs) ++adcOverrunCount;
}

void emitAdcFrame() {
  char frame[96];
  const int written = snprintf(frame, sizeof(frame), "ADC_FRAME %lu|%lu|%lu|%lu\n",
                               static_cast<unsigned long>(adcMillivolts[0]),
                               static_cast<unsigned long>(adcMillivolts[1]),
                               static_cast<unsigned long>(adcMillivolts[2]),
                               static_cast<unsigned long>(adcMillivolts[3]));
  if (written > 0) {
    Serial.print(frame);
    adcFrameBytes += static_cast<uint32_t>(written);
  }
}

void startAdcTest() {
  adcTestActive = true;
  adcTestStartedMs = millis();
  adcTestStartSamples = adcSampleCount;
  adcTestStartReadTimeUs = adcReadTimeUs;
  adcTestStartOverruns = adcOverrunCount;
  adcTestStartFrameBytes = adcFrameBytes;
  Serial.println("[TEST] ADC 5 ms sampling / 20 ms serial-frame test started (10 s).");
}

void finishAdcTestIfReady() {
  if (!adcTestActive || millis() - adcTestStartedMs < kAdcTestDurationMs) return;
  const uint32_t elapsedMs = millis() - adcTestStartedMs;
  const uint32_t samples = adcSampleCount - adcTestStartSamples;
  const uint32_t readUs = adcReadTimeUs - adcTestStartReadTimeUs;
  const uint32_t overruns = adcOverrunCount - adcTestStartOverruns;
  const uint32_t bytes = adcFrameBytes - adcTestStartFrameBytes;
  const float rateHz = elapsedMs ? (samples * 1000.0f / elapsedMs) : 0;
  const float averageReadUs = samples ? (readUs * 1.0f / samples) : 0;
  const float bytesPerSecond = elapsedMs ? (bytes * 1000.0f / elapsedMs) : 0;
  Serial.printf("ADC_TEST samples=%lu|rate_hz=%.1f|read_us=%.1f|overruns=%lu|frame_bps=%.0f\n",
                static_cast<unsigned long>(samples), rateHz, averageReadUs,
                static_cast<unsigned long>(overruns), bytesPerSecond);
  adcTestActive = false;
}

void scheduleWifiBackoff() {
  // One fixed household AP: retry predictably.  The previous 6–60 second
  // exponential wait made a software reset appear stuck after a single miss.
  WiFi.disconnect(false, false);
  wifiBssidRecovery = false;
  wifiRetryFailures = min<uint8_t>(wifiRetryFailures + 1, 4);
  const uint32_t retryDelayMs = min<uint32_t>(12000, 1000UL << wifiRetryFailures);
  wifiRetryAtMs = millis() + retryDelayMs;
  wifiPhase = WifiPhase::Backoff;
  Serial.printf("[INFO] Wi-Fi retry %u scheduled in %lu ms.\n",
                static_cast<unsigned>(wifiRetryFailures), static_cast<unsigned long>(retryDelayMs));
}

bool startWifiRecoveryScan() {
  // After AUTH_EXPIRE the C3 Wi-Fi driver can still own an aborted association.
  // Starting scanNetworks() against that transaction returns WIFI_SCAN_FAILED
  // (-2).  Rebuild STA first, then launch the asynchronous scan.
  WiFi.scanDelete();
  WiFi.disconnect(true, true);
  delay(100);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(false);
  WiFi.persistent(false);
  WiFi.setSleep(false);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  delay(100);
  return WiFi.scanNetworks(true, true) != WIFI_SCAN_FAILED;
}

void beginBssidFallback() {
  if (wifiCandidateIndex >= wifiCandidateCount) { scheduleWifiBackoff(); return; }
  const WifiCandidate& candidate = wifiCandidates[wifiCandidateIndex];
  Serial.printf("[TEST] Trying BSSID %02X:%02X:%02X:%02X:%02X:%02X, channel %ld, RSSI %ld dBm.\n",
                candidate.bssid[0], candidate.bssid[1], candidate.bssid[2], candidate.bssid[3],
                candidate.bssid[4], candidate.bssid[5], static_cast<long>(candidate.channel),
                static_cast<long>(candidate.rssi));
  WiFi.disconnect(false, false);
  WiFi.begin(wifiTargetSsid.c_str(), wifiTargetPassword.c_str(), candidate.channel, candidate.bssid, true);
  wifiPhase = WifiPhase::BssidFallback;
  wifiPhaseStartedMs = millis();
}

void completeWifiConnection() {
  preferences.putString("ssid", wifiTargetSsid);
  preferences.putString("password", wifiTargetPassword);
  wifiRetryFailures = 0;
  wifiBssidRecovery = false;
  wifiRetryAtMs = 0;
  wifiPhase = WifiPhase::Online;
  Serial.printf("[PASS] Wi-Fi connected. IP address: %s\n", WiFi.localIP().toString().c_str());
  emitWifiStatus();
  // Rebuild MQTT for every newly established network session.  The client
  // object may survive a Wi-Fi outage while its socket cannot recover.
  rebuildMqttClient();
}

void connectWifiCredentials(const String& ssid, const String& password) {
  if (ssid.isEmpty()) { Serial.println("[FAIL] Wi-Fi SSID is empty."); return; }
  // A user-selected network becomes the sole future target immediately. This
  // prevents an in-flight failure from resurrecting an older saved network.
  preferences.putString("ssid", ssid);
  preferences.putString("password", password);
  wifiTargetSsid = ssid;
  wifiTargetPassword = password;
  wifiCandidateCount = 0;
  wifiCandidateIndex = 0;
  wifiScanRetries = 0;
  wifiBssidRecovery = false;
  wifiDisconnectPending = false;
  wifiRetryAtMs = 0;
  // Recreate STA before every association.  This avoids retaining a stale
  // station transaction across ESP.restart() / USB CDC reset.  Credentials
  // live in our private configuration and Preferences, not the Wi-Fi driver.
  WiFi.disconnect(true, true);
  delay(40);
  WiFi.mode(WIFI_STA);
  delay(20);
  WiFi.setAutoReconnect(false);
  WiFi.persistent(false);
  WiFi.setSleep(false);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  // One fixed household network: begin association directly. Scanning and
  // BSSID pinning were useful during installation, but can cause a needless
  // driver race and are not part of normal appliance operation.
  ++wifiAttemptNumber;
  Serial.printf("[TEST] Starting fixed Wi-Fi association #%lu: %s\n",
                static_cast<unsigned long>(wifiAttemptNumber), ssid.c_str());
  WiFi.begin(wifiTargetSsid.c_str(), wifiTargetPassword.c_str());
  wifiPhase = WifiPhase::Roaming;
  wifiPhaseStartedMs = millis();
}

void tickWifiConnection() {
  if ((wifiPhase == WifiPhase::Roaming || wifiPhase == WifiPhase::BssidFallback) &&
      WiFi.status() == WL_CONNECTED) { completeWifiConnection(); return; }

  // Wi-Fi callbacks are deliberately write-only.  Consume a real disconnect
  // here, in loop(), so the next operation never overlaps the driver callback.
  if (wifiDisconnectPending && (wifiPhase == WifiPhase::Roaming || wifiPhase == WifiPhase::BssidFallback)) {
    const uint8_t reason = wifiDisconnectReason;
    wifiDisconnectPending = false;
    if (wifiPhase == WifiPhase::BssidFallback) {
      ++wifiCandidateIndex;
      beginBssidFallback();
      return;
    }
    // Authentication expiry is the observed C3/AP failure.  A direct SSID
    // association can choose a poor AP/BSSID; scan only after such a failure
    // and retry the visible matching BSSID explicitly.
    wifiBssidRecovery = true;
    wifiCandidateCount = 0;
    wifiCandidateIndex = 0;
    if (!startWifiRecoveryScan()) {
      Serial.printf("[INFO] Wi-Fi recovery scan could not start after reason %u.\n", static_cast<unsigned>(reason));
      scheduleWifiBackoff();
      return;
    }
    Serial.printf("[INFO] Wi-Fi association ended (reason %u); scanning matching APs for BSSID recovery.\n", static_cast<unsigned>(reason));
    wifiPhase = WifiPhase::Scanning;
    wifiPhaseStartedMs = millis();
    return;
  }
  if (wifiPhase == WifiPhase::Online && WiFi.status() != WL_CONNECTED) {
    wifiPhase = WifiPhase::Backoff;
    wifiRetryAtMs = millis() + 2000;
    return;
  }
  if (wifiPhase == WifiPhase::Backoff && static_cast<int32_t>(millis() - wifiRetryAtMs) >= 0) {
    connectSavedWifi();
    return;
  }
  if (wifiPhase == WifiPhase::Scanning) {
    const int count = WiFi.scanComplete();
    if (count == WIFI_SCAN_RUNNING) return;
    if (count < 0) {
      if (wifiScanRetries++ == 0) {
        Serial.printf("[INFO] Wi-Fi scan transient error: %d; retrying scan.\n", count);
        if (!startWifiRecoveryScan()) {
          scheduleWifiBackoff();
          return;
        }
        wifiPhaseStartedMs = millis();
        return;
      }
      Serial.printf("[FAIL] Wi-Fi scan error: %d\n", count);
      scheduleWifiBackoff();
      return;
    }
    for (int i = 0; i < count && wifiCandidateCount < 4; ++i) {
      if (WiFi.SSID(i) != wifiTargetSsid || WiFi.RSSI(i) < -82) continue;
      memcpy(wifiCandidates[wifiCandidateCount].bssid, WiFi.BSSID(i), 6);
      wifiCandidates[wifiCandidateCount].rssi = WiFi.RSSI(i);
      wifiCandidates[wifiCandidateCount].channel = WiFi.channel(i);
      ++wifiCandidateCount;
    }
    WiFi.scanDelete();
    Serial.printf("[INFO] Recovery scan found %u matching AP(s).\n", static_cast<unsigned>(wifiCandidateCount));
    if (wifiBssidRecovery && wifiCandidateCount > 0) {
      beginBssidFallback();
    } else {
      scheduleWifiBackoff();
    }
    return;
  }
  if (wifiPhase == WifiPhase::Roaming && millis() - wifiPhaseStartedMs >= 15000) {
    Serial.println("[INFO] Fixed-network association timed out.");
    scheduleWifiBackoff();
    return;
  }
  if (wifiPhase == WifiPhase::BssidFallback && millis() - wifiPhaseStartedMs >= 9000) {
    ++wifiCandidateIndex;
    beginBssidFallback();
  }
}

void connectWifi(const String& encodedCredentials) {
  const int divider = encodedCredentials.indexOf('|');
  if (divider < 1) {
    Serial.println("[FAIL] Wi-Fi credentials format is invalid.");
    return;
  }
  connectWifiCredentials(urlDecode(encodedCredentials.substring(0, divider)),
                         urlDecode(encodedCredentials.substring(divider + 1)));
}

void connectSavedWifi() {
  Serial.printf("[INFO] Retrying fixed Wi-Fi network: %s\n", kFixedWifiSsid);
  connectWifiCredentials(kFixedWifiSsid, kFixedWifiPassword);
}

void forgetWifi() {
  WiFi.disconnect(true, true);
  // The production board has one fixed firmware network, so this is only a
  // temporary disconnect; the reconnect state machine resumes automatically.
  preferences.remove("bssid");
  preferences.remove("channel");
  wifiRetryAtMs = 0;
  wifiRetryFailures = 0;
  Serial.println("[OK] Wi-Fi disconnected; fixed-network retry remains enabled.");
}

void printHelp() {
  Serial.println("Commands:");
  Serial.println("  help    - show this list");
  Serial.println("  info    - print chip, flash, memory and reset information");
  Serial.println("  scan    - diagnostic-only scan; normal Wi-Fi is automatic");
  Serial.println("  led on  - turn the configured onboard LED on");
  Serial.println("  led off - turn the configured onboard LED off");
  Serial.println("  blink   - blink the configured onboard LED three times");
  Serial.println("  Wi-Fi is fixed in private_config.h and connects automatically at boot");
  Serial.println("  mqtt <uri>|<user>|<password> - save and start local MQTT");
  Serial.println("  adc test - measure 5 ms ADC sampling and 20 ms serial-frame rate for 10 seconds");
  Serial.println("  opto <power|mode> <on|off|pulse> - test the respective optocoupler; pulse is 300 ms");
  Serial.println("  reboot  - restart the board");
}

void runCommand(String command) {
  command.trim();
  if (command.isEmpty()) return;
  String normalized = command;
  normalized.toLowerCase();

  if (normalized == "help") printHelp();
  else if (normalized == "info") { printBoardInfo(); emitOptocouplerStatus(); emitWifiState(); emitWifiProgress(); emitMqttState(); }
  else if (normalized == "scan") scanWifi();
  else if (normalized == "led on") { setLed(true); Serial.println("[OK] LED on"); }
  else if (normalized == "led off") { setLed(false); Serial.println("[OK] LED off"); }
  else if (normalized == "blink") { blinkLed(3, 200, 200); Serial.println("[OK] Blink complete"); }
  else if (normalized == "wifi forget") forgetWifi();
  else if (normalized.startsWith("mqtt ")) { String value = command.substring(5); int a = value.indexOf('|'), b = value.indexOf('|', a + 1); if (a > 0 && b > a) { preferences.putString("mqtt-uri", value.substring(0,a)); preferences.putString("mqtt-user", value.substring(a+1,b)); preferences.putString("mqtt-pass", value.substring(b+1)); startMqttIfConfigured(); Serial.println("[OK] MQTT configuration saved."); } else Serial.println("[FAIL] MQTT format is uri|user|password."); }
  else if (normalized.startsWith("wifi ")) Serial.println("[INFO] Wi-Fi is fixed in firmware; manual network changes are disabled.");
  else if (normalized == "adc test") startAdcTest();
  else if (normalized == "opto power on") setOptocoupler(0, true);
  else if (normalized == "opto power off") setOptocoupler(0, false);
  else if (normalized == "opto power pulse") pulseOptocoupler(0);
  else if (normalized == "opto mode on") setOptocoupler(1, true);
  else if (normalized == "opto mode off") setOptocoupler(1, false);
  else if (normalized == "opto mode pulse") pulseOptocoupler(1);
  else if (normalized == "reboot") { Serial.println("Restarting..."); Serial.flush(); ESP.restart(); }
  else Serial.printf("Unknown command: %s (type 'help')\n", command.c_str());
}

}  // namespace

void setup() {
  pinMode(kBoardLedGpio, OUTPUT);
  setLed(false);
  for (size_t i = 0; i < kOptocouplerCount; ++i) {
    pinMode(kOptocouplerPins[i], OUTPUT);
    setOptocoupler(i, false);
  }

  Serial.begin(kSerialBaud);
  const uint32_t waitStarted = millis();
  // Networking must start promptly even when USB serial is not attached.
  while (!Serial && millis() - waitStarted < 250) delay(10);

  WiFi.onEvent(onWifiEvent);
  WiFi.mode(WIFI_STA);
  preferences.begin("wifi-config", false);
  analogReadResolution(12);
  for (size_t i = 0; i < kAdcChannelCount; ++i) {
    analogSetPinAttenuation(kAdcPins[i], ADC_11db);
    if (kEnableInternalAdcPulldownForBareBoardTest) {
      pinMode(kAdcPins[i], INPUT_PULLDOWN);
    }
  }
  printBoardInfo();
  Serial.println("[PASS] Firmware booted and USB serial is responding.");
  Serial.printf("[INFO] Fixed Wi-Fi %s will connect automatically in the background.\n", kFixedWifiSsid);
  printHelp();
  Serial.println("Board check ready. Type a command and press Enter.");
}

void loop() {
  while (Serial.available()) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\n' || c == '\r') {
      if (!commandBuffer.isEmpty()) {
        runCommand(commandBuffer);
        commandBuffer.clear();
      }
    } else if (commandBuffer.length() < 192) {
      commandBuffer += c;
    }
  }
  if (WiFi.status() == WL_CONNECTED && millis() - lastWifiStatusMs >= kWifiStatusIntervalMs) {
    lastWifiStatusMs = millis();
    emitWifiStatus();
  }
  if (wifiPhase == WifiPhase::Idle && wifiRetryAtMs != 0 &&
      static_cast<int32_t>(millis() - wifiRetryAtMs) >= 0) connectSavedWifi();
  tickWifiConnection();
  const uint32_t nowUs = micros();
  if (nowUs - lastAdcSampleUs >= kAdcSampleIntervalUs) {
    lastAdcSampleUs = nowUs;
    sampleAdcs();
  }
  if (millis() - lastAdcFrameMs >= kAdcFrameIntervalMs) {
    lastAdcFrameMs = millis();
    emitAdcFrame();
  }
  if (mqttOnline && millis() - lastHeartbeatMs >= 2000) {
    lastHeartbeatMs = millis();
    publishHeartbeat();
    publishState();
  }
  if (mqttOnline && mqttAnnouncePending) {
    mqttAnnouncePending = false;
    publishMqtt(mqttAvailabilityTopic, "online", 1, true);
    publishState();
    lastHeartbeatMs = millis();
  }
  if (!mqttOnline && mqttClient && WiFi.status() == WL_CONNECTED &&
      millis() - lastMqttStartMs >= 10000) {
    Serial.println("[INFO] MQTT watchdog rebuilding offline client.");
    rebuildMqttClient();
  }
  if (telemetryEnabled && mqttOnline && millis() - lastMqttTelemetryMs >= telemetryIntervalMs) {
    lastMqttTelemetryMs = millis();
    publishMqtt(topicFor("telemetry"), "{\"uptime_ms\":" + String(millis()) + ",\"adc_mv\":[" + String(adcMillivolts[0]) + "," + String(adcMillivolts[1]) + "," + String(adcMillivolts[2]) + "," + String(adcMillivolts[3]) + "],\"opto\":{\"power\":" + String(optocouplerActive[0] ? "true" : "false") + ",\"mode\":" + String(optocouplerActive[1] ? "true" : "false") + "}}");
  }
  if (telemetryEnabled && static_cast<int32_t>(millis() - telemetryExpiresAtMs) >= 0) { telemetryEnabled = false; publishDiagnostic("remote diagnostics expired"); }
  finishAdcTestIfReady();
  updateOptocouplerPulses();
  tickStatusLed();
  delay(0);
}
