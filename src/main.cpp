#include <Arduino.h>
#include <Preferences.h>
#include <WiFi.h>
#include <mqtt_client.h>
#include <esp_chip_info.h>
#include <esp_flash.h>
#include <esp_system.h>
#include <esp_wifi.h>

#include "board_config.h"
#include "device_identity.h"
#include "private_config.h"

namespace {

constexpr uint32_t kSerialBaud = 115200;
constexpr uint32_t kWifiStatusIntervalMs = 200;
constexpr uint32_t kAdcSampleIntervalUs = 5000;
constexpr uint32_t kAdcFrameIntervalMs = 20;
constexpr uint32_t kAdcTestDurationMs = 10000;
// Bench matrix on this assembled purifier controller found that maximum RF
// power repeatedly reached the AP at -59 dBm but failed authentication. 15 dBm
// completed association immediately. The lower setting also reduces supply
// current peaks and sensitivity to a detuned antenna beside the carrier PCB.
constexpr wifi_power_t kWifiTxPower = WIFI_POWER_15dBm;
constexpr int8_t kWifiTxPowerDbm = 15;
String commandBuffer;
Preferences preferences;
uint32_t lastWifiStatusMs = 0;
uint32_t wifiRetryAtMs = 300;
uint8_t wifiRetryFailures = 0;
uint32_t wifiAttemptNumber = 0;
volatile uint32_t wifiDisconnectEventsPending = 0;
volatile uint8_t wifiDisconnectReason = 0;
volatile bool wifiGotIpPending = false;
volatile bool wifiAssociatedPending = false;
portMUX_TYPE wifiEventMux = portMUX_INITIALIZER_UNLOCKED;
enum class WifiPhase : uint8_t { Idle, Connecting, Backoff, RadioRecovery, Online };
WifiPhase wifiPhase = WifiPhase::Idle;
uint32_t wifiPhaseStartedMs = 0;
uint32_t wifiOfflineSinceMs = 0;
uint32_t wifiIgnoreDisconnectUntilMs = 0;
uint32_t wifiDisconnectCount = 0;
uint32_t wifiSuccessfulConnections = 0;
uint32_t wifiRadioRecoveryCount = 0;
String wifiTargetSsid;
String wifiTargetPassword;
uint32_t lastHeartbeatMs = 0;
uint32_t lastMqttTelemetryUs = 0;
uint32_t lastStatusLedMs = 0;
bool statusLedOn = false;
uint8_t ledPatternEdgesRemaining = 0;
bool ledPatternOn = false;
uint16_t ledPatternOnMs = 100;
uint16_t ledPatternOffMs = 100;
uint32_t ledPatternNextMs = 0;
uint32_t telemetryIntervalUs = 100000;
uint32_t telemetrySequence = 0;
uint32_t mqttPublishFailures = 0;
uint32_t mqttTelemetryPublished = 0;
uint32_t mqttTelemetryDropped = 0;
uint32_t mqttPublishMaxUs = 0;
uint32_t remoteRebootAtMs = 0;
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
  // Arduino dispatches these callbacks from its Wi-Fi event task. Never call
  // Serial, Preferences, scan, disconnect or begin here; doing so races the
  // core's own disconnect handler. Transfer only primitive facts to loop().
  portENTER_CRITICAL(&wifiEventMux);
  if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
    wifiDisconnectReason = info.wifi_sta_disconnected.reason;
    ++wifiDisconnectEventsPending;
  } else if (event == ARDUINO_EVENT_WIFI_STA_CONNECTED) {
    wifiAssociatedPending = true;
  } else if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
    wifiGotIpPending = true;
  }
  portEXIT_CRITICAL(&wifiEventMux);
}

constexpr int kOptocouplerPins[] = {kPowerOptocouplerGpio, kModeOptocouplerGpio};
constexpr const char* kOptocouplerLabels[] = {"power", "mode"};
constexpr size_t kOptocouplerCount = sizeof(kOptocouplerPins) / sizeof(kOptocouplerPins[0]);
void blinkLed(uint8_t count, uint16_t onMs, uint16_t offMs);
void setOptocoupler(size_t index, bool active);
void pulseOptocoupler(size_t index);
void connectSavedWifi();
const char* wifiPhaseName();

