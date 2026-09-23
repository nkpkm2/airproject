#pragma once

// ESP32-C3 Super Mini boards from different vendors are not fully standardized.
// Override these values in platformio.ini if your board uses another LED pin.
#ifndef BOARD_LED_GPIO
#define BOARD_LED_GPIO 8
#endif

#ifndef BOARD_LED_ACTIVE_LOW
#define BOARD_LED_ACTIVE_LOW 1
#endif

constexpr int kBoardLedGpio = BOARD_LED_GPIO;
constexpr bool kBoardLedActiveLow = BOARD_LED_ACTIVE_LOW != 0;

// Confirmed purifier board mapping. Sensor order follows the physical purifier modes.
// GPIO4=low, GPIO3=medium, GPIO1=high, GPIO0=auto.
constexpr int kAdcPins[] = {4, 3, 1, 0};
constexpr const char* kAdcLabels[] = {"low", "medium", "high", "auto"};
constexpr size_t kAdcChannelCount = sizeof(kAdcPins) / sizeof(kAdcPins[0]);
constexpr bool kEnableInternalAdcPulldownForBareBoardTest = false;

// PC817 optocoupler control inputs. Both outputs start inactive at boot.
constexpr int kPowerOptocouplerGpio = 5;
constexpr int kModeOptocouplerGpio = 7;
constexpr bool kOptocouplerActiveHigh = true;
