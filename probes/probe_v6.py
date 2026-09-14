#!/usr/bin/env python3
"""M0 probe v6 — Technic Move Hub: full working sequence + light hunt.
1) VM calibration (steering will MOVE - hub must be free)
2) VM lights on/off (drive frame, speed 0)
3) Direct-write hunt on unknown ports (light array candidates)
4) Single-shot value reads (PortValueRequest 0x42) - voltage/button/encoders
5) Motor A/B identification spins
Bond exists; plain connect."""
import asyncio, datetime, json, traceback, os
from bleak import BleakScanner, BleakClient

ADDR = os.environ.get("MOVEHUB_ADDR", "")  # your hub MAC; empty = discover by name
CHAR = "00001624-1212-efde-1623-785feabcd123"
LOG_PATH = "probe_m0_log_v6.json"

log = []

def ts():
    return datetime.datetime.now().isoformat(timespec="milliseconds")

def note(tag, data):
    entry = {"t": ts(), "tag": tag}
    if isinstance(data, (bytes, bytearray)):
        entry["hex"] = bytes(data).hex()
    else:
        entry["msg"] = str(data)
    log.append(entry)
    print(entry["t"], tag, entry.get("hex", entry.get("msg")), flush=True)

def savelog():
    with open(LOG_PATH, "w") as f:
        json.dump(log, f, indent=1)

async def find_hub(timeout_s=300):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        try:
            dev = await BleakScanner.find_device_by_filter(
                lambda d, ad: (d.address and d.address.upper() == ADDR)
                or "Technic Move" in (ad.local_name or ""),
                timeout=15.0)
        except Exception as e:
            note("scan_error", repr(e))
            await asyncio.sleep(2)
            continue
        if dev:
            note("found", dev.address)
            return dev
        print("  ...rescanning", flush=True)
    return None

async def try_write(client, data, tag, wait=0.35):
    note("write:" + tag, bytes(data))
    try:
        await asyncio.wait_for(
            client.write_gatt_char(CHAR, bytes(data), response=False), timeout=8)
        await asyncio.sleep(wait)
        return "ok"
    except Exception as e:
        note("write_error:" + tag, repr(e))
        return repr(e)

def vm_drive(speed=0, steer=0, flags=0x00):
    return bytes([0x0D, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x03, 0x00,
                  speed & 0xFF, steer & 0xFF, flags & 0xFF, 0x00])

async def main():
    dev = await find_hub()
    if not dev:
        note("abort", "hub not found")
        return
    client = BleakClient(dev, timeout=25,
                         disconnected_callback=lambda c: note("disconnected", "cb"))
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
        note("connected", client.is_connected)

        def handle(_c, data):
            note("notify", bytes(data))

        try:
            await asyncio.wait_for(client.start_notify(CHAR, handle), timeout=15)
            note("notify_started", True)
        except Exception as e:
            note("notify_error", repr(e))
        await asyncio.sleep(1.0)

        # bond check: status LED -> green
        r = await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x3F, 0x11, 0x51, 0x00, 0x06]),
            "led_green")
        if r != "ok":
            note("bond_fail", r)

        # ---- 1) VM calibration (STEERING MOVES) ----
        await try_write(client,
            bytes([0x0D, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x03, 0x00,
                   0x00, 0x00, 0x10, 0x00]), "vm_setup1", wait=0.5)
        await try_write(client,
            bytes([0x0D, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x03, 0x00,
                   0x00, 0x00, 0x08, 0x00]), "vm_setup2", wait=2.5)

        # ---- 2) VM lights ON (speed 0) then OFF ----
        await try_write(client, vm_drive(0, 0, 0x00), "vm_lights_ON", wait=1.2)
        await try_write(client, vm_drive(0, 0, 0x04), "vm_lights_OFF", wait=1.0)
        await try_write(client, vm_drive(0, 0, 0x00), "vm_lights_ON2", wait=1.0)

        # ---- 3) direct-write hunt on unknown ports ----
        for port in (0x38, 0x39, 0x3A, 0x3B, 0x3E, 0x40):
            await try_write(client,
                bytes([0x08, 0x00, 0x81, port, 0x11, 0x51, 0x00, 0x3F]),
                f"hunt_{port:02x}_m0_3F")
        # retry 0x35 now (after VM setup): bitmap all-on, startup 0x00
        await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x35, 0x00, 0x51, 0x00, 0x3F]),
            "hunt_35_m0_3F")

        # ---- 4) single-shot value reads ----
        for port in (0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x3C):
            await try_write(client, bytes([0x04, 0x00, 0x42, port]),
                            f"valuereq_{port:02x}", wait=0.3)

        # ---- 5) motor identification spins ----
        await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x32, 0x00, 0x51, 0x00, 0x1E]),
            "motorA_spin", wait=1.0)
        await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x32, 0x00, 0x51, 0x00, 0x7F]),
            "motorA_brake", wait=0.6)
        await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x33, 0x00, 0x51, 0x00, 0x1E]),
            "motorB_spin", wait=1.0)
        await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x33, 0x00, 0x51, 0x00, 0x7F]),
            "motorB_brake", wait=0.6)

        # steering sweep via VM
        await try_write(client, vm_drive(0, 40, 0x00), "vm_steer_right40", wait=1.0)
        await try_write(client, vm_drive(0, -40, 0x00), "vm_steer_left40", wait=1.0)
        await try_write(client, vm_drive(0, 0, 0x00), "vm_steer_center", wait=0.8)

        # lights off + stop
        await try_write(client, vm_drive(0, 0, 0x04), "vm_final_off")
        for port in (0x32, 0x33, 0x34):
            await try_write(client,
                bytes([0x08, 0x00, 0x81, port, 0x00, 0x51, 0x00, 0x7F]),
                f"final_brake_{port:02x}")

        await asyncio.wait_for(client.disconnect(), timeout=10)
        note("done", "clean exit")
    except Exception as e:
        note("fatal", repr(e))
        traceback.print_exc()
    savelog()
    print("log entries:", len(log))

asyncio.run(main())