String topicFor(const char* suffix) { return "air/v1/" + deviceId() + "/" + suffix; }
bool publishMqtt(const String& topic, const String& payload, int qos = 0, bool retain = false) {
  if (!mqttOnline || !mqttClient) return false;
  const uint32_t startedUs = micros();
  // QoS 0 is live data: send it directly and never put it in the persistent
  // outbox. Older esp-mqtt releases can drain stored QoS 0 messages at only
  // one item per second, starving heartbeat and incoming commands. QoS 1
  // state/acks remain queued and reliable.
  const int result = qos == 0
    ? esp_mqtt_client_publish(mqttClient, topic.c_str(), payload.c_str(), payload.length(), 0, retain)
    : esp_mqtt_client_enqueue(mqttClient, topic.c_str(), payload.c_str(), payload.length(), qos, retain, true);
  mqttPublishMaxUs = max(mqttPublishMaxUs, micros() - startedUs);
  if (result < 0) ++mqttPublishFailures;
  return result >= 0;
}
void publishAck(const String& requestId, const char* result) {
  publishMqtt(topicFor("ack"), "{\"request_id\":\"" + requestId + "\",\"result\":\"" + result + "\"}", 1);
}
void publishState() {
  if (!mqttOnline) return;
  char body[576];
  snprintf(body, sizeof(body), "{\"schema\":1,\"device_id\":\"%s\",\"powered\":false,\"mode\":\"unknown\",\"uptime_ms\":%lu,\"adc_mv\":[%lu,%lu,%lu,%lu],\"opto\":{\"power\":%s,\"mode\":%s},\"wifi\":{\"ssid\":\"%s\",\"rssi_dbm\":%d,\"tx_power_dbm\":%d,\"ip\":\"%s\",\"bssid\":\"%s\",\"channel\":%ld,\"disconnects\":%lu,\"radio_recoveries\":%lu},\"mqtt\":{\"publish_failures\":%lu,\"telemetry_published\":%lu,\"telemetry_dropped\":%lu,\"publish_max_us\":%lu}}",
           deviceId().c_str(), static_cast<unsigned long>(millis()), static_cast<unsigned long>(adcMillivolts[0]), static_cast<unsigned long>(adcMillivolts[1]), static_cast<unsigned long>(adcMillivolts[2]), static_cast<unsigned long>(adcMillivolts[3]), optocouplerActive[0] ? "true" : "false", optocouplerActive[1] ? "true" : "false", WiFi.SSID().c_str(), WiFi.RSSI(), kWifiTxPowerDbm, WiFi.localIP().toString().c_str(), WiFi.BSSIDstr().c_str(), static_cast<long>(WiFi.channel()), static_cast<unsigned long>(wifiDisconnectCount), static_cast<unsigned long>(wifiRadioRecoveryCount), static_cast<unsigned long>(mqttPublishFailures), static_cast<unsigned long>(mqttTelemetryPublished), static_cast<unsigned long>(mqttTelemetryDropped), static_cast<unsigned long>(mqttPublishMaxUs));
  publishMqtt(topicFor("state"), body, 1, true);
}
void publishHeartbeat() {
  publishMqtt(topicFor("heartbeat"), "{\"uptime_ms\":" + String(millis()) +
    ",\"rssi_dbm\":" + String(WiFi.RSSI()) + ",\"tx_power_dbm\":" + String(kWifiTxPowerDbm) + ",\"ip\":\"" + WiFi.localIP().toString() +
    "\",\"wifi_phase\":\"" + wifiPhaseName() + "\",\"disconnects\":" + String(wifiDisconnectCount) +
    ",\"radio_recoveries\":" + String(wifiRadioRecoveryCount) +
    ",\"mqtt_publish_failures\":" + String(mqttPublishFailures) +
    ",\"telemetry_dropped\":" + String(mqttTelemetryDropped) + "}");
}
void publishDiagnostic(const String& event) {
  publishMqtt(topicFor("diagnostic"), "{\"uptime_ms\":" + String(millis()) + ",\"event\":\"" + event + "\"}", 1);
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
  else if (type == "opto.power.pulse") { pulseOptocoupler(0); publishAck(requestId, "ok"); }
  else if (type == "opto.mode.pulse") { pulseOptocoupler(1); publishAck(requestId, "ok"); }
  else if (type == "device.reboot") {
    // Give the QoS 1 acknowledgement enough time to leave the MQTT outbox
    // before restarting. The UI can therefore distinguish an accepted reboot
    // from a command that was never received.
    publishAck(requestId, "ok");
    remoteRebootAtMs = millis() + 1500;
  }
  else if (type == "debug.configure") {
    const int requestedHz = constrain(jsonIntField(command, "adc_stream_hz", 0), 0, 120);
    const int requestedDuration = constrain(jsonIntField(command, "expires_in_s", 60), 1, 600);
    telemetryEnabled = requestedHz > 0;
    telemetryIntervalUs = requestedHz ? 1000000UL / static_cast<uint32_t>(requestedHz) : 1000000UL;
    telemetrySequence = 0;
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
  esp_mqtt_client_config_t config{}; config.uri = mqttUri.c_str(); config.client_id = mqttClientId.c_str(); config.username = mqttUser.c_str(); config.password = mqttPassword.c_str(); config.lwt_topic = mqttAvailabilityTopic.c_str(); config.lwt_msg = "offline"; config.lwt_qos = 1; config.lwt_retain = 1; config.keepalive = 10; config.disable_auto_reconnect = false; config.reconnect_timeout_ms = 2000; config.network_timeout_ms = 3000; config.task_prio = 6; config.event_handle = mqttEvent;
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
    case WifiPhase::Connecting: return "associating";
    case WifiPhase::Backoff: return "retry-wait";
    case WifiPhase::RadioRecovery: return "radio-recovery";
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

void completeWifiConnection() {
  wifiRetryFailures = 0;
  wifiRetryAtMs = 0;
  wifiOfflineSinceMs = 0;
  wifiPhase = WifiPhase::Online;
  ++wifiSuccessfulConnections;
  Serial.printf("[PASS] Wi-Fi ready. IP=%s BSSID=%s CH=%ld RSSI=%d dBm successes=%lu disconnects=%lu\n",
                WiFi.localIP().toString().c_str(), WiFi.BSSIDstr().c_str(),
                static_cast<long>(WiFi.channel()), WiFi.RSSI(),
                static_cast<unsigned long>(wifiSuccessfulConnections),
                static_cast<unsigned long>(wifiDisconnectCount));
  emitWifiStatus();
  // Rebuild MQTT for every newly established network session.  The client
  // object may survive a Wi-Fi outage while its socket cannot recover.
  rebuildMqttClient();
}

void connectWifiCredentials(const String& ssid, const String& password) {
  if (ssid.isEmpty()) { Serial.println("[FAIL] Wi-Fi SSID is empty."); return; }
  wifiTargetSsid = ssid;
  wifiTargetPassword = password;
  wifiRetryAtMs = 0;
  // Configure once before connect. ALL_CHANNEL_SCAN lets the IDF driver see
  // every AP sharing this SSID and choose the strongest one itself. Do not run
  // a second application scan while this internal connect scan is active.
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(false);
  WiFi.persistent(false);
  WiFi.setSleep(false);
  esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
  esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20);
  WiFi.setTxPower(kWifiTxPower);
  WiFi.setScanMethod(WIFI_ALL_CHANNEL_SCAN);
  WiFi.setSortMethod(WIFI_CONNECT_AP_BY_SIGNAL);
  ++wifiAttemptNumber;
  Serial.printf("[TEST] Starting driver-managed Wi-Fi association #%lu: %s (TX=%d dBm)\n",
                static_cast<unsigned long>(wifiAttemptNumber), ssid.c_str(), kWifiTxPowerDbm);
  WiFi.begin(wifiTargetSsid.c_str(), wifiTargetPassword.c_str());
  wifiPhase = WifiPhase::Connecting;
  wifiPhaseStartedMs = millis();
  if (!wifiOfflineSinceMs) wifiOfflineSinceMs = millis();
}

void connectSavedWifi() {
  connectWifiCredentials(kFixedWifiSsid, kFixedWifiPassword);
}

void recoverWifiRadio() {
  ++wifiRadioRecoveryCount;
  wifiPhase = WifiPhase::RadioRecovery;
  wifiIgnoreDisconnectUntilMs = millis() + 1500;
  portENTER_CRITICAL(&wifiEventMux);
  wifiDisconnectEventsPending = 0;
  portEXIT_CRITICAL(&wifiEventMux);
  Serial.printf("[WARN] Restarting Wi-Fi radio after repeated failures; recovery=%lu.\n",
                static_cast<unsigned long>(wifiRadioRecoveryCount));
  WiFi.scanDelete();
  WiFi.setAutoReconnect(false);
  WiFi.mode(WIFI_OFF);
  delay(150);
  WiFi.mode(WIFI_STA);
  delay(50);
  wifiRetryFailures = 0;
  connectSavedWifi();
}

void tickWifiConnection() {
  const uint32_t now = millis();
  bool associatedEvent = false;
  bool gotIpEvent = false;
  uint32_t pendingDisconnects = 0;
  uint8_t pendingDisconnectReason = 0;
  portENTER_CRITICAL(&wifiEventMux);
  associatedEvent = wifiAssociatedPending;
  gotIpEvent = wifiGotIpPending;
  pendingDisconnects = wifiDisconnectEventsPending;
  pendingDisconnectReason = wifiDisconnectReason;
  wifiAssociatedPending = false;
  wifiGotIpPending = false;
  wifiDisconnectEventsPending = 0;
  portEXIT_CRITICAL(&wifiEventMux);

  if (associatedEvent) {
    Serial.printf("[INFO] Wi-Fi link associated; BSSID=%s CH=%ld, waiting for DHCP.\n",
                  WiFi.BSSIDstr().c_str(), static_cast<long>(WiFi.channel()));
  }
  if (gotIpEvent ||
      (WiFi.status() == WL_CONNECTED && WiFi.localIP()[0] != 0 && wifiPhase != WifiPhase::Online)) {
    completeWifiConnection();
    return;
  }

  const auto reason = static_cast<wifi_err_reason_t>(pendingDisconnectReason);
  const bool intentionalRadioEvent = reason == WIFI_REASON_ASSOC_LEAVE &&
    static_cast<int32_t>(now - wifiIgnoreDisconnectUntilMs) < 0;
  if (pendingDisconnects && !intentionalRadioEvent) {
    wifiDisconnectCount += pendingDisconnects;
    wifiRetryFailures = min<uint8_t>(wifiRetryFailures + pendingDisconnects, 8);
    if (!wifiOfflineSinceMs) wifiOfflineSinceMs = now;
    mqttOnline = false;
    Serial.printf("WIFI_DISCONNECT reason=%u|%s count=%lu consecutive=%u\n",
                  static_cast<unsigned>(reason), WiFi.disconnectReasonName(reason),
                  static_cast<unsigned long>(wifiDisconnectCount),
                  static_cast<unsigned>(wifiRetryFailures));

    if (wifiRetryFailures >= 8) {
      wifiPhase = WifiPhase::RadioRecovery;
      wifiRetryAtMs = now + 1000;
      Serial.println("[INFO] Wi-Fi radio recovery scheduled in 1000 ms.");
    } else {
      // Arduino-ESP32 performs one compulsory retry after the first failure,
      // even with auto reconnect disabled. Give that attempt time to finish;
      // every later retry is owned exclusively by this state machine.
      const uint32_t retryDelayMs = wifiDisconnectCount == 1
        ? 8000 : min<uint32_t>(6000, 1000UL << min<uint8_t>(wifiRetryFailures, 3));
      wifiRetryAtMs = now + retryDelayMs;
      wifiPhase = WifiPhase::Backoff;
      Serial.printf("[INFO] Driver-managed retry scheduled in %lu ms.\n",
                    static_cast<unsigned long>(retryDelayMs));
    }
  }

  if (wifiPhase == WifiPhase::Online && WiFi.status() != WL_CONNECTED) {
    if (!wifiOfflineSinceMs) wifiOfflineSinceMs = now;
    wifiPhase = WifiPhase::Backoff;
    wifiRetryAtMs = now + 2000;
  }
  if (wifiPhase == WifiPhase::Backoff && static_cast<int32_t>(now - wifiRetryAtMs) >= 0) {
    connectSavedWifi();
    return;
  }
  if (wifiPhase == WifiPhase::RadioRecovery && static_cast<int32_t>(now - wifiRetryAtMs) >= 0) {
    recoverWifiRadio();
    return;
  }
  // A lost driver event must not leave an unattended appliance stuck forever.
  if (wifiPhase == WifiPhase::Connecting && now - wifiPhaseStartedMs >= 20000) {
    Serial.println("[WARN] Wi-Fi connect watchdog expired without a terminal event.");
    recoverWifiRadio();
  }
}

void forgetWifi() {
  wifiIgnoreDisconnectUntilMs = millis() + 1500;
  WiFi.disconnect(false, false);
  wifiPhase = WifiPhase::Backoff;
  wifiRetryFailures = 0;
  wifiRetryAtMs = millis() + 2000;
  Serial.println("[OK] Wi-Fi disconnected; fixed-network reconnect scheduled.");
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
  if (telemetryEnabled && mqttOnline && nowUs - lastMqttTelemetryUs >= telemetryIntervalUs) {
    lastMqttTelemetryUs = nowUs;
    const String payload = "{\"seq\":" + String(telemetrySequence) + ",\"uptime_ms\":" + String(millis()) + ",\"adc_mv\":[" + String(adcMillivolts[0]) + "," + String(adcMillivolts[1]) + "," + String(adcMillivolts[2]) + "," + String(adcMillivolts[3]) + "],\"opto\":{\"power\":" + String(optocouplerActive[0] ? "true" : "false") + ",\"mode\":" + String(optocouplerActive[1] ? "true" : "false") + "}}";
    if (publishMqtt(topicFor("telemetry"), payload)) {
      ++telemetrySequence;
      ++mqttTelemetryPublished;
    } else {
      ++mqttTelemetryDropped;
    }
  }
  if (telemetryEnabled && static_cast<int32_t>(millis() - telemetryExpiresAtMs) >= 0) { telemetryEnabled = false; publishDiagnostic("remote diagnostics expired"); }
  if (remoteRebootAtMs && static_cast<int32_t>(millis() - remoteRebootAtMs) >= 0) {
    remoteRebootAtMs = 0;
    Serial.println("[INFO] Remote reboot accepted; restarting now.");
    Serial.flush();
    ESP.restart();
  }
  finishAdcTestIfReady();
  updateOptocouplerPulses();
  tickStatusLed();
  delay(0);
}
