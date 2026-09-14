# movehub-88019

<p align="center">
  <img src="https://img.shields.io/badge/hub-Technic%20Move%2088019-00A8E8" alt="Technic Move Hub 88019" />
  <img src="https://img.shields.io/badge/set-42176%20%2B%2042214%20%2B%2042239-D5001C" alt="LEGO sets" />
  <img src="https://img.shields.io/badge/python-3.11+-3776AB" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/dependency-bleak%20only-3DDC84" alt="Single dependency: bleak" />
  <img src="https://img.shields.io/badge/license-MIT-yellow" alt="MIT license" />
</p>

Open-source Python driver and interactive keyboard controller for the **LEGO® Technic Move Hub 88019** — the hub from set **42176** (Porsche GT4 e-Performance), also used in sets **42214** and **42239**. It talks pure **Bluetooth Low Energy** with the **LEGO Wireless Protocol 3 (LWP3)**:

- **no LEGO app** (Control+ / Powered Up), no account, no phone
- **no firmware replacement** — stock firmware is enough (custom firmware can't be flashed anyway: the bootloader is locked)
- Linux + BlueZ, Python 3.11+, a single dependency ([bleak](https://github.com/hbldh/bleak))

> Not affiliated with or endorsed by the LEGO Group. LEGO® is a trademark of the LEGO Group.
> Everything here was verified live on a real hub (stock firmware, 2026).

## What's inside

- **`movehub.py`** — async driver library: scan → connect → pair, VM handshake, steering calibration, continuous 20 Hz drive stream, direct motor power, status LED, battery voltage, accelerometer, safe session end
- **`controller.py`** — interactive terminal controller (AUTO drive mode + ROBOT motor mode, telemetry, watchdog, script mode)
- **`demo.py`** — end-to-end demo drive
- **`docs/protocol.md`** — reverse-engineered protocol notes: port map, frames, flags, crash map
- **`probes/`** — the 15 probe scripts that mapped the protocol, with their JSON session logs

## Critical rules (learned the hard way)

1. **Always end the session with `HUB_ACTION_DISCONNECT` (`04 00 02 02`) before the BLE disconnect.** If you just drop the link, the hub's drive VM refuses **all** drive frames in the **next** session with `ERR 0x05` — and that state **survives power-cycles**. The library does this automatically in `close()`.
2. **Connect first, pair (bond) second.** Pairing before connecting ends in `AuthenticationTimeout`. The hub requires bonding (Security Mode 1 Level 2); an unencrypted link silently ignores commands and is dropped after ~30 s.
3. **Never write to ports ≥ `0x38`** (internal sensors) — that freezes/crashes the hub. Recovery: power cycle.
4. **Never set a sensor notification delta of 0** — that triggers a ~250 Hz notification storm and the hub crashes within seconds. A delta of `0xFFFF` is silently ignored; use a real delta.
5. **One BLE connection at a time** — the LEGO app / handset must be disconnected.
6. `speed 0` = FLOAT, not brake. `0x7F` (127) = BRAKE.
7. **The six headlights are all-or-nothing** over LWP3: on/off + automatic brake lights. Port `0x35` is a read-only state bitmap (9 write formats tested, all rejected). Per-light control would require different firmware.
8. **USB-C on the hub is charge-only** — the SoC (TI CC2642) has no USB data peripheral. BLE is the only control path.
9. **Steering calibration moves the steering physically** — run it with the wheels free, and re-run it on every new build (end stops depend on the mechanics).

Full details: [`docs/protocol.md`](docs/protocol.md).

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt        # bleak

# find your hub (press the green button on the hub first)
python3 - <<'EOF'
import asyncio
from bleak import BleakScanner

async def scan():
    for d in await BleakScanner.discover(timeout=10):
        if d.name and "Technic" in d.name:
            print(d.address, d.name)

asyncio.run(scan())
EOF

export MOVEHUB_ADDR=AA:BB:CC:DD:EE:FF  # optional; empty = auto-discover by name
python3 demo.py                        # end-to-end test drive
python3 controller.py                  # interactive
```

**First run ever:** the hub must be bonded. Either pair once with `bluetoothctl` (`connect` first, then `pair`, then `trust`), or construct `MoveHub(pair_if_needed=True)` in your own script.

## Controller

### AUTO mode (VM drive — the car)

- `W` / `S` — speed +10 / −10 (held, like a throttle)
- `A` / `D` — steering −10 / +10
- arrow keys — same as W/S/A/D
- `L` — lights on/off · `B` — brake (momentary, brake lights) · `SPACE` — emergency stop
- `C` — steering calibration (the steering sweeps!)
- `V` — battery voltage · `T` — accelerometer XYZ
- `M` — switch to ROBOT mode · `Q` / `ESC` — quit (safe shutdown) · `?` — help

### ROBOT mode (direct motors — custom builds)

- `I`/`K` — motor A ±10 · `J`/`L` — motor B ±10 · `U`/`O` — motor C (servo) ±10
- `X` — brake all motors
- `M` — back to AUTO mode · `V` / `T` / `Q` / `?` — same as above

### Script mode (autonomous runs, no terminal needed)

```bash
python3 controller.py --script "wwd@"                 # replay keys, 0.35 s apart
python3 controller.py --script "wwd@ss@" --final-wait 5   # + silence at the end
```

`@` = 1.2 s pause. Available keys: `w a s d` (drive), `l` lights, `b` brake, space = stop, `c` calibrate, `v` voltage, `t` IMU. The watchdog auto-stops 2 s after the last scripted input and the session always ends cleanly — safe for daemons/cron.

## Library

```python
import asyncio
from movehub import MoveHub, MOTOR_A, MOTOR_B, MOTOR_C, BRAKE

async def main():
    async with MoveHub() as hub:            # scan + connect (auto-discovery)
        await hub.handshake()               # subscribe + ARM the drive VM
        await hub.calibrate()               # steering sweep (physical!)
        await hub.start_stream()            # 20 Hz drive frames = keepalive
        hub.set_drive(speed=30, steer=20, lights=True)
        await asyncio.sleep(2)
        hub.set_drive(0, 0, brake=True)
        await hub.motor_power(MOTOR_A, 50)  # direct motor, -100..100
        await hub.set_led("green")          # status LED
        print(await hub.read_voltage(), "mV")
        print(await hub.read_accel(), "mG")
    # leaving the context = safe shutdown incl. HUB_ACTION_DISCONNECT

asyncio.run(main())
```

API summary:

- `MoveHub(address=None, pair_if_needed=False)` — address defaults to `$MOVEHUB_ADDR` or name-based auto-discovery
- `await hub.handshake()` / `await hub.calibrate()` / `await hub.start_stream()`
- `hub.set_drive(speed, steer, lights=True, brake=False, eco=False)` — speed −100..100, steer ±70
- `await hub.motor_power(motor, power)` — A/B/C, power −100..100, `BRAKE`, `FLOAT`
- `await hub.set_led(color)` — named color or LWP3 palette index
- `await hub.read_voltage()` → mV · `await hub.read_accel()` → (x, y, z) mG
- `await hub.read_port(port, mode, delta)` — generic one-shot sensor read (the only working path on this firmware)
- `await hub.close()` — always safe: lights off + `HUB_ACTION_DISCONNECT`

## Probes

`probes/` contains the probe series that mapped the protocol on a real hub. Each script is standalone and logs every BLE frame to JSON (`probes/logs/`). The progression:

- `probe_m0.py` (v4) — first connect→pair bonding flow
- `probe_v5.py` — light bitmap hunt; discovered the delta-0 notification storm crash
- `probe_v6.py` — full working sequence; discovered the write-to-0x38+ crash
- `probe_v7.py` — safe read-only discovery: mode info for every port 0x32–0x40
- `probe_v8.py` — per-light write hunt + motor identification spins
- `probe_v9.py` — 9 write formats for port 0x35, all rejected (lights are read-only)
- `probe_v10`–`v12` — VM flags/tail/mode sweeps; VM state machine decoded
- `probe_v13.py` — root cause found: the `HUB_ACTION_DISCONNECT` session-end requirement
- `probe_v14.py` — confirmed the continuous 20 Hz drive stream pattern
- `probe_v15.py` — bond-removal / re-pair recovery + a full 20 Hz test drive

Run one with `MOVEHUB_ADDR` set (or empty for name discovery) — but read [`docs/protocol.md`](docs/protocol.md) §6 first: some probes intentionally trigger the crash modes and need a power cycle afterwards.

## Sources & credits

This project stands on earlier community reverse-engineering — read these first if you dig into the protocol:

- **[DanieleBenedettelli/TechnicMoveHub](https://github.com/DanieleBenedettelli/TechnicMoveHub)** (MIT) — ESP32 reference for the connect→pair bonding flow on this hub
- **[maxswinkels/42176-controller](https://github.com/maxswinkels/42176-controller)** (MIT) — VM handshake, the continuous 20 Hz drive stream, session-end semantics and the flags byte; our driver adopted those proven elements
- **[Pybricks](https://github.com/pybricks/pybricks-micropython)** — `pb_type_iodevices_lwp3device.c` (MIT), the reference LWP3 drive-device implementation
- **[LEGO BLE Wireless Protocol docs](https://lego.github.io/lego-ble-wireless-protocol-docs/)** — the official LWP3 specification

Our contribution: the independent probe series v3–v15 (full port-map verification, the crash map, proof of the lights limitation, root cause of the session-lock `ERR 0x05`), a standalone async Python driver on bleak, and the two-mode controller with watchdog and script mode.

## License

MIT — see [LICENSE](LICENSE).
