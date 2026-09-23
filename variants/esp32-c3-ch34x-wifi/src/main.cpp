#include <Arduino.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_chip_info.h>
#include <esp_flash.h>
#include <esp_system.h>

namespace {

constexpr uint32_t kSerialBaud = 115200;
constexpr uint32_t kWifiStatusIntervalMs = 200;
constexpr uint32_t kWifiReconnectIntervalMs = 10000;

Preferences preferences;
String commandBuffer;
uint32_t lastWifiStatusMs = 0;
uint32_t lastWifiReconnectMs = 0;

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

void printBoardInfo() {
  esp_chip_info_t chip{};
  esp_chip_info(&chip);
  uint32_t flashBytes = 0;
  const esp_err_t flashResult = esp_flash_get_size(nullptr, &flashBytes);

  Serial.println();
  Serial.println("=== ESP32-C3 CH34X WIFI CHECK ===");
  Serial.printf("Firmware       : %s %s\n", __DATE__, __TIME__);
  Serial.printf("Chip model     : %s, revision %d\n", ESP.getChipModel(), chip.revision);
  Serial.printf("CPU frequency  : %u MHz\n", ESP.getCpuFreqMHz());
  Serial.printf("Flash          : %s%lu bytes\n",
                flashResult == ESP_OK ? "" : "read-error, ",
                static_cast<unsigned long>(flashBytes));
  Serial.printf("Heap free/min  : %lu / %lu bytes\n",
                static_cast<unsigned long>(ESP.getFreeHeap()),
                static_cast<unsigned long>(ESP.getMinFreeHeap()));
  Serial.printf("Reset reason   : %s (%d)\n", resetReasonName(esp_reset_reason()),
                static_cast<int>(esp_reset_reason()));
  Serial.printf("MAC address    : %s\n", WiFi.macAddress().c_str());
  Serial.println("Peripheral I/O : disabled pending pinout verification");
  Serial.println("===================================");
}

void emitWifiStatus() {
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("WIFI_STATUS %s|%s|%d\n", WiFi.SSID().c_str(),
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
  }
}

void scanWifi() {
  Serial.println("[TEST] Wi-Fi scan started...");
  WiFi.mode(WIFI_STA);
  const int count = WiFi.scanNetworks(false, true);
  if (count < 0) {
    Serial.printf("[FAIL] Wi-Fi scan error: %d\n", count);
    return;
  }
  Serial.printf("[PASS] Wi-Fi radio works; %d network(s) visible.\n", count);
  for (int i = 0; i < count; ++i) {
    const String ssid = WiFi.SSID(i).isEmpty() ? "<hidden>" : WiFi.SSID(i);
    Serial.printf("  %2d. RSSI %4d dBm  CH %2d  %s\n", i + 1, WiFi.RSSI(i),
                  WiFi.channel(i), ssid.c_str());
  }
  WiFi.scanDelete();
}

void configureRadio() {
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
}

bool waitForWifi(uint32_t timeoutMs) {
  const uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < timeoutMs) {
    delay(100);
  }
  return WiFi.status() == WL_CONNECTED;
}

void saveConnection(const String& ssid, const String& password) {
  preferences.putString("ssid", ssid);
  preferences.putString("password", password);
  uint8_t bssid[6];
  memcpy(bssid, WiFi.BSSID(), sizeof(bssid));
  preferences.putBytes("bssid", bssid, sizeof(bssid));
  preferences.putInt("channel", WiFi.channel());
}

