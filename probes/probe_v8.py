#!/usr/bin/env python3
"""M0b probe v8 — Technic Move Hub: per-light hunt + sensors + motor ID.
Phases (in order):
  A) safe reads: PortValueRequest all ports; ModeInfo t03/t04/t06/tF0 for
     unknown ports (mapping tells us if/how a mode is writable)
  D) motor identification spins (A then B — watch which wheels move)
  B) port 0x35 write-format variants (lights OFF before each, watch after)
  B2) port 0x37 single attempts (if B all rejected)
  E) demo: lights ON, steering sweep, brake
  C) VM tail-byte probe (0x01..0x08) with survival check
Every write is survival-checked: no feedback in 4 s => hub crashed => abort.
Bond exists; press the green button to wake the hub."""
import asyncio, datetime, json, traceback, os
from bleak import BleakScanner, BleakClient

ADDR = os.environ.get("MOVEHUB_ADDR", "")  # your hub MAC; empty = discover by name
CHAR = "00001624-1212-efde-1623-785feabcd123"
LOG_PATH = "probe_m0_log_v8.json"

log = []
notify_event = asyncio.Event()

class HubDead(Exception):
    pass

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

async def cmd(client, data, tag, timeout=4.0, settle=0.4, expect_feedback=True):
    """Write + wait for feedback. Raises HubDead if hub goes silent."""
    note("write:" + tag, bytes(data))
    notify_event.clear()
    try:
        await asyncio.wait_for(
            client.write_gatt_char(CHAR, bytes(data), response=False), timeout=8)
    except Exception as e:
        note("write_error:" + tag, repr(e))
        raise HubDead(tag)
    if expect_feedback:
        try:
            await asyncio.wait_for(notify_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            note("hub_silent_after", tag)
            raise HubDead(tag)
    await asyncio.sleep(settle)

def vm_drive(speed=0, steer=0, flags=0x00, tail=0x00):
    return bytes([0x0D, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x03, 0x00,
                  speed & 0xFF, steer & 0xFF, flags & 0xFF, tail & 0xFF])

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
    ("f32_63",  bytes([0x0B, 0x00, 0x81, 0x35, 0x11, 0x51, 0x00, 0x00, 0x00, 0x7C, 0x42])),  # float32 63.0
    ("f32_1",   bytes([0x0B, 0x00, 0x81, 0x35, 0x11, 0x51, 0x00, 0x00, 0x00, 0x80, 0x3F])),  # float32 1.0
    ("i16_63",  bytes([0x0A, 0x00, 0x81, 0x35, 0x11, 0x51, 0x00, 0x3F, 0x00])),
    ("i32_63",  bytes([0x0C, 0x00, 0x81, 0x35, 0x11, 0x51, 0x00, 0x3F, 0x00, 0x00, 0x00])),
    ("i8_01",   bytes([0x08, 0x00, 0x81, 0x35, 0x11, 0x51, 0x00, 0x01])),
    ("su_01",   bytes([0x08, 0x00, 0x81, 0x35, 0x01, 0x51, 0x00, 0x3F])),
    ("su_10",   bytes([0x08, 0x00, 0x81, 0x35, 0x10, 0x51, 0x00, 0x3F])),
    ("wd_50",   bytes([0x07, 0x00, 0x81, 0x35, 0x11, 0x50, 0x3F])),
    ("six_01",  bytes([0x0D, 0x00, 0x81, 0x35, 0x11, 0x51, 0x00, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01])),
]

