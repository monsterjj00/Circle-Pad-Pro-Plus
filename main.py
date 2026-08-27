"""
Circle Pad — Pico 2 W input redirection host.

Runs a Wi-Fi AP named "Circle Pad", spoofs the 3DS's/Pretendo's connectivity
check so it holds the connection with no real internet present, then streams
full controller state to a 3DS running Luma3DS's Rosalina > Miscellaneous
options... > Start InputRedirection over UDP port 4950.

Covers: all 12 standard buttons, a digital circle pad (4 buttons, snaps to
full deflection), an analog C-stick (2-axis joystick module) with ZL/ZR,
and the HOME/POWER/POWER-long "interface buttons".

Protocol notes (confirmed against LumaTeam/Luma3DS's input_redirection.c
and TuxSH/InputRedirectionClient-Qt's main.cpp — see sendFrame()):
  - 3DS binds a UDP socket to its own IP on port 4950. No handshake, no ack.
  - Packet is 20 bytes: buttons(4) + touch(4) + circle pad(4) + C-stick/ZL/ZR(4)
    + interface buttons(4), all little-endian.
  - Button word is ACTIVE-LOW: default 0x00000FFF (nothing pressed),
    clear a bit to register that button as pressed.
  - Circle pad: (y << 12) | x, each 0-0xFFF, center 0x800, neutral const
    0x007FF7FF. x/y = axis * 0x5d0 + 0x800, clamped.
  - C-stick/ZL/ZR: (y << 24) | (x << 16) | (zl_zr_bits << 8) | 0x81, each
    axis 0-0xFF, center 0x80, neutral const 0x80800081. The raw stick
    position gets rotated 45 degrees before packing (matches the real
    client — this is a real Nintendo hardware quirk, not a bug).
  - Interface buttons (HOME=bit0, POWER=bit1, POWER-long=bit2) are
    ACTIVE-HIGH, unlike the main button word.

Wiring: each digital pin -> switch -> GND, using the Pico's internal
pull-ups (pressed = pin reads LOW). C-stick uses a 2-axis analog joystick
module on GP26 (X) / GP27 (Y) — see the C-stick section below for why the
circle pad is digital-only (ADC pin budget).
"""

import network
import select
import socket
import struct
import time
from machine import Pin, ADC

# ---------------------------------------------------------------------------
# Status LED: blinks while broadcasting/waiting, goes solid once the 3DS
# (or anything) has associated with the AP.
# ---------------------------------------------------------------------------
led = Pin("LED", Pin.OUT)

# ---------------------------------------------------------------------------
# Wi-Fi AP setup
# ---------------------------------------------------------------------------
SSID = "Circle Pad"
PASSWORD = ""   # >= 8 chars for WPA2. Set to "" and SECURITY=0 for open network.
SECURITY = 0             # 3 = WPA2-PSK on most MicroPython builds; 0 = open

ap = network.WLAN(network.AP_IF)
ap.config(ssid=SSID, password=PASSWORD, security=SECURITY)
ap.active(True)

while not ap.active():
    time.sleep(0.1)

print("AP up. Pico IP:", ap.ifconfig()[0])
print('Waiting for the 3DS to connect and start InputRedirection...')
print('Check the IP shown on the 3DS Rosalina screen and confirm it matches TARGET_IP below.')

BLINK_PERIOD_MS = 400   # time between on/off toggles while nothing's connected
_last_blink = time.ticks_ms()
_led_state = False
_last_conn_state = None

def log_connection_state():
    """Print whenever a station associates with / drops from the AP."""
    global _last_conn_state
    state = ap.isconnected()
    if state != _last_conn_state:
        print("AP association changed -> station connected:", state)
        _last_conn_state = state

def update_led():
    """Call this often. Blinks while no station is associated, goes solid once one is."""
    global _last_blink, _led_state
    if ap.isconnected():
        led.on()
        return
    now = time.ticks_ms()
    if time.ticks_diff(now, _last_blink) >= BLINK_PERIOD_MS:
        _led_state = not _led_state
        led.value(_led_state)
        _last_blink = now

