# Anycubic Kobra 3 — LAN MQTT Protocol Notes

Reverse-engineered notes for the Anycubic Kobra 3 series local LAN protocol
(stock firmware). Verified against firmware 2.4.1.9 and 2.4.6.7 on Kobra 3
(model id `20024`). Also reported to work: `20026` (K3 Max), `20027` (K3 V2),
`20025` (KS1), `20029` (KS1 Max).

> Primary sources: live captures of Anycubic Slicer Next traffic, the
> documented command reference in [rvanderp3/kobra-connect](https://github.com/rvanderp3/kobra-connect)
> (`docs/mqtt-commands.md`), and [A-to-PC/kobra-lan-monitor](https://github.com/A-to-PC/kobra-lan-monitor).

## Connection

- The printer itself runs the MQTT **broker** (TLS, port `9883`)
- Credentials are **per-session** and obtained via a local HTTP handshake (see below)
- MQTT v3.1.1 works; MQTT v5 is accepted too

### Credential handshake

1. `GET http://<ip>:18910/info`
   → `{"token": "<32 chars>", "ctrlInfoUrl": "http://<ip>:18910/ctrl", ...}`
2. `POST <ctrlInfoUrl>?ts=<unix_ms>&nonce=<6 alnum>&sign=<md5(md5(token[:16])+ts+nonce)>&did=<random client id>`
   → `{"code": 200, "data": {"info": "<base64 ciphertext>", "token": "<iv>"}}`
3. AES-128-CBC decrypt, key = `token[16:32]`, iv = `data.token`, PKCS7 padding
   → `{"broker": "mqtts://<ip>:9883", "deviceId": "<32 hex>", "modeId": "20024",
      "username": "userXXXXXXXX", "password": "<random>", ...}`

Old credentials stop working for *new* connections once a new session is
requested — always re-run the handshake before connecting. Existing long-lived
connections stay valid.

## Topics

```
# commands to the printer (both prefixes work for most commands)
anycubic/anycubicCloud/v1/slicer/printer/{model_id}/{device_id}/{kind}
anycubic/anycubicCloud/v1/web/printer/{model_id}/{device_id}/{kind}

# reports from the printer
anycubic/anycubicCloud/v1/printer/public/{model_id}/{device_id}/{kind}/report

# convenience subscription
anycubic/anycubicCloud/v1/printer/public/{model_id}/{device_id}/#
```

## Base message format

```json
{
  "type": "<kind>",
  "action": "<action>",
  "timestamp": <unix_ms>,
  "msgid": "<uuid>",
  "data": { ... }
}
```

## Commands

### Query info (full status snapshot)
`kind=info, action=query` → `info/report` with:
`data.printerName`, `data.state` (`free`/`busy`), `data.temp` (curr/target nozzle & hotbed),
`data.print_speed_mode`, `data.fan_speed_pct`, `data.project` (active print:
`state`, `progress`, `curr_layer`, `total_layers`, `remain_time` (minutes!),
`print_time` (minutes), `filename`, `pause`, `task_id`), `data.last_project`,
`data.version`, `data.urls.rtspUrl` (HTTP-FLV stream), `data.urls.fileUploadurl`.

### Print control
`kind=print`:
- `action=query` → `print/report` with active print state
- `action=start` → `data: {taskid: "-1", filename: "...", filetype: 1}`
- `action=pause` → `data: {taskid: "-1"}`
- `action=resume` → `data: {taskid: "-1"}`  (verified — NOT `continue`)
- `action=stop` → `data: {taskid: "-1"}` (cancels the print)

### Update print settings
`kind=print, action=update`:
```json
{"data": {"taskid": "-1", "settings": {
    "target_nozzle_temp": 210,
    "target_hotbed_temp": 60,
    "fan_speed_pct": 100,
    "print_speed_mode": 2,
    "z_comp": 0}}}
```
`print_speed_mode`: `1` = silent, `2` = standard, `3` = sport/fast.

### Lights
`kind=light, action=control` → `data: {type: 1|2|3, status: 0|1, brightness: 0-100}`
(`1` = head light, `2` = chamber light (KS1), `3` = camera light).

### Camera / video
`kind=video`:
- `action=startCapture` / `action=stopCapture` → starts/stops the onboard
  video encoder. **The `http://<ip>:18088/flv` HTTP-FLV stream serves no
  frames until this is sent.** Idempotent; safe to send repeatedly.

### Files
`kind=file`: `action=listLocal|listUdisk` (`data: {path: "/"}`),
`action=deleteLocal|deleteUdisk` (`data: {path, filename}`).

### File upload (HTTP)
`POST http://<ip>:18910/gcode_upload?s=<token>` — token from `info/report`
`data.urls.fileUploadurl`.

### Other known kinds
`tempature` (set/query + periodic `tempature/report` during prints),
`fan` (query/report), `light`, `multiColorBox` (ACE color box: getInfo/setDry/refresh),
`peripherie` (query → camera/udisk/multiColorBox presence), `calibration`,
`extrudeControl`, `skip` (skip objects), `lastWill`, `status`, `properties`.

## Notes

- `remain_time` / `print_time` in reports are in **minutes**
- Pause states reported as `pause` (newer FW) or `paused` (older FW); resuming
  reports `resuming` before returning to `printing`
- No raw G-code injection endpoint exists in the stock protocol
  (see [Rinkhals](https://github.com/jbatonnet/Rinkhals) for custom firmware with Moonraker)
