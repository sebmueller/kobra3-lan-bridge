# Kobra 3 LAN Bridge

Monitor and control your Anycubic Kobra 3 printers over the local network —
no cloud account required.

- Full status: progress, layers, remaining/elapsed time, temperatures, fan, job, firmware
- Control: pause / resume / stop, print speed mode, nozzle & bed target temperatures
- Camera encoder control (pairs with go2rtc for live views)
- MQTT discovery — printers appear as devices in Home Assistant automatically
- Multi-printer support: only name + IP needed, everything else auto-discovered

## Requirements

- MQTT broker (e.g. the Mosquitto broker add-on)
- Kobra 3 on stock firmware, reachable on the LAN

See the [GitHub README](https://github.com/sebmueller/kobra3-lan-bridge) for
full documentation, camera setup and the protocol notes.