# ---------------------------------------------------------------------------
# Fake "internet" for the 3DS's connection test
# ---------------------------------------------------------------------------
# The 3DS's system-level connectivity check does a plain HTTP GET to
# http://conntest.nintendowifi.net/ and refuses to consider itself "online"
# (hanging or dropping the connection) unless that succeeds. On an isolated
# AP with no real internet, we spoof it: a tiny DNS server resolves that one
# domain to the Pico's own IP, and a tiny HTTP server answers any request
# with a 200 OK. Everything else is left unanswered on purpose.
CONNTEST_DOMAINS = (b"conntest.nintendowifi.net", b"conntest.pretendo.cc")
PICO_IP = ap.ifconfig()[0]
PICO_IP_BYTES = bytes(int(octet) for octet in PICO_IP.split("."))

DNS_PORT = 53
dns_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
dns_sock.setblocking(False)
dns_sock.bind(("0.0.0.0", DNS_PORT))

HTTP_PORT = 80
http_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
http_sock.setblocking(False)
http_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
http_sock.bind(("0.0.0.0", HTTP_PORT))
http_sock.listen(2)

HTTP_BODY = (
    b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" \n'
    b'    "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">\n'
    b'    <html>\n'
    b'    <head>\n'
    b'    <title>HTML Page</title>\n'
    b'    </head>\n'
    b'    <body bgcolor="#FFFFFF">\n'
    b'    This is test.html page\n'
    b'    </body>\n'
    b'    </html>'
)
HTTP_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: text/html; charset=UTF-8\r\n"
    b"Content-Length: " + str(len(HTTP_BODY)).encode() + b"\r\n"
    b"Connection: close\r\n"
    b"X-Organization: Nintendo\r\n"
    b"\r\n" + HTTP_BODY
)

_http_clients = []

def parse_dns_question(data):
    """Parse the first question's name out of a DNS query (starts at byte 12).
    Returns (list_of_label_bytes, offset_just_past_the_zero_terminator)."""
    labels = []
    pos = 12
    while True:
        length = data[pos]
        pos += 1
        if length == 0:
            break
        labels.append(data[pos:pos + length])
        pos += length
    return labels, pos

def handle_dns_packet():
    try:
        data, addr = dns_sock.recvfrom(512)
    except OSError:
        return
    if len(data) < 13:
        return
    try:
        labels, name_end = parse_dns_question(data)
    except IndexError:
        return
    qname = b".".join(labels)
    if qname.lower() not in CONNTEST_DOMAINS:
        return  # not a domain we care about — leave it unanswered
    question_end = name_end + 4  # + QTYPE(2) + QCLASS(2)
    txn_id = data[0:2]
    qdcount = data[4:6]
    header = txn_id + b"\x81\x80" + qdcount + b"\x00\x01\x00\x00\x00\x00"
    question = data[12:question_end]
    # Answer: name = pointer back to question, TYPE=A, CLASS=IN, TTL=60, RDLENGTH=4, RDATA=our IP
    answer = b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04" + PICO_IP_BYTES
    dns_sock.sendto(header + question + answer, addr)

def accept_http_client():
    try:
        conn, addr = http_sock.accept()
        conn.setblocking(False)
        _http_clients.append(conn)
    except OSError:
        pass

def service_http_client(conn):
    try:
        data = conn.recv(512)
    except OSError:
        data = None
    if data:
        try:
            conn.send(HTTP_RESPONSE)
        except OSError:
            pass
    try:
        conn.close()
    except OSError:
        pass
    if conn in _http_clients:
        _http_clients.remove(conn)

def poll_conntest_spoof():
    """Call this often from the main loop — non-blocking, services DNS + HTTP."""
    watch = [dns_sock, http_sock] + _http_clients
    readable, _, _ = select.select(watch, [], [], 0)
    for s in readable:
        if s is dns_sock:
            handle_dns_packet()
        elif s is http_sock:
            accept_http_client()
        else:
            service_http_client(s)

# ---------------------------------------------------------------------------
# Target (the 3DS) — confirm against the IP shown on-screen in Rosalina
# ---------------------------------------------------------------------------
TARGET_IP = "192.168.4.16"
TARGET_PORT = 4950

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# ---------------------------------------------------------------------------
# GPIO buttons — standard 12
# ---------------------------------------------------------------------------
BTN_PINS = {
    "A":      Pin(2,  Pin.IN, Pin.PULL_UP),
    "B":      Pin(3,  Pin.IN, Pin.PULL_UP),
    "SELECT": Pin(4,  Pin.IN, Pin.PULL_UP),
    "START":  Pin(5,  Pin.IN, Pin.PULL_UP),
    "RIGHT":  Pin(6,  Pin.IN, Pin.PULL_UP),
    "LEFT":   Pin(7,  Pin.IN, Pin.PULL_UP),
    "UP":     Pin(8,  Pin.IN, Pin.PULL_UP),
    "DOWN":   Pin(9,  Pin.IN, Pin.PULL_UP),
    "R":      Pin(10, Pin.IN, Pin.PULL_UP),
    "L":      Pin(11, Pin.IN, Pin.PULL_UP),
    "X":      Pin(12, Pin.IN, Pin.PULL_UP),
    "Y":      Pin(13, Pin.IN, Pin.PULL_UP),
}

