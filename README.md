# Oral-B Live

A Home Assistant custom integration for Oral-B Bluetooth toothbrushes that
brings **live brushing data back** on recent iO-series firmwares.

## Why this exists

Recent iO firmware (observed on an iO Series with the iO Sense charger,
mid-2026) no longer broadcasts live session data in its BLE
advertisements. During a brushing session the brush is nearly silent on
the air; it only emits a single *post-brushing summary* advertisement
(state `10`) carrying the final duration. The official `oralb`
integration is a passive listener, so with these firmwares it can no
longer show the live timer, pressure, or sector — and depending on
timing (e.g. the Oral-B phone app winning the connection), even the
end-of-session summary can be lost.

GATT reconnaissance showed that the live data still exists — it just
moved from broadcasts to **notifications on a connection**:

| Characteristic (a0f0ffXX) | Content | Behavior |
| --- | --- | --- |
| `ff04` | toothbrush state | notify on change |
| `ff06` | pressure event | notify (high-pressure pulses) |
| `ff07` | brushing mode | notify on change |
| `ff08` | brushing time (s) | **notify at 1 Hz while running** |
| `ff09` | sector | notify on change |
| `ff05` | status blob, byte 0 = battery % | read |

No pairing/bonding is required. There is also a ~30 Hz motion telemetry
stream (`ff0b`/`ff0d`) used by the app's zone-coverage tracking — not
consumed by this integration (yet).

## How it works

**Hybrid passive + active:**

- *Passive:* listens to advertisements (manufacturer id `0xDC`) like the
  official integration — zero connection cost while the brush sleeps.
- *Active:* as soon as an advertisement shows an awake state, the
  integration connects (via any connectable Bluetooth path, including
  ESPHome Bluetooth proxies with `active: true`) and subscribes to the
  notification characteristics above. The live 1 Hz timer, pressure,
  mode and sector flow straight into the entities.
- *Polite:* if the connection cannot be won — typically because the
  Oral-B phone app or the iO Sense charger holds it — it degrades to
  passive listening instead of fighting. When the brush goes back to
  charging/sleep, the connection is released promptly so other clients
  can sync.

## Entities

Mirrors the official integration so existing dashboards and toothbrush
cards keep working:

- Toothbrush state (`running`, `charging`, `selection_menu`,
  `post_brushing_summary`, …) — with `live_connection` and `rssi`
  attributes
- Time (seconds, live at 1 Hz during sessions when connected)
- Pressure (`normal` / `high`)
- Mode
- Sector / Number of sectors
- Battery

## Installation

1. HACS → Integrations → ⋮ → *Custom repositories* → add
   `https://github.com/thomasgregg/oralb-ha` as type *Integration*.
2. Install **Oral-B Live**, restart Home Assistant.
3. Disable (or delete) the official Oral-B config entry for your brush
   to avoid duplicate entities and connection competition.
4. The brush is auto-discovered; or add it via *Settings → Devices &
   Services → Add integration → Oral-B Live* (wake the brush first).

## Requirements

- A connectable Bluetooth path near the brush. An ESPHome Bluetooth
  proxy works great — make sure `bluetooth_proxy: active: true` is set.
- For best reception, run the proxy's scanner continuously:

  ```yaml
  esp32_ble_tracker:
    scan_parameters:
      interval: 320ms
      window: 320ms
      continuous: true
  ```

## Known limitations

- If the Oral-B phone app is running with Bluetooth on, it may win the
  connection race; the integration then falls back to passive data for
  that session (on recent firmware this may mean only the end-of-session
  summary).
- Session *history* download (the `ff8x` command channel used by the
  app) is not implemented yet.
- Mode/state tables contain best-effort mappings; unknown values are
  exposed as `mode_<n>` / `unknown_state_<n>`. Issue reports with the
  raw values are welcome.

## Credits

- [bkbilly/oralb_ble](https://github.com/bkbilly/oralb_ble) for pioneering
  the active-connection approach and the original characteristic maps.
- The official [oralb integration](https://www.home-assistant.io/integrations/oralb/)
  and [oralb-ble](https://github.com/Bluetooth-Devices/oralb-ble) library.
- Related: [home-assistant/core#177039](https://github.com/home-assistant/core/issues/177039)
  (passive updates freezing after poll attempts in the official
  integration).

## Disclaimer

Not affiliated with Oral-B / Procter & Gamble. Byte-level protocol
knowledge was obtained by observing a single iO-series device; other
models may differ.

