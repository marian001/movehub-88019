#!/usr/bin/env python3
"""M0 probe v4 — Technic Move Hub 88019.
Order per Benedettelli ESP32 flow: CONNECT FIRST, then PAIR (bond, LE secure),
then notify + write. Retries, timeouts on every step. Press the green button."""
import asyncio, datetime, json, traceback, os
from bleak import BleakScanner, BleakClient

ADDR = os.environ.get("MOVEHUB_ADDR", "")  # your hub MAC; empty = discover by name
CHAR = "00001624-1212-efde-1623-785feabcd123"
LOG_PATH = "probe_m0_log.json"

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

async def step(coro, tag, timeout):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except Exception as e:
        note("step_error:" + tag, repr(e))
        return None

async def find_hub(timeout_s=600):
    print(f"scanning up to {timeout_s}s — press the green button", flush=True)
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

async def connect_and_pair():
    """Connect first, pair while connected (Benedettelli order)."""
    dev = await find_hub()
    if not dev:
        return None
    client = BleakClient(dev, timeout=25,
                         disconnected_callback=lambda c: note("disconnected", "cb"))
    await step(client.connect(), "connect", 30)
    if not client.is_connected:
        note("fail", "not connected")
        return None
    note("connected", client.is_connected)
    r = await step(client.pair(), "pair", 40)
    note("pair_result", r if r is not None else "failed/timeout")
    return client

async def try_write(client, data, tag, wait=0.25):
    note("write:" + tag, bytes(data))
    try:
        await asyncio.wait_for(
            client.write_gatt_char(CHAR, bytes(data), response=False), timeout=8)
        await asyncio.sleep(wait)
        return "ok"
    except Exception as e:
        note("write_error:" + tag, repr(e))
        return repr(e)

async def run_probe(client):
    def handle(_c, data):
        note("notify", bytes(data))

    r = await step(client.start_notify(CHAR, handle), "start_notify", 15)
    note("notify_started", r is not None)
    await asyncio.sleep(1.5)

    async def w(data, tag, wait=0.25):
        await try_write(client, data, tag, wait)

    # --- 1) Port info ---
    for port in (0x32, 0x33, 0x34, 0x35, 0x36, 0x3F):
        for itype in (0x01, 0x02):
            await w(bytes([0x05, 0x00, 0x21, port, itype]),
                    f"portinfo_{port:02x}_t{itype:02x}")

    # --- 2) Mode info ---
    for port in (0x32, 0x33, 0x34, 0x35, 0x36):
        for mode in (0, 1, 2):
            for it in (0x01, 0x02, 0x05):
                await w(bytes([0x06, 0x00, 0x22, port, mode, it]),
                        f"modeinfo_{port:02x}_m{mode}_t{it:02x}")

    # --- 3) Status LED ---
    for color in (0x05, 0x03, 0x09):
        await w(bytes([0x08, 0x00, 0x81, 0x3F, 0x11, 0x51, 0x00, color]),
                f"led_color_{color:02x}")

    # --- 4) Motors A/B/C ---
    for name, port in (("A", 0x32), ("B", 0x33), ("C", 0x34)):
        await w(bytes([0x08, 0x00, 0x81, port, 0x00, 0x51, 0x00, 0x1E]),
                f"motor_{name}_power30")
        await asyncio.sleep(0.35)
        await w(bytes([0x08, 0x00, 0x81, port, 0x00, 0x51, 0x00, 0x7F]),
                f"motor_{name}_brake")
        await asyncio.sleep(0.5)

    # --- 5) Port 0x35 light probe ---
    for val in (0x03, 0x06, 0x00):
        await w(bytes([0x08, 0x00, 0x81, 0x35, 0x11, 0x51, 0x00, val]),
                f"p35_mode0_{val:02x}")

    # --- 6) Lights via VM ---
    await w(bytes([0x0D, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x03, 0x00,
                   0x00, 0x00, 0x00, 0x00]), "vm_lights_on")
    await asyncio.sleep(0.6)
    await w(bytes([0x0D, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x03, 0x00,
                   0x00, 0x00, 0x04, 0x00]), "vm_lights_off")
    await asyncio.sleep(0.6)

    for port in (0x32, 0x33, 0x34):
        await w(bytes([0x08, 0x00, 0x81, port, 0x00, 0x51, 0x00, 0x7F]),
                f"final_brake_{port:02x}")

async def main():
    for attempt in range(3):
        client = None
        try:
            client = await connect_and_pair()
            if client:
                await run_probe(client)
                await step(client.disconnect(), "disconnect", 10)
                note("done", "clean exit")
                break
        except Exception as e:
            note("attempt_error", repr(e))
            traceback.print_exc()
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        savelog()
        await asyncio.sleep(3)
    savelog()
    print("log entries:", len(log))

asyncio.run(main())
