# Technic Move Hub 88019 — hardware & LWP3 protocol notes

Reverse-engineering notes for the **LEGO® Technic Move Hub 88019** — the hub shipped with set **42176** (Porsche GT4 e-Performance), also used in sets **42214** and **42239** (Batmobile Tumbler). Everything below was mapped live against a real hub running stock firmware, using the probe scripts in [`../probes/`](../probes/) (session logs in `probes/logs/`).

Legend: ✅ verified on our hub · ⚠️ community knowledge (not independently verified) · ❓ still open

## Sources

- [DanieleBenedettelli/TechnicMoveHub](https://github.com/DanieleBenedettelli/TechnicMoveHub) (MIT) — connect-then-pair bonding flow on ESP32
- [maxswinkels/42176-controller](https://github.com/maxswinkels/42176-controller) (MIT) — VM handshake, 20 Hz drive stream, session-end semantics, flags byte
- [Pybricks `pb_type_iodevices_lwp3device.c`](https://github.com/pybricks/pybricks-micropython/blob/master/pybricks/iodevices/pb_type_iodevices_lwp3device.c) (MIT) — reference drive-device implementation
- [LEGO BLE Wireless Protocol docs](https://lego.github.io/lego-ble-wireless-protocol-docs/) — the LWP3 specification
- Our probe series v3–v15 (logs in `probes/logs/`)

## 1. Hardware

- **Sets:** 42176 (Porsche GT4 e-Performance), 42214, 42239 (Batmobile Tumbler). Hub part `33098103479c01`, no. **88019**, ~16×9×6 studs, **no external ports**.
- **Brain:** TI CC2642 (BLE SoC, single chip) → **USB-C is charge-only** ✅ (`lsusb` shows no LEGO VID 0694; the CC2642 has no USB data peripheral).
- **Battery:** built-in Li-Ion 3.6 V / 2100 mAh, charged over USB-C. Ours read **4024 mV** ✅.
- **Motors:** A + B rear drive (side + rear outputs, **they spin in opposite directions**), C front servo with planetary gearing and a magnetic encoder.
- **Lights:** 6 LEDs (4 front, channels 1–4; 2 rear, channels 5–6) + light pipes.
- **Power button + LED** on the bottom. Short press = on; the hub then advertises for ~20–120 s (the advertisement disappears if nothing connects). A connected hub glows blue.
- **The bootloader is locked** (special password; a long button press is only off/reset) → custom firmware (Pybricks) **cannot** be flashed. Not a problem — LWP3 already exposes direct control of every motor.

## 2. BLE connection ✅

- **Name:** `Technic Move` · **Service:** `00001623-1212-EFDE-1623-785FEABCD123` · **Characteristic (write+notify):** `00001624-1212-EFDE-1623-785feabcd123`
- **Manufacturer data (company ID 919):** `00 84 02 FF 01 00` (hub type 0x84) ✅
- **The connection requires bonding (Security Mode 1, Level 2). Correct order: CONNECT → PAIR.** On Linux with bleak: `client.connect()` → `client.pair()` (works reliably ✅; afterwards `bluetoothctl info` shows Paired/Bonded/Trusted and later connections are encrypted automatically).
- **Pairing before connecting ends in `AuthenticationTimeout`** ✅ — don't do it.
- **Without encryption the hub silently ignores commands and drops the link after ~30 s** ✅.
- **Only one BLE connection at a time** — the LEGO app / handset must be disconnected.
- After connecting, the hub sends the **port map** by itself (Hub Attached I/O, `0x0f 0x00 0x04 <port> <io type u16 LE> ...`) — 15 entries ✅.

## 3. Port map ✅ (probed)

| Port | IO type | Meaning | Notes |
|---|---|---|---|
| `0x32` | 0x0056 | motor A (drive, tacho) | 5 modes; direct `51 00` write moves the motor ✅ |
| `0x33` | 0x0056 | motor B (drive, tacho) | same as A ✅ (independent control) |
| `0x34` | 0x0057 | motor C (servo, tacho) | steer via VM `0x36`; direct power also moves it ✅ |
| `0x35` | 0x0058 | **read-only light-state sensor (bitmap 0..63)** ✅ | listed as OUTPUT in the port map, but **every write is rejected** (ERR 0x06/0x05, all 9 tested formats) → **per-light control is NOT possible** over LWP3; the firmware exposes lights only through VM flags |
| `0x36` | 0x0059 | **virtual "drive VM" port** | firmware bytecode VM; requires the handshake (§4.0) ✅; calibration + drive frames ACK ✅ |
| `0x37` | 0x003C | **steering sensor (DEG ±90°)** ✅ | mode 1 = 32-bit int LE; stream units 0x2c/0x2d |
| `0x38` | 0x0039 | **accelerometer (mG)** ✅ | X/Y/Z 32-bit int LE; **writing here CRASHES the hub** |
| `0x39` | 0x003A | **gyroscope (DPS)** ✅ | 3× 32-bit int LE |
| `0x3A` | 0x003B | **tilt (deg)** ✅ | 3× 32-bit int LE |
| `0x3B` | 0x0041 | **quaternion** ✅ | 4× 32-bit int LE |
| `0x3C` | 0x0014 | **voltage** ✅ | one-shot via input-format delta 0x64 → `45 3C <mV u16 LE>` |
| `0x3D` | 0x005C | 4× "Var" ❓ | output modes; meaning unknown |
| `0x3E` | 0x005E | ❓ (0..255) | |
| `0x3F` | 0x0017 | **RGB status LED** ✅ | write `81 3F 11 51 00 <color>` ✅ |
| `0x40` | 0x005F | ❓ (0..255) | |

Official LWP3 IO type IDs: 0x14 voltage, 0x15 current, 0x17 RGB light, 0x27 internal motor w/ tacho, … Types 0x56–0x5F are Move-Hub-specific (not in the official table).

## 4. Commands (LWP3 WriteDirect `0x81`)

Frame: `len, 0x00, 0x81, port, startup/completion, 0x51 (WriteDirectModeData), mode, …payload`.
Replies: PortOutputCommandFeedback `05 00 82 <port> <status>` (status 0x0A = done) or Error `05 00 05 81 <code>` (0x05 = command not recognized, 0x06 = illegal argument).

### 4.0 VM handshake — mandatory ✅ (from 42176-controller, verified)

The drive VM on port `0x36` requires, before first use:

1. `0a 00 41 36 00 01 00 00 00 01` — PortInputFormatSetup (subscribe, 1 Hz)
2. `05 00 21 36 00` — PortInformationRequest (state)
3. state → if `02` (STOPPED): `08 00 81 36 11 51 00 01` (START), then ARM with `09 00 81 36 11 51 00 04 01`
4. if RUNNING: ARM directly with `09 00 81 36 11 51 00 04 01`

VM states (from `0c 00 45 36 03 <id> …`): `01`=RUNNING, `02`=STOPPED, `03`=EXECUTING. Encoder feedback (byte 6 of `45 36 03` streams) = motor C position.

### 4.1 Steering calibration (after handshake, VM) ✅ ACK, physically sweeps the steering

```
0d 00 81 36 11 51 00 03 00 00 00 10 00
0d 00 81 36 11 51 00 03 00 00 00 08 00
```

Re-run on every new build (end stops depend on the mechanics). The steering MOVES during calibration.

### 4.2 Drive frame (VM, 20 Hz stream) ✅

```
0d 00 81 36 11 51 00 03 00 | speed | steer | flags | 00
```

- `speed` −100..100; `steer` signed ±70 (mechanical end stops); `flags` see §5.
- **Send continuously at ~20 Hz** (like the reference driver; also acts as a keepalive).

### 4.3 Direct motor (A/B/C) ✅

```
08 00 81 <32|33|34> 00 51 00 <power>   # power −100..100, 0x7F=BRAKE, 0x00=FLOAT
```

⚠️ Community reports angle-target (goto) commands on A/B crash the hub — stick to power. A and B are fully independent ✅.

### 4.4 Status LED (0x3F) ✅

```
08 00 81 3F 11 51 00 <colorID>   # LWP3 palette: 01 pink, 02 purple, 03 blue, 04 lightblue,
                                 # 05 cyan, 06 green, 07 yellow, 08 orange, 09 red, 0A white
```

### 4.5 Reading sensor values

- `PortValueRequest` (`04 00 42 <port>`): **does not work** ✅ (no reply at all; the hub ignores it).
- `PortInputFormatSetup` `0a 00 41 <port> <mode> <delta u32 LE> <notify>` ✅ — the hub sends the current value immediately and then streams `45 <port> …`. **A delta of 0xFFFF is silently ignored** ✅ — use a real delta (e.g. 0x64 = 100 mV for voltage). **A delta of 0 → ~250 Hz notification storm → the hub crashes** ✅.
- One-shot read: enable notify → read the value → disable again (`… 00 00 00 00 00`).

## 5. Flags byte (byte 11 of the drive frame) ✅ (42176-controller + our tests)

| Bit | Meaning | Verified |
|---|---|---|
| `0x01` | BRAKE (brakes + brake lights + holds steering) | ⚠️ community, visually ✅ |
| `0x02` | POWER_LIMIT (eco mode) | ⚠️ |
| `0x04` | lights off | ✅ VM 0x00/0x04 |
| `0x08`/`0x10` | INIT / CALIBRATE | ✅ |
| `0x20`/`0x40`/`0x80` | **no effect** (ARM test: all ACKed, nothing visible) | ✅ negative result |

**Lights = ON/OFF + automatic brake lights only. Per-light control is NOT possible over LWP3** ✅ (0x35 read-only, 9 formats rejected; flags 0x20–0x80 no effect). Per-light control would need a firmware change — out of scope.

## 6. Pitfalls & crashes ✅

- **Unencrypted link: commands silently fail, disconnect after ~30 s.**
- **Writing to unknown internal ports (≥ 0x38) freezes/crashes the hub.** Recovery: power cycle.
- **Notification storm (delta 0) crashes the hub within ~2 s.**
- **⛔ Unclean session end = the biggest trap:** closing the BLE link with just `client.disconnect()` — without `HUB_ACTION_DISCONNECT` — leaves the hub's drive VM bound to the dead session. The **next** session gets **ERR 0x05 for every drive frame** (calibration included). This state **survives power-cycles** and only heals after another clean session-end cycle. The library therefore ALWAYS sends `04 00 02 02` before disconnecting. (The hub replies `04 00 02 31` and closes the link itself.)
- **The VM accepts commands even when not running** — without the handshake it just ACKs and does nothing (easily misdiagnosed as an error).
- Speed 0 = FLOAT, not brake; 127 = BRAKE.
- One BLE connection at a time.

## 7. Open questions ❓

- Meaning of ports 0x3D (4 "Var" modes), 0x3E, 0x40 (0..255).
- Byte 12 (tail) of the drive frame — no effect for all 8 tested values.
- Which of 0x32/0x33 is the left/right motor.
- `SetDecTime` and other VM modes (mode bytes 0x00/0x01/0x02/0x04/0x05 — no effect in our tests).
