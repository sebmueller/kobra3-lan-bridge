"""Anycubic Kobra 3 LAN Bridge for Home Assistant.

Connects to one or more Anycubic Kobra 3 printers over their local LAN MQTT
protocol (no cloud account required), publishes Home Assistant MQTT discovery
entities for status/monitoring, forwards user commands (pause/resume/stop,
print speed, nozzle/bed target temperatures, camera capture) to the printers,
and re-publishes printer status reports into a local MQTT broker (e.g. the
Mosquitto add-on) so Home Assistant can consume them.

Configuration is read from /data/options.json (standard Home Assistant add-on
options) or the KOBRA_OPTIONS_JSON environment variable (useful for testing).

Options:
{
  "printers": [
    {"name": "Kobra 3 - oben", "ip": "192.168.2.107"}
  ],
  "mqtt_host": "core-mosquitto",
  "mqtt_port": 1883,
  "mqtt_user": "mqtt",
  "mqtt_pass": "mqtt",
  "poll_interval": 30
}
"""

import base64
import hashlib
import json
import os
import random
import re
import ssl
import string
import sys
import threading
import time
import urllib.request
import uuid

import paho.mqtt.client as mqtt

MODEL_ID = '20024'  # Anycubic Kobra 3


class _Tee:
    """Mirror stdout/stderr into a log file (readable via the app_configs share)."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
            except Exception:
                pass
        return len(data)

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass


try:
    _logfile = open(os.environ.get('KOBRA_LOG', '/data/kobra_bridge.log'), 'a', buffering=1)
    sys.stdout = _Tee(sys.__stdout__, _logfile)
    sys.stderr = _Tee(sys.__stderr__, _logfile)
except Exception:
    pass


def load_options():
    path = os.environ.get('KOBRA_OPTIONS', '/data/options.json')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    env = os.environ.get('KOBRA_OPTIONS_JSON')
    if env:
        return json.loads(env)
    # Fallback for manual testing: single printer via env vars
    ip = os.environ.get('KOBRA_PRINTER_IP')
    if ip:
        return {'printers': [{'name': os.environ.get('KOBRA_PRINTER_NAME', 'Kobra 3'), 'ip': ip}]}
    raise SystemExit('Keine Konfiguration gefunden (/data/options.json oder KOBRA_OPTIONS_JSON)')


def slugify(name, fallback):
    slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    return slug or fallback


# ---------------------------------------------------------------------------
# Credential handshake (per-session MQTT login for the printer's own broker)
# ---------------------------------------------------------------------------

def _md5_hex(s):
    return hashlib.md5(s.encode()).hexdigest()


def _rand_alnum(n):
    alphabet = string.ascii_letters + string.digits
    return ''.join(random.choice(alphabet) for _ in range(n))


def aes_cbc_decrypt(key16, iv16, data):
    """Pure-Python AES-128-CBC decryption (single short payload, speed irrelevant)."""
    sbox = [
        0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
        0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
        0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
        0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
        0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
        0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
        0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
        0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
        0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
        0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
        0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
        0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
        0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
        0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
        0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
        0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
    ]
    inv_sbox = [0] * 256
    for i, v in enumerate(sbox):
        inv_sbox[v] = i
    rcon = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]

    def xtime(a):
        a <<= 1
        return (a ^ 0x1b) & 0xff if a & 0x100 else a

    def mul(a, b):
        p = 0
        for _ in range(8):
            if b & 1:
                p ^= a
            b >>= 1
            a = xtime(a)
        return p

    words = [list(key16[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        tmp = list(words[i - 1])
        if i % 4 == 0:
            tmp = tmp[1:] + tmp[:1]
            tmp = [sbox[b] for b in tmp]
            tmp[0] ^= rcon[i // 4 - 1]
        words.append([a ^ b for a, b in zip(words[i - 4], tmp)])
    round_keys = [[words[r * 4 + c] for c in range(4)] for r in range(11)]

    def add_round_key(state, rk):
        for c in range(4):
            for r in range(4):
                state[r][c] ^= rk[c][r]

    def inv_shift_rows(state):
        for r in range(1, 4):
            state[r] = state[r][-r:] + state[r][:-r]

    def inv_sub_bytes(state):
        for r in range(4):
            for c in range(4):
                state[r][c] = inv_sbox[state[r][c]]

    def inv_mix_columns(state):
        for c in range(4):
            a = [state[r][c] for r in range(4)]
            state[0][c] = mul(a[0], 14) ^ mul(a[1], 11) ^ mul(a[2], 13) ^ mul(a[3], 9)
            state[1][c] = mul(a[0], 9) ^ mul(a[1], 14) ^ mul(a[2], 11) ^ mul(a[3], 13)
            state[2][c] = mul(a[0], 13) ^ mul(a[1], 9) ^ mul(a[2], 14) ^ mul(a[3], 11)
            state[3][c] = mul(a[0], 11) ^ mul(a[1], 13) ^ mul(a[2], 9) ^ mul(a[3], 14)

    def decrypt_block(block):
        state = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
        add_round_key(state, round_keys[10])
        for rnd in range(9, -1, -1):
            inv_shift_rows(state)
            inv_sub_bytes(state)
            add_round_key(state, round_keys[rnd])
            if rnd != 0:
                inv_mix_columns(state)
        out = bytearray(16)
        for c in range(4):
            for r in range(4):
                out[c * 4 + r] = state[r][c]
        return bytes(out)

    out = bytearray()
    prev = iv16
    for i in range(0, len(data) - len(data) % 16, 16):
        block = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(decrypt_block(block), prev))
        prev = block
    return bytes(out)


def discover_credentials(printer_host):
    """Perform the printer's local credential handshake.

    1. GET http://{host}:18910/info  -> {token, ctrlInfoUrl}
    2. POST {ctrlInfoUrl}?ts&nonce&sign&did  (sign = md5(md5(token[:16])+ts+nonce))
       -> {code:200, data:{info: <aes-cbc-b64>, token: <iv>}}
    3. AES-128-CBC decrypt with key=token[16:32] -> {username, password, deviceId, modeId, ...}
    """
    info = json.loads(urllib.request.urlopen(f'http://{printer_host}:18910/info', timeout=10).read())
    token = info['token']
    ts = str(int(time.time() * 1000))
    nonce = _rand_alnum(6)
    did = 'KobraBridge-' + _rand_alnum(8)
    sign = _md5_hex(_md5_hex(token[:16]) + ts + nonce)
    req = urllib.request.Request(f"{info['ctrlInfoUrl']}?ts={ts}&nonce={nonce}&sign={sign}&did={did}", data=b'')
    ctrl = json.loads(urllib.request.urlopen(req, timeout=10).read())
    if ctrl.get('code') != 200:
        raise RuntimeError(f'ctrl code {ctrl.get("code")}')
    key = token[16:32].encode()
    iv = ctrl['data']['token'].encode()
    plain = aes_cbc_decrypt(key, iv, base64.b64decode(ctrl['data']['info']))
    return json.loads(plain[:-plain[-1]])


# ---------------------------------------------------------------------------
# MQTT command definitions
# ---------------------------------------------------------------------------

SPEED_NAMES = {1: 'Stumm', 2: 'Standard', 3: 'Sport'}
SPEED_VALUES = {v: k for k, v in SPEED_NAMES.items()}


def make_client(cid):
    if hasattr(mqtt, 'CallbackAPIVersion'):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, cid)
    return mqtt.Client(cid)


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class Printer:
    def __init__(self, cfg):
        self.name = cfg['name']
        self.ip = cfg['ip']
        self.prefix = slugify(cfg.get('prefix') or cfg['name'], self.ip.replace('.', '_'))
        self.device_id = None
        self.model_id = MODEL_ID
        self.fw = '?'
        self.client = None
        self.connected = threading.Event()
        self.targets = {'target_nozzle_temp': 0, 'target_hotbed_temp': 0}
        self.targets_lock = threading.Lock()

    @property
    def report_topic(self):
        return (f'{self.prefix}/anycubic/anycubicCloud/v1/printer/public/'
                f'{self.model_id}/{self.device_id}/info/report')

    @property
    def temp_report_topic(self):
        return (f'{self.prefix}/anycubic/anycubicCloud/v1/printer/public/'
                f'{self.model_id}/{self.device_id}/tempature/report')

    def web_topic(self, kind):
        return f'anycubic/anycubicCloud/v1/web/printer/{self.model_id}/{self.device_id}/{kind}'

    def query_topic(self):
        return f'anycubic/anycubicCloud/v1/slicer/printer/{self.model_id}/{self.device_id}/info'

    def build_discovery(self):
        st = self.report_topic
        trt = self.temp_report_topic
        pfx = self.prefix
        cmd = f'{pfx}/cmd'

        def cmp(key, name, **kw):
            d = {'p': 'sensor', 'name': name, 'unique_id': f'{pfx}_{key}', 'state_topic': st,
                 'qos': 1, 'expire_after': 90}
            d.update(kw)
            return d

        payload = {
            'dev': {'ids': pfx, 'name': self.name, 'mf': 'Anycubic', 'mdl': 'Kobra 3', 'sw': self.fw},
            'o': {'name': 'kobra3-lan-bridge', 'sw': VERSION},
            'cmps': {
                'status': cmp('status', 'Status', icon='mdi:printer-3d',
                              value_template='{% set p = value_json.data.project %}'
                                             '{% if p and p.state %}{{ p.state }}{% else %}{{ value_json.data.state or "idle" }}{% endif %}',
                              json_attributes_topic=st, json_attributes_path='data.project'),
                'progress': cmp('progress', 'Fortschritt', unit_of_measurement='%', state_class='measurement',
                                icon='mdi:progress-clock',
                                value_template='{% if value_json.data.project %}{{ value_json.data.project.progress or 0 }}{% else %}0{% endif %}'),
                'remain': cmp('remain', 'Restzeit (Minuten)', unit_of_measurement='min', state_class='measurement',
                              icon='mdi:timer',
                              value_template='{% if value_json.data.project and value_json.data.project.remain_time %}'
                                             '{{ value_json.data.project.remain_time }}{% else %}0{% endif %}'),
                'remain_hm': cmp('remain_hm', 'Restzeit', icon='mdi:timer-outline',
                                 value_template='{% set m = (value_json.data.project.remain_time or 0) | int %}'
                                                '{% set h = m // 60 %}{% set r = m % 60 %}'
                                                '{% if h == 0 %}{{ r }} Minuten{% else %}{{ h }} {{ "Stunde" if h == 1 else "Stunden" }}'
                                                '{% if r > 0 %} und {{ r }} {{ "Minute" if r == 1 else "Minuten" }}{% endif %}{% endif %}'),
                'print_time': cmp('print_time', 'Druckzeit', unit_of_measurement='min', icon='mdi:clock-start',
                                  value_template='{% if value_json.data.project and value_json.data.project.print_time %}'
                                                 '{{ value_json.data.project.print_time }}{% else %}0{% endif %}'),
                'layer': cmp('layer', 'Layer', icon='mdi:layers-triple',
                             value_template='{% if value_json.data.project %}{{ value_json.data.project.curr_layer or 0 }}{% else %}0{% endif %}'),
                'nozzle_temp': cmp('nozzle_temp', 'Düsentemperatur', device_class='temperature',
                                   unit_of_measurement='°C', state_class='measurement',
                                   value_template='{% if value_json.data.temp %}{{ value_json.data.temp.curr_nozzle_temp or 0 }}{% else %}0{% endif %}'),
                'nozzle_target': cmp('nozzle_target', 'Düsen-Zieltemperatur', device_class='temperature',
                                     unit_of_measurement='°C',
                                     value_template='{% if value_json.data.temp %}{{ value_json.data.temp.target_nozzle_temp or 0 }}{% else %}0{% endif %}'),
                'bed_temp': cmp('bed_temp', 'Bett-Temperatur', device_class='temperature',
                                unit_of_measurement='°C', state_class='measurement',
                                value_template='{% if value_json.data.temp %}{{ value_json.data.temp.curr_hotbed_temp or 0 }}{% else %}0{% endif %}'),
                'bed_target': cmp('bed_target', 'Bett-Zieltemperatur', device_class='temperature',
                                  unit_of_measurement='°C',
                                  value_template='{% if value_json.data.temp %}{{ value_json.data.temp.target_hotbed_temp or 0 }}{% else %}0{% endif %}'),
                'fan': cmp('fan', 'Druckkopf-Lüfter', unit_of_measurement='%', icon='mdi:fan',
                           value_template='{{ value_json.data.fan_speed_pct or 0 }}'),
                'firmware': cmp('firmware', 'Firmware', icon='mdi:chip', entity_category='diagnostic',
                                value_template="{{ value_json.data.version or 'unbekannt' }}"),
                'job': cmp('job', 'Job', icon='mdi:file-cad',
                           value_template='{% if value_json.data.project and value_json.data.project.filename %}'
                                          '{{ value_json.data.project.filename }}{% else %}-{% endif %}'),
                'speed_sel': {'p': 'select', 'name': 'Geschwindigkeit', 'unique_id': f'{pfx}_speed',
                              'state_topic': st, 'qos': 1,
                              'options': list(SPEED_NAMES.values()),
                              'icon': 'mdi:speedometer',
                              'command_topic': f'{cmd}/speed',
                              'value_template': '{% set m = (value_json.data.print_speed_mode or 2) | int %}'
                                                '{% if m == 1 %}Stumm{% elif m == 3 %}Sport{% else %}Standard{% endif %}'},
                'nozzle_set': {'p': 'number', 'name': 'Düse Soll', 'unique_id': f'{pfx}_nozzle_set',
                               'state_topic': trt, 'qos': 1,
                               'device_class': 'temperature', 'unit_of_measurement': '°C',
                               'min': 0, 'max': 300, 'step': 5, 'mode': 'box',
                               'icon': 'mdi:thermometer',
                               'command_topic': f'{cmd}/nozzle_temp',
                               'value_template': '{{ value_json.data.target_nozzle_temp or 0 }}'},
                'bed_set': {'p': 'number', 'name': 'Bett Soll', 'unique_id': f'{pfx}_bed_set',
                            'state_topic': trt, 'qos': 1,
                            'device_class': 'temperature', 'unit_of_measurement': '°C',
                            'min': 0, 'max': 120, 'step': 5, 'mode': 'box',
                            'icon': 'mdi:radiator',
                            'command_topic': f'{cmd}/bed_temp',
                            'value_template': '{{ value_json.data.target_hotbed_temp or 0 }}'},
                'btn_pause': {'p': 'button', 'name': 'Pause', 'unique_id': f'{pfx}_btn_pause',
                              'command_topic': f'{cmd}/pause', 'payload_press': 'PAUSE',
                              'icon': 'mdi:pause', 'qos': 1},
                'btn_resume': {'p': 'button', 'name': 'Fortsetzen', 'unique_id': f'{pfx}_btn_resume',
                               'command_topic': f'{cmd}/resume', 'payload_press': 'RESUME',
                               'icon': 'mdi:play', 'qos': 1},
                'btn_stop': {'p': 'button', 'name': 'Stopp', 'unique_id': f'{pfx}_btn_stop',
                             'command_topic': f'{cmd}/stop', 'payload_press': 'STOP',
                             'icon': 'mdi:stop-circle-outline', 'qos': 1},
                'btn_cam_start': {'p': 'button', 'name': 'Kamera starten', 'unique_id': f'{pfx}_btn_cam_start',
                                  'command_topic': f'{cmd}/cam_start', 'payload_press': 'ON',
                                  'icon': 'mdi:video', 'qos': 1, 'entity_category': 'config'},
                'btn_cam_stop': {'p': 'button', 'name': 'Kamera stoppen', 'unique_id': f'{pfx}_btn_cam_stop',
                                 'command_topic': f'{cmd}/cam_stop', 'payload_press': 'OFF',
                                 'icon': 'mdi:video-off', 'qos': 1, 'entity_category': 'config'},
            },
        }
        return json.dumps(payload, ensure_ascii=False)

    def send_msg(self, kind, typ, action, data):
        if not (self.client and self.client.is_connected()):
            return False
        payload = {'type': typ, 'action': action, 'msgid': str(uuid.uuid4()),
                   'timestamp': int(time.time() * 1000)}
        if data is not None:
            payload['data'] = data
        self.client.publish(self.web_topic(kind), json.dumps(payload), 1)
        return True


VERSION = '2.0.0'
options = load_options()
PRINTERS = [Printer(p) for p in options['printers']]
LOCAL_HOST = options.get('mqtt_host', 'core-mosquitto')
LOCAL_PORT = int(options.get('mqtt_port', 1883))
LOCAL_USER = options.get('mqtt_user', '')
LOCAL_PASS = options.get('mqtt_pass', '')
POLL_SECONDS = int(options.get('poll_interval', 30))
SUFFIX = os.environ.get('KOBRA_ID_SUFFIX', '')

local_client = None
local_ready = threading.Event()
printer_clients = {}
targets_lock = threading.Lock()


def publish_discovery():
    for dev in PRINTERS:
        if dev.device_id:
            local_client.publish(f'homeassistant/device/{dev.prefix}/config',
                                 dev.build_discovery(), 1, retain=True)
    print('[bridge] Discovery published (retained)', flush=True)


def on_local_connect(c, u, f, rc):
    print(f'[bridge] Lokaler Broker verbunden rc={rc}', flush=True)
    if rc == 0:
        local_ready.set()
        publish_discovery()
        for dev in PRINTERS:
            c.subscribe(f'{dev.prefix}/cmd/#', 1)


def handle_local_command(topic, payload):
    parts = topic.split('/')
    if len(parts) < 3 or parts[1] != 'cmd':
        return
    pfx, cmd = parts[0], parts[2]
    dev = next((d for d in PRINTERS if d.prefix == pfx), None)
    if dev is None:
        return
    pc = printer_clients.get(pfx)
    if not (pc and pc.is_connected()):
        print(f'[cmd] {pfx}/{cmd}: Drucker nicht verbunden', flush=True)
        return
    text = payload.decode('utf-8', 'replace').strip()
    try:
        if cmd == 'pause':
            dev.send_msg('print', 'print', 'pause', {'taskid': '-1'})
        elif cmd == 'resume':
            dev.send_msg('print', 'print', 'resume', {'taskid': '-1'})
        elif cmd == 'stop':
            dev.send_msg('print', 'print', 'stop', {'taskid': '-1'})
        elif cmd == 'speed':
            mode = SPEED_VALUES.get(text)
            if mode is None:
                digits = ''.join(ch for ch in text if ch.isdigit())
                mode = int(digits) if digits else 2
            dev.send_msg('print', 'print', 'update',
                         {'taskid': '-1', 'settings': {'print_speed_mode': mode}})
        elif cmd in ('nozzle_temp', 'bed_temp'):
            value = int(float(text))
            with targets_lock:
                tgt = dict(dev.targets)
            key = 'target_nozzle_temp' if cmd == 'nozzle_temp' else 'target_hotbed_temp'
            tgt[key] = value
            tgt.setdefault('target_nozzle_temp', 0)
            tgt.setdefault('target_hotbed_temp', 0)
            dev.send_msg('print', 'print', 'update', {'taskid': '-1', 'settings': tgt})
        elif cmd in ('cam_start', 'cam_stop'):
            dev.send_msg('video', 'video', 'startCapture' if cmd == 'cam_start' else 'stopCapture', None)
        else:
            return
        print(f'[cmd] {pfx}/{cmd} -> {text!r} gesendet', flush=True)
    except Exception as e:
        print(f'[cmd] Fehler {pfx}/{cmd}: {e}', flush=True)


def on_local_message(c, u, m):
    handle_local_command(m.topic, m.payload)


def printer_worker(dev):
    prefix = dev.prefix
    while True:
        try:
            creds = discover_credentials(dev.ip)
            dev.device_id = creds['deviceId']
            dev.model_id = creds.get('modeId', MODEL_ID)
            print(f'[{prefix}] Handshake OK (user={creds["username"]}, id={dev.device_id[:8]}...)', flush=True)
            if local_ready.is_set():
                publish_discovery()
        except Exception as e:
            print(f'[{prefix}] Handshake fehlgeschlagen: {e} - retry in 15s', flush=True)
            time.sleep(15)
            continue

        connected = threading.Event()
        c = make_client('kobra_bridge_' + prefix + SUFFIX)
        c.username_pw_set(creds['username'], creds['password'])
        c.tls_set(cert_reqs=ssl.CERT_NONE)
        c.tls_insecure_set(True)

        def on_connect(cc, u, f, rc):
            print(f'[{prefix}] Drucker verbunden rc={rc}', flush=True)
            if rc == 0:
                cc.subscribe('anycubic/#', 1)
                cc.publish(dev.web_topic('video'), json.dumps(
                    {'type': 'video', 'action': 'startCapture', 'msgid': str(uuid.uuid4()),
                     'timestamp': int(time.time() * 1000), 'data': None}), 1)
                connected.set()

        def on_message(cc, u, m):
            with targets_lock:
                try:
                    d = json.loads(m.payload)
                    if d.get('type') == 'tempature' and isinstance(d.get('data'), dict):
                        tgt = dev.targets
                        for k in ('target_nozzle_temp', 'target_hotbed_temp'):
                            if k in d['data']:
                                tgt[k] = d['data'][k]
                except Exception:
                    pass
            local_client.publish(f'{prefix}/{m.topic}', m.payload, 1)
            try:
                d = json.loads(m.payload)
                if d.get('type') == 'info' and isinstance(d.get('data'), dict):
                    ver = d['data'].get('version')
                    if ver and ver != dev.fw:
                        dev.fw = ver
                        local_client.publish(f'homeassistant/device/{prefix}/config',
                                             dev.build_discovery(), 1, retain=True)
                        print(f'[{prefix}] Firmware {ver} erkannt, Discovery aktualisiert', flush=True)
            except Exception:
                pass

        def on_disconnect(cc, u, rc):
            print(f'[{prefix}] getrennt rc={rc}', flush=True)
            connected.clear()

        c.on_connect = on_connect
        c.on_message = on_message
        c.on_disconnect = on_disconnect
        printer_clients[prefix] = c
        try:
            c.connect(dev.ip, 9883, keepalive=60)
            c.loop_start()
            connected.wait(timeout=15)
            while connected.is_set():
                time.sleep(2)
        except Exception as e:
            print(f'[{prefix}] Verbindungsfehler: {e}', flush=True)
        try:
            c.loop_stop()
            c.disconnect()
        except Exception:
            pass
        printer_clients.pop(prefix, None)
        print(f'[{prefix}] Neuverbindung in 5s', flush=True)
        time.sleep(5)


def poll_worker():
    while True:
        time.sleep(POLL_SECONDS)
        for dev in PRINTERS:
            c = printer_clients.get(dev.prefix)
            if c and c.is_connected():
                payload = json.dumps({'type': 'info', 'action': 'query', 'msgid': str(uuid.uuid4()),
                                      'timestamp': int(time.time() * 1000)})
                c.publish(dev.query_topic(), payload, 1)


if __name__ == '__main__':
    print(f'[bridge] Starte Kobra 3 LAN Bridge v{VERSION}', flush=True)
    local_client = make_client('kobra_bridge_local' + SUFFIX)
    if LOCAL_USER:
        local_client.username_pw_set(LOCAL_USER, LOCAL_PASS)
    local_client.on_connect = on_local_connect
    local_client.on_message = on_local_message
    local_client.connect_async(LOCAL_HOST, LOCAL_PORT, keepalive=60)
    local_client.loop_start()

    local_ready.wait(timeout=30)

    threads = []
    for dev in PRINTERS:
        t = threading.Thread(target=printer_worker, args=(dev,), daemon=True)
        threads.append(t)
        t.start()
        time.sleep(0.3)
    threading.Thread(target=poll_worker, daemon=True).start()
    print('[bridge] Alle Worker gestartet', flush=True)
    while True:
        time.sleep(60)
        states = {d.prefix: (printer_clients[d.prefix].is_connected() if d.prefix in printer_clients else False)
                  for d in PRINTERS}
        print('[bridge] Verbindungsstatus:', states, flush=True)
