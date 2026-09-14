#!/usr/bin/env python3
"""M0 probe v5 — Technic Move Hub: light bitmap (port 0x35) + sensor input.
Bond already exists, so plain connect should encrypt. Focus:
  A) find the working write format for port 0x35 (0..63 bitmap = 6 lights)
  B) enable value notifications on motors (PortInputFormatSetup 0x41)
  C) read values while spinning a motor briefly"""
import asyncio, datetime, json, traceback, os
from bleak import BleakScanner, BleakClient

ADDR = os.environ.get("MOVEHUB_ADDR", "")  # your hub MAC; empty = discover by name
CHAR = "00001624-1212-efde-1623-785feabcd123"
LOG_PATH = "probe_m0_log_v5.json"

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

async def find_hub(timeout_s=240):
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
        print("  ...rescanning (press green button if hub is off)", flush=True)
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

        # verify encryption works now (bond): LED -> green 0x06
        r = await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x3F, 0x11, 0x51, 0x00, 0x06]),
            "led_green_bondtest")
        if r != "ok":
            note("bond_fail", r)

        # ---- A) port 0x35 light bitmap formats ----
        # A1: startup 0x00 (like motors), mode 0, all lights on (63)
        await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x35, 0x00, 0x51, 0x00, 0x3F]),
            "p35_fmtA_allon")
        # A2: startup 0x11 (like LED), mode 0, all on
        await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x35, 0x11, 0x51, 0x00, 0x3F]),
            "p35_fmtB_allon")
        # A3: 2-byte payload (bitmap + ?)
        await try_write(client,
            bytes([0x09, 0x00, 0x81, 0x35, 0x00, 0x51, 0x00, 0x3F, 0x00]),
            "p35_fmtC_allon")
        # A4: mode 1
        await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x35, 0x00, 0x51, 0x01, 0x3F]),
            "p35_fmtD_mode1")
        # A5: subcommand 0x50 (WriteDirect) instead of 0x51
        await try_write(client,
            bytes([0x07, 0x00, 0x81, 0x35, 0x00, 0x50, 0x3F]),
            "p35_fmtE_direct")

        # ---- B) enable value notifications on ports 0x32/0x33/0x34 ----
        # PortInputFormatSetup: [len, hub, 0x41, port, mode, delta u32, notif 0x01]
        for port, mode in ((0x32, 0x00), (0x33, 0x00), (0x34, 0x02)):
            await try_write(client,
                bytes([0x0A, 0x00, 0x41, port, mode, 0x00, 0x00, 0x00, 0x00, 0x01]),
                f"inputfmt_{port:02x}_m{mode}")

        # ---- C) spin motor A slowly and watch notifications ----
        await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x32, 0x00, 0x51, 0x00, 0x14]),
            "motorA_power20", wait=0.1)
        await asyncio.sleep(2.0)  # collect notifications
        await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x32, 0x00, 0x51, 0x00, 0x7F]),
            "motorA_brake")

        # battery / system: try HubAlert? skip. read port values once more
        await asyncio.sleep(1.0)

        # leave lights off attempt (whichever format worked shows in log)
        await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x35, 0x00, 0x51, 0x00, 0x00]),
            "p35_alloff")

        await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x32, 0x00, 0x51, 0x00, 0x7F]), "final_A")
        await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x33, 0x00, 0x51, 0x00, 0x7F]), "final_B")
        await try_write(client,
            bytes([0x08, 0x00, 0x81, 0x34, 0x00, 0x51, 0x00, 0x7F]), "final_C")

        await asyncio.wait_for(client.disconnect(), timeout=10)
        note("done", "clean exit")
    except Exception as e:
        note("fatal", repr(e))
        traceback.print_exc()
    savelog()
    print("log entries:", len(log))

asyncio.run(main())