# Bit positions inside the 32-bit button word (standard 3DS HID layout,
# confirmed against both Luma3DS's server and the Qt client's sendFrame())
BTN_BITS = {
    "A": 0, "B": 1, "SELECT": 2, "START": 3,
    "RIGHT": 4, "LEFT": 5, "UP": 6, "DOWN": 7,
    "R": 8, "L": 9, "X": 10, "Y": 11,
}

# ---------------------------------------------------------------------------
# Circle pad — digital (4 buttons, snaps to full deflection like a D-pad).
# Separate physical pins from the D-pad above; this is the actual analog
# stick's position, sent as its own field in the packet.
# ---------------------------------------------------------------------------
CPAD_PINS = {
    "UP":    Pin(14, Pin.IN, Pin.PULL_UP),
    "DOWN":  Pin(15, Pin.IN, Pin.PULL_UP),
    "LEFT":  Pin(16, Pin.IN, Pin.PULL_UP),
    "RIGHT": Pin(17, Pin.IN, Pin.PULL_UP),
}

# ---------------------------------------------------------------------------
# ZL / ZR (New 3DS only) — digital, packed alongside the C-stick word.
# ---------------------------------------------------------------------------
ZL_PIN = Pin(18, Pin.IN, Pin.PULL_UP)
ZR_PIN = Pin(19, Pin.IN, Pin.PULL_UP)

# ---------------------------------------------------------------------------
# HOME / POWER / POWER (long-press) — the "interface buttons" field.
# ---------------------------------------------------------------------------
HOME_PIN = Pin(20, Pin.IN, Pin.PULL_UP)
POWER_PIN = Pin(21, Pin.IN, Pin.PULL_UP)
POWERLONG_PIN = Pin(22, Pin.IN, Pin.PULL_UP)

# ---------------------------------------------------------------------------
# C-stick — analog, via a 2-axis joystick module.
# Wire the module's X/Y wipers to GP26 (ADC0) and GP27 (ADC1), and its
# power/ground to 3V3/GND. GP28 (ADC2) is left free/spare.
# ---------------------------------------------------------------------------
cstick_x_adc = ADC(Pin(26))
cstick_y_adc = ADC(Pin(27))

# ---------------------------------------------------------------------------
# C-stick calibration — most analog joystick modules don't actually swing
# the full 0-65535 ADC range at physical full deflection, so these need to
# be measured for your specific hardware. Set CALIBRATE = True, reflash,
# and watch the console while moving the stick to each extreme and letting
# it rest at center — note the raw values it prints, then plug them in below
# and set CALIBRATE = False again.
CALIBRATE = False

CSTICK_X_MIN, CSTICK_X_CENTER, CSTICK_X_MAX = -34464, 33016, 100000
CSTICK_Y_MIN, CSTICK_Y_CENTER, CSTICK_Y_MAX = -34464, 33016, 100000

def read_axis(adc, min_val, center_val, max_val):
    """Normalize a raw ADC reading to -1.0..1.0 using measured calibration,
    handling the min->center and center->max halves separately since most
    joysticks aren't perfectly symmetric around their center point."""
    raw = adc.read_u16()
    if raw >= center_val:
        span = max_val - center_val
        value = 0.0 if span <= 0 else (raw - center_val) / span
    else:
        span = center_val - min_val
        value = 0.0 if span <= 0 else (raw - center_val) / span
    return max(-1.0, min(1.0, value))

if CALIBRATE:
    print("Calibration mode: move the C-stick to each extreme and let it")
    print("rest at center, noting the raw values below. Ctrl-C when done.")
    while True:
        print("raw X:", cstick_x_adc.read_u16(), " raw Y:", cstick_y_adc.read_u16())
        time.sleep(0.2)