VARIANTS_37 = [
    ("f32_63",  bytes([0x0B, 0x00, 0x81, 0x37, 0x11, 0x51, 0x00, 0x00, 0x00, 0x7C, 0x42])),
    ("i8_01",   bytes([0x08, 0x00, 0x81, 0x37, 0x11, 0x51, 0x00, 0x01])),
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

        # sanity: LED green (bond check)
        await cmd(client, bytes([0x08, 0x00, 0x81, 0x3F, 0x11, 0x51, 0x00, 0x06]),
                  "led_green_sanity")

        # ---------- PHASE A: reads ----------
        note("phase", "A: value requests + mapping info")
        for port in range(0x32, 0x41):
            await cmd(client, bytes([0x04, 0x00, 0x42, port]),
                      f"valuereq_{port:02x}", timeout=3.0, settle=0.35)
        # mapping (t06), pct (t03), SI (t04), capabilities (tF0) for unknowns
        for port in (0x35, 0x37, 0x38, 0x39, 0x3A, 0x3B, 0x3D, 0x3E, 0x40, 0x3F):
            for mode in range(0, 4):
                for it in (0x03, 0x04, 0x06, 0xF0):
                    try:
                        await cmd(client, bytes([0x06, 0x00, 0x22, port, mode, it]),
                                  f"m_{port:02x}_{mode}_t{it:02x}", timeout=3.0, settle=0.22)
                    except HubDead:
                        raise
        # names for 0x35/0x37 with generous settle
        for port in (0x35, 0x37, 0x3D):
            await cmd(client, bytes([0x06, 0x00, 0x22, port, 0x00, 0x01]),
                      f"name_{port:02x}", timeout=3.0, settle=0.8)

        # ---------- PHASE D: motor identification ----------
        note("phase", "D: motor A then B spin (watch wheels)")
        await cmd(client, bytes([0x08, 0x00, 0x81, 0x32, 0x00, 0x51, 0x00, 0x1E]),
                  "spin_A_30", settle=0.7)
        await cmd(client, bytes([0x08, 0x00, 0x81, 0x32, 0x00, 0x51, 0x00, 0x7F]),
                  "brake_A", settle=2.0)
        await cmd(client, bytes([0x08, 0x00, 0x81, 0x33, 0x00, 0x51, 0x00, 0x1E]),
                  "spin_B_30", settle=0.7)
        await cmd(client, bytes([0x08, 0x00, 0x81, 0x33, 0x00, 0x51, 0x00, 0x7F]),
                  "brake_B", settle=2.0)

        # ---------- PHASE B: 0x35 write variants ----------
        note("phase", "B: port 0x35 format hunt (watch the LIGHTS)")
        for name, frame in VARIANTS_35:
            await cmd(client, vm_drive(0, 0, 0x04), f"pre_off_{name}", settle=0.5)
            try:
                await cmd(client, frame, f"p35_{name}", timeout=4.0, settle=1.5)
                note("variant_result", f"{name}: got feedback (see log above)")
            except HubDead:
                note("hub_died_at", f"p35_{name}")
                raise
            await cmd(client, vm_drive(0, 0, 0x04), f"post_off_{name}", settle=0.5)

        # ---------- PHASE B2: 0x37 attempts ----------
        note("phase", "B2: port 0x37 attempts")
        for name, frame in VARIANTS_37:
            try:
                await cmd(client, frame, f"p37_{name}", timeout=4.0, settle=1.2)
                note("variant_result", f"p37 {name}: got feedback")
            except HubDead:
                note("hub_died_at", f"p37_{name}")
                raise

        # ---------- PHASE E: demo ----------
        note("phase", "E: demo lights + steering sweep")
        await cmd(client, vm_drive(0, 0, 0x00), "demo_lights_on", settle=0.8)
        await cmd(client, vm_drive(0, 50, 0x00), "demo_steer_right", settle=1.0)
        await cmd(client, vm_drive(0, -50, 0x00), "demo_steer_left", settle=1.0)
        await cmd(client, vm_drive(0, 0, 0x00), "demo_steer_center", settle=0.8)
        await cmd(client, vm_drive(0, 0, 0x04), "demo_lights_off", settle=0.6)

        # ---------- PHASE C: VM tail byte probe ----------
        note("phase", "C: VM tail-byte probe")
        for tail in (0x01, 0x02, 0x04, 0x08):
            try:
                await cmd(client, vm_drive(0, 0, 0x00, tail),
                          f"tail_{tail:02x}", timeout=4.0, settle=1.0)
            except HubDead:
                note("hub_died_at", f"tail_{tail:02x}")
                raise

        # ---------- final ----------
        for port in (0x32, 0x33, 0x34):
            await cmd(client, bytes([0x08, 0x00, 0x81, port, 0x00, 0x51, 0x00, 0x7F]),
                      f"final_brake_{port:02x}", settle=0.3)
        await cmd(client, bytes([0x08, 0x00, 0x81, 0x3F, 0x11, 0x51, 0x00, 0x06]),
                  "final_led_green")
        await asyncio.wait_for(client.disconnect(), timeout=10)
        note("done", "clean exit")
    except HubDead as hd:
        note("hub_dead", str(hd))
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5)
        except Exception:
            pass
    except Exception as e:
        note("fatal", repr(e))
        traceback.print_exc()
    savelog()
    print("log entries:", len(log))

asyncio.run(main())
