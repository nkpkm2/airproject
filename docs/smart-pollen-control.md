# Pollen-aware smart control

## Purpose and operating boundary

The feature in the family overview is an opt-in advisory/actuation controller for Cupressaceae pollen (including cypress/Chinese arborvitae). It is paused by default. It only issues a physical power-button pulse or one mode-button pulse at a time after the device has a passed four-channel light calibration, an explicit weak-pollen baseline, and a fresh online state. Manual commands remain independent. Devices with unknown/contradictory lamps, no calibration, hidden catalog entries, or a stale heartbeat are skipped.

The current public global pollen API queried during implementation does **not** provide Cupressaceae pollen in China. Weather is therefore only bounded contextual evidence. An operator enters the local official pollen level, the forecast's expected peak time, and its source URL in the UI; Baidu/social discussion is a capped corroboration field, not a scraped or authenticated feed. This avoids claiming inaccessible or unrelated data as a cypress measurement. Public Open-Meteo weather is fetched every 15 minutes after coordinates are configured and is attributed in the UI.

## Inputs and score

The controller calculates an explainable 0–100 score:

- Official/local Cupressaceae forecast is primary: level 0/1/2/3 starts at 0/12/45/72 points.
- Forecast/current rain, temperature, cloud cover and sunshine can change the score by at most 12 points. Rain lowers airborne-pollen likelihood; warm, dry, sunny conditions raise it modestly. Several sunny days cause only a small bounded reduction.
- Social/allergy chatter adds at most 12 points. It cannot on its own produce a high or extreme decision.
- Configured annual local season adds 4 monitoring points only. It never claims that pollen was measured.
- An official expected peak within the configured lead window requests one extra tier of pre-ramp.
- The configured 17:10 window (40 minutes by default) raises the score to at least “normal.”
- After high/extreme risk, a linear tail contributes up to 18 points over the configured indoor residual period (18 h by default). Lower risk is accepted only after a 90-minute dwell.

Score bands are weak `<18`, normal `18–41`, high `42–67`, extreme `≥68`; weather/social/season alone are prevented from classifying extreme. Upward transitions are immediate; downshifts wait 90 minutes. The outdoor pollen signal is not a direct indoor sensor, so results are recommendations, not medical advice.

## Fan target and physical actuation

Each selected purifier has a user-defined weak-pollen baseline (`off`, low, medium, high, auto). An `off` baseline is never automatically powered on. Explicit fan speeds scale only within low → medium → high; auto remains auto because its actual airflow is appliance-controlled and is not guaranteed to be higher. Controller aggressiveness limits the upward step: normal at most +1, high +2, extra at most +3 (bounded by high). Weak returns to the configured baseline.

The room estimate uses 160 m² × 2.5 m = 400 m³. With 23 devices and the supplied *nominal airflow* of 20/70/150 m³/h per device, aggregate estimates are 460/1,610/3,450 m³/h, or approximately 1.15/4.03/8.63 nominal air-volume turnovers per hour. 15× and 20× would require 6,000 and 8,000 m³/h. These are not CADR: filter efficiency, installation, leakage, and actual airflow are not known, so the UI explicitly avoids presenting airflow turnover as certified clean-air delivery.

For safety, the controller never sends a burst of mode taps. It queues at most one smart action for a device, waits for command acknowledgement and the expected lamp transition, then proceeds after a cooldown. If a mode does not change within 12 seconds, it records the failure and retries cautiously; three consecutive failures pause that device until settings are saved again. A manually issued MQTT command pauses smart actions for that purifier for 30 minutes. Power changes use the GPIO5 momentary pulse (300 ms), not a held optocoupler output.

## Settings required

1. Enter home coordinates and timezone for local weather context.
2. Enter the locality's cypress pollen season, official forecast level, source URL, and expected peak time when available.
3. Optionally enter a verified trend level and short evidence note from Baidu Index or public allergy reports; do not put account credentials here.
4. Set each commissioned purifier's weak-season baseline. Only calibrated devices can be selected for automatic control.
5. Choose normal/high/extra aggressiveness and review the capacity estimate before enabling.

## Validation and remaining integration

`test_smart_control.py` covers room-volume arithmetic, location gating, evidence caps, rain effects, peak pre-ramp, target speed limits, and one-step physical actuation. The `dev_server.py` tests cover the API/domain layer. Firmware must be rebuilt and exercised on a bench before smart-control enablement on a real purifier. Direct authenticated Baidu Index and social-platform APIs are not available in this repository; the current implementation intentionally accepts operator-verified levels instead of scraping logged-in or unstable pages.
