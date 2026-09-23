#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>

#include "private_config.h"

namespace {

struct Profile {
  const char* name;
  uint8_t protocol;
  wifi_power_t power;
};

constexpr Profile profiles[] = {
  {"bgn-max-ht20", WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N, WIFI_POWER_19_5dBm},
  {"bg-max-ht20", WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G, WIFI_POWER_19_5dBm},
  {"gn-max-ht20", WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N, WIFI_POWER_19_5dBm},
  {"bgn-15dbm-ht20", WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N, WIFI_POWER_15dBm},
};
constexpr size_t profileCount = sizeof(profiles) / sizeof(profiles[0]);

uint8_t targetBssid[6]{};
int32_t targetChannel = 0;
int32_t targetRssi = -127;
size_t profileIndex = 0;
uint8_t attemptsInProfile = 0;
uint8_t warmupFailures = 0;
bool warmup = true;
bool finished = false;
volatile bool disconnected = false;
volatile bool gotIp = false;
volatile uint8_t disconnectReason = 0;
volatile bool ignoreIntentionalDisconnect = false;
uint32_t attemptStartedMs = 0;
uint32_t nextAttemptMs = 0;

void wifiEvent(arduino_event_id_t event, arduino_event_info_t info) {
  if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
    if (ignoreIntentionalDisconnect && info.wifi_sta_disconnected.reason == WIFI_REASON_ASSOC_LEAVE) {
      ignoreIntentionalDisconnect = false;
      return;
    }
    disconnectReason = info.wifi_sta_disconnected.reason;
    disconnected = true;
  } else if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
    gotIp = true;
  }
}

void startAttempt() {
  if (profileIndex >= profileCount) {
    finished = true;
    Serial.println("[MATRIX] complete");
    return;
  }
  const Profile& profile = profiles[profileIndex];
  esp_wifi_set_protocol(WIFI_IF_STA, profile.protocol);
  esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20);
  WiFi.setTxPower(profile.power);
  disconnected = false;
  gotIp = false;
  ++attemptsInProfile;
  Serial.printf("[MATRIX] start profile=%s attempt=%u target_rssi=%ld channel=%ld\n",
                profile.name, static_cast<unsigned>(attemptsInProfile),
                static_cast<long>(targetRssi), static_cast<long>(targetChannel));
  WiFi.begin(kFixedWifiSsid, kFixedWifiPassword, targetChannel, targetBssid, true);
  attemptStartedMs = millis();
  nextAttemptMs = 0;
}

void advanceProfile() {
  attemptsInProfile = 0;
  ++profileIndex;
  nextAttemptMs = millis() + 1500;
}

}  // namespace

void setup() {
  Serial.begin(115200);
  const uint32_t waitStarted = millis();
  while (!Serial && millis() - waitStarted < 1000) delay(10);
  Serial.println("[MATRIX] ESP32-C3 Wi-Fi protocol/power matrix booted.");
  WiFi.onEvent(wifiEvent);
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(false);
  WiFi.disconnect(false, false);
  delay(250);
  const int found = WiFi.scanNetworks(false, true);
  for (int i = 0; i < found; ++i) {
    if (WiFi.SSID(i) == kFixedWifiSsid && WiFi.RSSI(i) > targetRssi) {
      memcpy(targetBssid, WiFi.BSSID(i), 6);
      targetRssi = WiFi.RSSI(i);
      targetChannel = WiFi.channel(i);
    }
  }
  WiFi.scanDelete();
  Serial.printf("[MATRIX] strongest target rssi=%ld channel=%ld bssid=%02X:%02X:%02X:%02X:%02X:%02X\n",
                static_cast<long>(targetRssi), static_cast<long>(targetChannel), targetBssid[0], targetBssid[1],
                targetBssid[2], targetBssid[3], targetBssid[4], targetBssid[5]);
  if (!targetChannel) { Serial.println("[MATRIX] no matching AP"); finished = true; return; }
  Serial.println("[MATRIX] warmup begins; first Arduino-core retry is intentionally left alone.");
  startAttempt();
}

void loop() {
  if (finished) { delay(100); return; }
  if (gotIp || WiFi.status() == WL_CONNECTED) {
    gotIp = false;
    Serial.printf("[MATRIX-PASS] profile=%s attempt=%u ip=%s rssi=%d\n",
                  profiles[profileIndex].name, static_cast<unsigned>(attemptsInProfile),
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
    ignoreIntentionalDisconnect = true;
    advanceProfile();
    WiFi.disconnect(false, false);
    delay(1);
  }
  if (disconnected) {
    disconnected = false;
    const auto reason = static_cast<wifi_err_reason_t>(disconnectReason);
    Serial.printf("[MATRIX-FAIL] profile=%s attempt=%u reason=%u|%s elapsed_ms=%lu\n",
                  profiles[profileIndex].name, static_cast<unsigned>(attemptsInProfile),
                  static_cast<unsigned>(reason), WiFi.disconnectReasonName(reason),
                  static_cast<unsigned long>(millis() - attemptStartedMs));
    if (warmup) {
      if (++warmupFailures >= 2) {
        warmup = false;
        attemptsInProfile = 0;
        nextAttemptMs = millis() + 1500;
        Serial.println("[MATRIX] Arduino compulsory first retry consumed; controlled matrix begins.");
      }
      // After the first failure Arduino-ESP32 owns one compulsory retry. Do
      // not call begin until its second failure has arrived.
    } else if (attemptsInProfile >= 3) {
      advanceProfile();
    } else {
      nextAttemptMs = millis() + 1500;
    }
  }
  if (!warmup && !nextAttemptMs && millis() - attemptStartedMs > 12000) {
    Serial.printf("[MATRIX-FAIL] profile=%s attempt=%u reason=watchdog\n",
                  profiles[profileIndex].name, static_cast<unsigned>(attemptsInProfile));
    if (attemptsInProfile >= 3) advanceProfile(); else nextAttemptMs = millis() + 1500;
  }
  if (nextAttemptMs && static_cast<int32_t>(millis() - nextAttemptMs) >= 0) startAttempt();
  delay(1);
}
