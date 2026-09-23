#include <Arduino.h>
#include <WiFi.h>

#include "private_config.h"

namespace {

struct Candidate {
  uint8_t bssid[6]{};
  int32_t rssi = -127;
  int32_t channel = 0;
  wifi_auth_mode_t auth = WIFI_AUTH_MAX;
};

Candidate candidates[8];
size_t candidateCount = 0;
size_t candidateIndex = 0;
uint32_t attemptStartedMs = 0;
uint32_t nextActionMs = 0;
volatile bool disconnected = false;
volatile bool gotIp = false;
volatile uint8_t disconnectReason = 0;

void wifiEvent(arduino_event_id_t event, arduino_event_info_t info) {
  if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
    disconnectReason = info.wifi_sta_disconnected.reason;
    disconnected = true;
  } else if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
    gotIp = true;
  }
}

void printBssid(const uint8_t* bssid) {
  Serial.printf("%02X:%02X:%02X:%02X:%02X:%02X", bssid[0], bssid[1], bssid[2], bssid[3], bssid[4], bssid[5]);
}

void scanCandidates() {
  WiFi.setAutoReconnect(false);
  WiFi.disconnect(false, false);
  delay(250);
  WiFi.scanDelete();
  Serial.println("[ISO] Starting foreground all-channel scan.");
  const int found = WiFi.scanNetworks(false, true);
  candidateCount = 0;
  for (int i = 0; i < found && candidateCount < 8; ++i) {
    if (WiFi.SSID(i) != kFixedWifiSsid) continue;
    Candidate& candidate = candidates[candidateCount++];
    memcpy(candidate.bssid, WiFi.BSSID(i), 6);
    candidate.rssi = WiFi.RSSI(i);
    candidate.channel = WiFi.channel(i);
    candidate.auth = static_cast<wifi_auth_mode_t>(WiFi.encryptionType(i));
    Serial.printf("[ISO] candidate=%u rssi=%ld channel=%ld auth=%u bssid=",
                  static_cast<unsigned>(candidateCount - 1), static_cast<long>(candidate.rssi),
                  static_cast<long>(candidate.channel), static_cast<unsigned>(candidate.auth));
    printBssid(candidate.bssid);
    Serial.println();
  }
  WiFi.scanDelete();
  for (size_t i = 0; i < candidateCount; ++i) {
    for (size_t j = i + 1; j < candidateCount; ++j) {
      if (candidates[j].rssi > candidates[i].rssi) {
        Candidate temporary = candidates[i]; candidates[i] = candidates[j]; candidates[j] = temporary;
      }
    }
  }
  candidateIndex = 0;
  Serial.printf("[ISO] Scan complete: visible=%d matching=%u.\n", found, static_cast<unsigned>(candidateCount));
}

void connectCandidate() {
  if (!candidateCount) {
    nextActionMs = millis() + 3000;
    return;
  }
  if (candidateIndex >= candidateCount) {
    scanCandidates();
    if (!candidateCount) { nextActionMs = millis() + 3000; return; }
  }
  Candidate& candidate = candidates[candidateIndex];
  Serial.printf("[ISO] Connecting candidate=%u rssi=%ld channel=%ld bssid=",
                static_cast<unsigned>(candidateIndex), static_cast<long>(candidate.rssi),
                static_cast<long>(candidate.channel));
  printBssid(candidate.bssid);
  Serial.println();
  disconnected = false;
  gotIp = false;
  WiFi.begin(kFixedWifiSsid, kFixedWifiPassword, candidate.channel, candidate.bssid, true);
  attemptStartedMs = millis();
  nextActionMs = 0;
}

}  // namespace

void setup() {
  Serial.begin(115200);
  const uint32_t waitStarted = millis();
  while (!Serial && millis() - waitStarted < 1000) delay(10);
  Serial.println("[ISO] Minimal ESP32-C3 Wi-Fi isolation firmware booted.");
  WiFi.onEvent(wifiEvent);
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  WiFi.setSleep(false);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  WiFi.setAutoReconnect(false);
  scanCandidates();
  connectCandidate();
}

void loop() {
  if (gotIp || WiFi.status() == WL_CONNECTED) {
    gotIp = false;
    Serial.printf("[ISO-PASS] connected ip=%s rssi=%d channel=%ld bssid=%s\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI(),
                  static_cast<long>(WiFi.channel()), WiFi.BSSIDstr().c_str());
    delay(1000);
    return;
  }
  if (disconnected) {
    disconnected = false;
    const auto reason = static_cast<wifi_err_reason_t>(disconnectReason);
    Serial.printf("[ISO-FAIL] reason=%u|%s candidate=%u elapsed_ms=%lu\n",
                  static_cast<unsigned>(reason), WiFi.disconnectReasonName(reason),
                  static_cast<unsigned>(candidateIndex),
                  static_cast<unsigned long>(millis() - attemptStartedMs));
    ++candidateIndex;
    nextActionMs = millis() + 1500;
  }
  if (!nextActionMs && millis() - attemptStartedMs >= 15000) {
    Serial.printf("[ISO-FAIL] watchdog candidate=%u\n", static_cast<unsigned>(candidateIndex));
    ++candidateIndex;
    nextActionMs = millis() + 1500;
  }
  if (nextActionMs && static_cast<int32_t>(millis() - nextActionMs) >= 0) connectCandidate();
  delay(1);
}