BUTTONS_NEUTRAL = 0x00000FFF   # nothing pressed
TOUCH_NEUTRAL = 0x02000000     # not touching
CPAD_NEUTRAL = 0x007FF7FF      # circle pad centered
CPP_NEUTRAL = 0x80800081       # C-stick centered, ZL/ZR released

CPAD_BOUND = 0x5d0  # 1488 — matches the official Qt client's scaling exactly
CPP_BOUND = 0x7f    # 127  — matches the official Qt client's scaling exactly
SQRT_HALF = 0.70710678118654752440

def read_buttons():
    word = BUTTONS_NEUTRAL
    for name, pin in BTN_PINS.items():
        if pin.value() == 0:  # pulled low = pressed
            word &= ~(1 << BTN_BITS[name])
    return word

def pack_circle_pad():
    """Digital circle pad: reads CPAD_PINS, snaps to full deflection."""
    lx = ly = 0.0
    if CPAD_PINS["LEFT"].value() == 0:
        lx -= 1.0
    if CPAD_PINS["RIGHT"].value() == 0:
        lx += 1.0
    if CPAD_PINS["UP"].value() == 0:
        ly += 1.0
    if CPAD_PINS["DOWN"].value() == 0:
        ly -= 1.0
    if lx == 0.0 and ly == 0.0:
        return CPAD_NEUTRAL
    x = int(lx * CPAD_BOUND) + 0x800
    y = int(ly * CPAD_BOUND) + 0x800
    x = 0xfff if x >= 0xfff else (0x000 if x < 0 else x)
    y = 0xfff if y >= 0xfff else (0x000 if y < 0 else y)
    return (y << 12) | x

def pack_cstick():
    """Analog C-stick from ADC, plus ZL/ZR — packed exactly like the Qt client
    (including its 45-degree rotation of the raw stick position)."""
    rx = read_axis(cstick_x_adc, CSTICK_X_MIN, CSTICK_X_CENTER, CSTICK_X_MAX)
    ry = read_axis(cstick_y_adc, CSTICK_Y_MIN, CSTICK_Y_CENTER, CSTICK_Y_MAX)
    ir_buttons_state = 0
    if ZR_PIN.value() == 0:
        ir_buttons_state |= 0x2
    if ZL_PIN.value() == 0:
        ir_buttons_state |= 0x4
    # Small deadzone so a resting joystick doesn't jitter around center
    if abs(rx) < 0.05 and abs(ry) < 0.05 and ir_buttons_state == 0:
        return CPP_NEUTRAL
    x = int(SQRT_HALF * (rx + ry) * CPP_BOUND) + 0x80
    y = int(SQRT_HALF * (ry - rx) * CPP_BOUND) + 0x80
    x = 0xff if x >= 0xff else (0x00 if x < 0 else x)
    y = 0xff if y >= 0xff else (0x00 if y < 0 else y)
    return (y << 24) | (x << 16) | (ir_buttons_state << 8) | 0x81

def read_interface_buttons():
    word = 0
    if HOME_PIN.value() == 0:
        word |= 1
    if POWER_PIN.value() == 0:
        word |= 2
    if POWERLONG_PIN.value() == 0:
        word |= 4
    return word

def build_packet():
    buttons = read_buttons()
    circle_pad = pack_circle_pad()
    cstick = pack_cstick()
    interface = read_interface_buttons()
    # Always send the full 20-byte packet (buttons, touch, circle pad,
    # C-stick/ZL/ZR, interface buttons) — matches what the real client sends.
    return struct.pack("<IIIII", buttons, TOUCH_NEUTRAL, circle_pad, cstick, interface)

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
SEND_HZ = 30
PERIOD = 1.0 / SEND_HZ

print("Streaming full controller state to %s:%d at %d Hz. Ctrl-C to stop." % (TARGET_IP, TARGET_PORT, SEND_HZ))

try:
    while True:
        update_led()
        log_connection_state()
        poll_conntest_spoof()
        packet = build_packet()
        try:
            sock.sendto(packet, (TARGET_IP, TARGET_PORT))
        except OSError as e:
            # Most commonly: no route / host unreachable if the 3DS hasn't
            # connected yet or InputRedirection isn't running on it.
            print("send failed:", e)
        time.sleep(PERIOD)
except KeyboardInterrupt:
    print("Stopped.")
finally:
    led.off()
    sock.close()
    dns_sock.close()
    http_sock.close()
    for c in _http_clients:
        c.close()