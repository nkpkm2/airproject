#pragma once

#include <Arduino.h>
#include <WiFi.h>

inline String deviceId() {
  String mac = WiFi.macAddress();
  mac.replace(":", "");
  mac.toLowerCase();
  return "pur-" + mac;
}
