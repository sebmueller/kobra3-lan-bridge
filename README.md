# Kobra 3 LAN Bridge for Home Assistant

Monitor and control **Anycubic Kobra 3** printers over your local network — **no cloud, no accounts, no firmware changes**. Runs as a [Home Assistant](https://www.home-assistant.io/) add-on and publishes printers as native MQTT devices with full control.

![Status: working](https://img.shields.io/badge/status-working-brightgreen) ![Tested on](https://img.shields.io/badge/tested%20on-Kobra%203%20%2B%20HA%202026.8-blue)

## Features

- 📊 **Full monitoring**: print progress, current/target layer, remaining & elapsed time (human-readable), nozzle/bed temperatures (actual + target), part cooling fan, current job, firmware version
- 🎛️ **Full control**: Pause / Resume / Stop buttons, print speed select (Silent / Standard / Sport), nozzle & bed target temperature number inputs
- 📷 **Camera support**: starts the printer's onboard camera encoder automatically; pairs perfectly with [go2rtc](https://github.com/AlexxIT/go2rtc) for live views in HA
- 🔌 **No cloud**: talks directly to the printer's own LAN MQTT broker — works fully offline
- 🖨️ **Multi-printer**: add as many printers as you like (only IP + name needed — everything else is auto-discovered)
- 🔄 **Self-healing**: per-session credential handshake on every (re)connect, automatic reconnects, entities expire cleanly when a printer goes offline

## Requirements

- Home Assistant OS / Supervised with the **Mosquitto broker** add-on (any MQTT broker works)
- Anycubic Kobra 3 (or compatible: K3 Max / K3 V2 — model IDs `20024`/`20026`/`20027`) on stock firmware, reachable on your LAN
- Printers must be connected to the same network (the "LAN mode" the Anycubic Slicer Next uses)

## Installation

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories** and add:
   ```
   https://github.com/sebmueller/kobra3-lan-bridge
   ```
2. Install **Kobra 3 LAN Bridge** from the store.
3. Configure it (Settings → Add-ons → Kobra 3 LAN Bridge → Configuration):
   - **Printers**: one entry per printer — just a display `name` and the printer's LAN `ip`
   - **MQTT**: host/port/credentials of your MQTT broker. For the Mosquitto add-on, add a login under *Mosquitto broker → Configuration → Logins* (e.g. user `mqtt`, password of your choice)
4. Start the add-on. Within ~30 seconds all entities appear.

> **Tip**: Nothing needs to be configured on the printer itself — it keeps its stock firmware and can still be used with Anycubic Slicer Next and the Anycubic app at the same time.

## What you get

Per printer (device with unique IDs, so renaming is safe):

| Entity | Type | Description |
|---|---|---|
| `sensor.*_status` | sensor | `printing` / `resuming` / `paused` / `free` / `busy` |
| `sensor.*_fortschritt` | sensor | Print progress 0–100 % |
| `sensor.*_restzeit` / `restzeit_minuten` | sensor | Remaining time, human readable ("5 Stunden und 58 Minuten") and numeric (minutes) |
| `sensor.*_druckzeit` | sensor | Elapsed print time (minutes) |
| `sensor.*_layer` | sensor | Current layer |
| `sensor.*_dusentemperatur` / `_bett_temperatur` | sensor | Nozzle / bed temperature (°C) |
| `sensor.*_dusen_zieltemperatur` / `_bett_zieltemperatur` | sensor | Nozzle / bed target temperature |
| `sensor.*_druckkopf_lufter` | sensor | Part cooling fan (%) |
| `sensor.*_job` | sensor | Current job filename |
| `sensor.*_firmware` | sensor | Printer firmware version |
| `button.*_pause` / `_fortsetzen` / `_stopp` | button | Print control |
| `select.*_geschwindigkeit` | select | Print speed mode |
| `number.*_duse_soll` / `_bett_soll` | number | Set nozzle / bed target temperature |
| `button.*_kamera_starten` / `_kamera_stoppen` | button | Camera encoder control |

## Dashboard example

Create a dashboard with a `gauge` (progress), a `camera`/`picture-entity` card (see below), button rows and an entities card with the select/number entities. Every state a print reports is also available as JSON attributes on the status sensor.

## Camera

The printer's camera only streams after its encoder has been started (`video startCapture` — the bridge does this automatically on connect). The raw stream is then available at:

```
http://<printer-ip>:18088/flv
```

For a live view in Home Assistant, the recommended setup is [go2rtc](https://github.com/AlexxIT/go2rtc) (as a container on any always-on machine or as an add-on):

```yaml
streams:
  kobra_oben: http://192.168.1.100:18088/flv
```

go2rtc restreams this as RTSP (`rtsp://<go2rtc-host>:8554/kobra_oben`) or serves JPEG snapshots (`http://<go2rtc-host>:1984/api/frame.jpeg?src=kobra_oben`) which a HA *Generic Camera* can display.

## How it works

Anycubic's stock firmware runs its own MQTT broker (TLS, port 9883) on the printer. Slicer Next talks to it in "LAN mode" using **per-session credentials** that must be obtained through a local HTTP handshake:

1. `GET http://<printer-ip>:18910/info` → `token`, `ctrlInfoUrl`
2. `POST <ctrlInfoUrl>?ts=<ms>&nonce=<6 alnum>&sign=<md5(md5(token[:16])+ts+nonce)>&did=<random>` → encrypted payload
3. AES-128-CBC decrypt (key = `token[16:32]`, iv = response `token`) → `{username, password, deviceId, modeId, ...}`

The bridge performs this handshake on every (re)connect, then:

- subscribes to `anycubic/anycubicCloud/v1/printer/public/{model}/{device}/#` for status reports
- publishes commands to `anycubic/anycubicCloud/v1/web/printer/{model}/{device}/{kind}`
- republishes everything to your local MQTT broker as `kobra3_<printer>/anycubic/...` and publishes [MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery) configs

### Full protocol reference

See [`docs/mqtt-protocol.md`](docs/mqtt-protocol.md) — topics, payload formats for all known commands (pause/resume/stop/start, temperature & speed updates, lights, video capture, file listing/upload) and pointers to related projects.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Entities never appear | Check the add-on logs: handshake fails → printer unreachable, wrong IP, or MQTT login wrong |
| `handshake failed: ctrl code …` | Printer firmware older than ~2.4.x may need the cloud-linked app once; usually it just works |
| Sensors become unavailable periodically | MQTT broker connection flapping — check broker logs |
| Camera shows nothing | Press *Kamera starten*; verify `http://<printer-ip>:18088/flv` streams (VLC) |

## Credits

This project stands on the shoulders of the community's reverse-engineering work:

- [rvanderp3/kobra-connect](https://github.com/rvanderp3/kobra-connect) — documented MQTT command reference (pause/resume/stop, update settings, lights)
- [A-to-PC/kobra-lan-monitor](https://github.com/A-to-PC/kobra-lan-monitor) & [A-to-PC/Kobra-Time-Lapse](https://github.com/A-to-PC/Kobra-Time-Lapse) — credential handshake, pause command, video capture control, live-verified payloads
- [jbatonnet/Rinkhals](https://github.com/jbatonnet/Rinkhals) — firmware internals & MQTT docs

Our implementation is an independent Python rewrite (no copied code) — thanks for mapping the protocol!

## License

[MIT](LICENSE)
