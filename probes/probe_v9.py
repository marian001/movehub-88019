#!/usr/bin/env python3
"""M0b probe v9 — per-light bitmap + sensors, fixed feedback logic.
Order: motor ID (A,B spins) -> sensor streaming (steer/voltage/accel) ->
0x35 format hunt (9 variants, liveness-checked via LED) -> if a variant
is accepted: individual-bit test (1,2,4,8,16,32) so you can name which
light each bit drives.
Writes with startup 0x00 (direct motor) produce NO feedback by spec —
only 0x11 frames do. Liveness check = LED write expecting 0x82 reply."""
import asyncio, datetime, json, struct, traceback, os
from bleak import BleakScanner, BleakClient

ADDR = os.environ.get("MOVEHUB_ADDR", "")  # your hub MAC; empty = discover by name
CHAR = "00001624-1212-efde-1623-785feabcd123"
LOG_PATH = "probe_m0_log_v9.json"

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

async def wait_feedback(timeout=4.0):
    notify_event.clear()
    try:
        await asyncio.wait_for(notify_event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False

async def w(client, data, tag, wait=0.3, feedback=False):
    note("write:" + tag, bytes(data))
    try:
        await asyncio.wait_for(
            client.write_gatt_char(CHAR, bytes(data), response=False), timeout=8)
    except Exception as e:
        note("write_error:" + tag, repr(e))
        return False
    ok = True
    if feedback:
        ok = await wait_feedback()
        if not ok:
            note("no_feedback", tag)
    await asyncio.sleep(wait)
    return ok

def vm_drive(speed=0, steer=0, flags=0x00):
    return bytes([0x0D, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x03, 0x00,
                  speed & 0xFF, steer & 0xFF, flags & 0xFF, 0x00])

LED = lambda color: bytes([0x08, 0x00, 0x81, 0x3F, 0x11, 0x51, 0x00, color])
MOTOR = lambda port, p: bytes([0x08, 0x00, 0x81, port, 0x00, 0x51, 0x00, p & 0xFF])
BRAKE = lambda port: MOTOR(port, 0x7F)

async def alive(client):
    return await w(client, LED(0x06), "liveness_led", wait=0.2, feedback=True)

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

VARIANTS_35 = [
    ("f32_63",  bytes([0x0B, 0x00, 0x81, 0x35, 0x11, 0x51, 0x00, 0x00, 0x00, 0x7C, 0x42])),
    ("f32_1",   bytes([0x0B, 0x00, 0x81, 0x35, 0x11, 0x51, 0x00, 0x00, 0x00, 0x80, 0x3F])),
    ("i16_63",  bytes([0x0A, 0x00, 0x81, 0x35, 0x11, 0x51, 0x00, 0x3F, 0x00])),
    ("i32_63",  bytes([0x0C, 0x00, 0x81, 0x35, 0x11, 0x51, 0x00, 0x3F, 0x00, 0x00, 0x00])),
    ("i8_01",   bytes([0x08, 0x00, 0x81, 0x35, 0x11, 0x51, 0x00, 0x01])),
    ("su_01",   bytes([0x08, 0x00, 0x81, 0x35, 0x01, 0x51, 0x00, 0x3F])),
    ("su_10",   bytes([0x08, 0x00, 0x81, 0x35, 0x10, 0x51, 0x00, 0x3F])),
    ("wd_50",   bytes([0x07, 0x00, 0x81, 0x35, 0x11, 0x50, 0x3F])),
    ("six_01",  bytes([0x0D, 0x00, 0x81, 0x35, 0x11, 0x51, 0x00, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01])),
]

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

        if not (await alive(client)):
            note("fatal", "liveness failed right after connect")
            return

        # ---------- PHASE 1: motor ID (NO feedback expected on 0x00 startup) ----------
        note("phase", "1: motor A spin 1.2s -> brake; then B")
        await w(client, MOTOR(0x32, 0x1E), "spin_A_30", wait=1.2)
        await w(client, BRAKE(0x32), "brake_A", wait=2.0)
        if not (await alive(client)):
            note("hub_dead_after", "motor A phase"); return
        await w(client, MOTOR(0x33, 0x1E), "spin_B_30", wait=1.2)
        await w(client, BRAKE(0x33), "brake_B", wait=2.0)
        if not (await alive(client)):
            note("hub_dead_after", "motor B phase"); return

        # ---------- PHASE 2: sensor streaming ----------
        note("phase", "2: sensor reads (steer angle, voltage, accel) 3s")
        for port, mode, delta in ((0x37, 0x00, 2), (0x3C, 0x00, 100), (0x38, 0x00, 200)):
            await w(client, bytes([0x0A, 0x00, 0x41, port, mode,
                                   delta, 0x00, 0x00, 0x00, 0x01]),
                    f"inputfmt_{port:02x}", wait=0.3, feedback=True)
        await asyncio.sleep(3.0)   # values stream into log as 0x45
        # disable notifications again
        for port, mode in ((0x37, 0x00), (0x3C, 0x00), (0x38, 0x00)):
            await w(client, bytes([0x0A, 0x00, 0x41, port, mode,
                                   0x00, 0x00, 0x00, 0x00, 0x00]),
                    f"inputoff_{port:02x}", wait=0.3, feedback=True)
        if not (await alive(client)):
            note("hub_dead_after", "sensor phase"); return

        # ---------- PHASE 3: 0x35 format hunt ----------
        note("phase", "3: port 0x35 format hunt — WATCH THE LIGHTS")
        accepted = None
        for name, frame in VARIANTS_35:
            await w(client, vm_drive(0, 0, 0x04), f"pre_off_{name}", wait=0.5, feedback=True)
            note("variant", name)
            await w(client, frame, f"p35_{name}", wait=1.5, feedback=True)
            # error replies (05 00 05 81 06) also count as feedback — parse later
            if not (await alive(client)):
                note("hub_dead_at", f"p35_{name}"); return
        # note: which variant actually took effect must be read from the log;
        # error ACKs (0x06) vs output feedback (0x82 35 ..) differ.

        # ---------- PHASE 4: individual bits (only if some variant was accepted) ----------
        # Parse log so far: find p35_* whose response was NOT an error
        err_tags = set()
        ok_tags = set()
        pend = None
        for e in log:
            if e["tag"].startswith("write:p35_"):
                pend = e["tag"][9:]
            elif e["tag"] == "notify" and pend:
                h = e.get("hex", "")
                if h.startswith("050005"):
                    err_tags.add(pend)
                elif h.startswith("05008235") or h.startswith("05004535"):
                    ok_tags.add(pend)
                pend = None
        good = sorted(ok_tags - err_tags)
        note("accepted_variants", str(good))
        if good:
            base = good[0]
            frame = dict(VARIANTS_35)[base]
            note("phase", f"4: individual bits using {base}")
            for bit, val in (("bit0_l1", 0x01), ("bit1_l2", 0x02), ("bit2_l3", 0x04),
                             ("bit3_l4", 0x08), ("bit4_l5", 0x10), ("bit5_l6", 0x20)):
                # replace last payload byte(s) with the bit value — assume last byte is the value
                mod = bytearray(frame); mod[-1] = val
                await w(client, vm_drive(0, 0, 0x04), f"bit_pre_off_{bit}", wait=0.4, feedback=True)
                await w(client, bytes(mod), f"p35_{bit}", wait=1.6, feedback=True)
                if not (await alive(client)):
                    note("hub_dead_at", bit); return
            await w(client, vm_drive(0, 0, 0x04), "bits_final_off", wait=0.5, feedback=True)

        # ---------- final ----------
        for port in (0x32, 0x33, 0x34):
            await w(client, BRAKE(port), f"final_brake_{port:02x}", wait=0.3)
        await w(client, LED(0x06), "final_led_green", wait=0.3, feedback=True)
        await asyncio.wait_for(client.disconnect(), timeout=10)
        note("done", "clean exit")
    except Exception as e:
        note("fatal", repr(e))
        traceback.print_exc()
    savelog()
    print("log entries:", len(log))

asyncio.run(main())
