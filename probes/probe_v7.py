#!/usr/bin/env python3
"""M0 probe v7 — Technic Move Hub: SAFE discovery, zero risky writes.
Read-only: mode info for ALL ports 0x32..0x40, all modes 0..5, info types
1,2,5,7 (name/raw/symbol/format). No PortOutput writes except known-good
(VM calibration + lights + status LED). This maps everything the hub will
tell us, including per-light capabilities."""
import asyncio, datetime, json, traceback, os
from bleak import BleakScanner, BleakClient

ADDR = os.environ.get("MOVEHUB_ADDR", "")  # your hub MAC; empty = discover by name
CHAR = "00001624-1212-efde-1623-785feabcd123"
LOG_PATH = "probe_m0_log_v7.json"

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

async def try_write(client, data, tag, wait=0.3):
    note("write:" + tag, bytes(data))
    try:
        await asyncio.wait_for(
            client.write_gatt_char(CHAR, bytes(data), response=False), timeout=8)
        await asyncio.sleep(wait)
        return "ok"
    except Exception as e:
        note("write_error:" + tag, repr(e))
        return repr(e)

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

        # ---- full mode info sweep (read-only) ----
        for port in range(0x32, 0x41):
            # port info: capabilities + mode count
            for itype in (0x01, 0x02):
                await try_write(client,
                    bytes([0x05, 0x00, 0x21, port, itype]),
                    f"portinfo_{port:02x}_t{itype:02x}")
            for mode in range(0, 6):
                for it in (0x01, 0x02, 0x05, 0x07):
                    await try_write(client,
                        bytes([0x06, 0x00, 0x22, port, mode, it]),
                        f"modeinfo_{port:02x}_m{mode}_t{it:02x}", wait=0.18)

        # ---- known-good writes to confirm link alive at end ----
        await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x3F, 0x11, 0x51, 0x00, 0x05]),
            "led_magenta_endtest")

        await asyncio.wait_for(client.disconnect(), timeout=10)
        note("done", "clean exit")
    except Exception as e:
        note("fatal", repr(e))
        traceback.print_exc()
    savelog()
    print("log entries:", len(log))

asyncio.run(main())