void connectWifi(const String& encodedCredentials) {
  const int divider = encodedCredentials.indexOf('|');
  if (divider < 1) {
    Serial.println("[FAIL] Wi-Fi credentials format is invalid.");
    return;
  }
  const String ssid = urlDecode(encodedCredentials.substring(0, divider));
  const String password = urlDecode(encodedCredentials.substring(divider + 1));
  Serial.printf("[TEST] Connecting to Wi-Fi SSID: %s\n", ssid.c_str());

  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);
  delay(300);
  configureRadio();

  int bestIndex = -1;
  int bestRssi = -1000;
  const int count = WiFi.scanNetworks(false, true);
  for (int i = 0; i < count; ++i) {
    if (WiFi.SSID(i) == ssid && WiFi.RSSI(i) > bestRssi) {
      bestIndex = i;
      bestRssi = WiFi.RSSI(i);
    }
  }

  if (bestIndex >= 0) {
    uint8_t bssid[6];
    memcpy(bssid, WiFi.BSSID(bestIndex), sizeof(bssid));
    const int32_t channel = WiFi.channel(bestIndex);
    Serial.printf("[TEST] Trying strongest BSSID, channel %ld, RSSI %d dBm\n",
                  static_cast<long>(channel), bestRssi);
    WiFi.scanDelete();
    WiFi.begin(ssid.c_str(), password.c_str(), channel, bssid, true);
  } else {
    WiFi.scanDelete();
    Serial.println("[INFO] No scan match found; trying SSID-only connection.");
    WiFi.begin(ssid.c_str(), password.c_str());
  }

  if (waitForWifi(15000)) {
    saveConnection(ssid, password);
    Serial.printf("[PASS] Wi-Fi connected. IP address: %s\n",
                  WiFi.localIP().toString().c_str());
    emitWifiStatus();
  } else {
    Serial.printf("[FAIL] Wi-Fi connection failed (status %d). Credentials were not saved.\n",
                  WiFi.status());
  }
}

void reconnectSavedWifi() {
  const String ssid = preferences.getString("ssid", "");
  const String password = preferences.getString("password", "");
  if (ssid.isEmpty()) return;

  configureRadio();
  if (preferences.getBytesLength("bssid") == 6) {
    uint8_t bssid[6];
    preferences.getBytes("bssid", bssid, sizeof(bssid));
    const int32_t channel = preferences.getInt("channel", 0);
    WiFi.begin(ssid.c_str(), password.c_str(), channel, bssid, true);
  } else {
    WiFi.begin(ssid.c_str(), password.c_str());
  }
}

void printHelp() {
  Serial.println("Commands: info, scan, wifi <ssid>|<password>, wifi forget, reboot");
  Serial.println("ADC, optocoupler and LED tests are disabled for this unverified board revision.");
}

void runCommand(String command) {
  command.trim();
  if (command.isEmpty()) return;
  String normalized = command;
  normalized.toLowerCase();
  if (normalized == "help") printHelp();
  else if (normalized == "info") printBoardInfo();
  else if (normalized == "scan") scanWifi();
  else if (normalized == "wifi forget") {
    WiFi.disconnect(true, true);
    preferences.clear();
    Serial.println("[OK] Saved Wi-Fi credentials removed; Wi-Fi disconnected.");
  } else if (normalized.startsWith("wifi ")) connectWifi(command.substring(5));
  else if (normalized == "reboot") {
    Serial.println("Restarting...");
    Serial.flush();
    ESP.restart();
  } else if (normalized == "blink" || normalized == "adc test" || normalized.startsWith("opto ")) {
    Serial.println("[INFO] Peripheral command ignored; this board's pinout is not verified.");
  } else {
    Serial.printf("Unknown command: %s (type 'help')\n", command.c_str());
  }
}

}  // namespace

void setup() {
  Serial.begin(kSerialBaud, SERIAL_8N1, RX, TX);
  delay(300);
  preferences.begin("wifi-config", false);
  configureRadio();
  printBoardInfo();
  Serial.println("[PASS] Firmware booted and CH34x UART is responding.");
  reconnectSavedWifi();
  printHelp();
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

  const uint32_t now = millis();
  if (WiFi.status() == WL_CONNECTED && now - lastWifiStatusMs >= kWifiStatusIntervalMs) {
    lastWifiStatusMs = now;
    emitWifiStatus();
  }
  if (WiFi.status() != WL_CONNECTED && now - lastWifiReconnectMs >= kWifiReconnectIntervalMs) {
    lastWifiReconnectMs = now;
    reconnectSavedWifi();
    Serial.printf("WIFI_STATE %d|%s\n", static_cast<int>(WiFi.status()), WiFi.SSID().c_str());
  }
  delay(0);
}
