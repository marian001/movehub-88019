#!/usr/bin/env python3
"""M0b probe v10 — VM flags bits + tail byte + VM mode sweep.
Drive frames (port 0x36) never crashed the hub, so this is low-risk.
For each probe: lights ON baseline first, then the probe, watch lights,
restore. Also try VM modes 1,2,4,5 with small payloads (error vs ACK)."""
import asyncio, datetime, json, traceback, os
from bleak import BleakScanner, BleakClient

ADDR = os.environ.get("MOVEHUB_ADDR", "")  # your hub MAC; empty = discover by name
CHAR = "00001624-1212-efde-1623-785feabcd123"
LOG_PATH = "probe_m0_log_v10.json"

log = []
notify_event = asyncio.Event()

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

def on_notify(_c, data):
    note("notify", bytes(data))
    notify_event.set()

async def w(client, data, tag, wait=0.3, feedback=True):
    note("write:" + tag, bytes(data))
    notify_event.clear()
    try:
        await asyncio.wait_for(
            client.write_gatt_char(CHAR, bytes(data), response=False), timeout=8)
    except Exception as e:
        note("write_error:" + tag, repr(e))
        return False
    ok = True
    if feedback:
        try:
            await asyncio.wait_for(notify_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            note("no_feedback", tag)
            ok = False
    await asyncio.sleep(wait)
    return ok

def vm(speed, steer, flags, tail=0x00, mode=0x03):
    return bytes([0x0D, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, mode,
                  speed & 0xFF, steer & 0xFF, flags & 0xFF, tail & 0xFF])

LED = lambda color: bytes([0x08, 0x00, 0x81, 0x3F, 0x11, 0x51, 0x00, color])

async def find_hub(timeout_s=600):
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
        print("  ...rescanning (press green button)", flush=True)
    return None

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
        await asyncio.wait_for(client.start_notify(CHAR, on_notify), timeout=15)
        note("notify_started", True)
        await asyncio.sleep(1.0)

        # calibration first (VM needs it)
        await w(client, vm(0, 0, 0x10), "calib1", wait=0.6)
        await w(client, vm(0, 0, 0x08), "calib2", wait=2.5)

        # ---------- flags bit sweep (lights baseline ON before each) ----------
        note("phase", "flags sweep — WATCH LIGHTS + STEERING")
        for bit, name in ((0x20, "f20"), (0x40, "f40"), (0x80, "f80"),
                          (0x24, "f24"), (0x28, "f28")):
            await w(client, vm(0, 0, 0x00), f"base_on_{name}", wait=0.6)
            await w(client, vm(0, 0, bit), f"flags_{name}", wait=1.8)
            await w(client, vm(0, 0, 0x04), f"off_{name}", wait=0.5)

        # ---------- tail byte sweep ----------
        note("phase", "tail byte sweep")
        for tail in (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80):
            await w(client, vm(0, 0, 0x00), f"t_base_{tail:02x}", wait=0.5)
            await w(client, vm(0, 0, 0x00, tail=tail), f"tail_{tail:02x}", wait=1.5)
            await w(client, vm(0, 0, 0x04), f"t_off_{tail:02x}", wait=0.4)

        # ---------- VM mode sweep (mode byte != 3) ----------
        note("phase", "VM mode sweep")
        for mode in (0x00, 0x01, 0x02, 0x04, 0x05):
            await w(client, vm(0, 0, 0x00, mode=mode), f"vmode_{mode:02x}", wait=1.5)

        # ---------- final ----------
        await w(client, vm(0, 0, 0x04), "final_lights_off", wait=0.5)
        await w(client, LED(0x06), "final_led_green", wait=0.3)
        await asyncio.wait_for(client.disconnect(), timeout=10)
        note("done", "clean exit")
    except Exception as e:
        note("fatal", repr(e))
        traceback.print_exc()
    savelog()
    print("log entries:", len(log))

asyncio.run(main())
