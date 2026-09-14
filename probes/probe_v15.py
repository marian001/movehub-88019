#!/usr/bin/env python3
"""M0b probe v15 — two-phase: (A) test if v13's clean session-end fixed the
VM; (B) if not, remove bond + re-pair + retest. Then full 20 Hz stream test.
The user only presses the green button when the script asks (watch stdout)."""
import asyncio, datetime, json, subprocess, traceback, os
from bleak import BleakScanner, BleakClient

ADDR = os.environ.get("MOVEHUB_ADDR", "")  # your hub MAC; empty = discover by name
CHAR = "00001624-1212-efde-1623-785feabcd123"
LOG_PATH = "probe_m0_log_v15.json"

VM_ARM   = bytes([0x09, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x04, 0x01])
VM_STOP  = bytes([0x0A, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x00, 0x00, 0x00])
VM_START = bytes([0x08, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x01])
VM_SUBSCRIBE = bytes([0x0A, 0x00, 0x41, 0x36, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01])
VM_STATE_REQ = bytes([0x05, 0x00, 0x21, 0x36, 0x00])
HUB_ACTION_DISCONNECT = bytes([0x04, 0x00, 0x02, 0x02])

VM_STATES = {(0x01,0x00):"RUNNING", (0x02,0x00):"STOPPING",
             (0x00,0x01):"STOPPED", (0x03,0x01):"EXECUTING"}

log = []
notify_event = asyncio.Event()
last_notify = {"b": None}
vm_state = {"name": "UNKNOWN", "raw": None}
stats = {"ack": 0, "err": 0}

def ts():
    return datetime.datetime.now().isoformat(timespec="milliseconds")

def note(tag, data, quiet=False):
    entry = {"t": ts(), "tag": tag}
    if isinstance(data, (bytes, bytearray)):
        entry["hex"] = bytes(data).hex()
    else:
        entry["msg"] = str(data)
    log.append(entry)
    if not quiet:
        print(entry["t"], tag, entry.get("hex", entry.get("msg")), flush=True)

def savelog():
    with open(LOG_PATH, "w") as f:
        json.dump(log, f, indent=1)

def on_notify(_c, data):
    b = bytes(data)
    last_notify["b"] = b
    if len(b) >= 6 and b[2] == 0x45 and b[3] == 0x36:
        raw = (b[4], b[5])
        vm_state["raw"] = raw
        vm_state["name"] = VM_STATES.get(raw, f"UNKNOWN{raw}")
        note("vmstate", b)
    elif len(b) == 5 and b[2] == 0x05:
        stats["err" if b[4] >= 0x03 else "ack"] += 1
        note("err_reply" if b[4] >= 0x03 else "ack_reply", b, quiet=True)
    else:
        note("notify", b, quiet=True)
    notify_event.set()

async def w(client, data, tag, wait=0.3, feedback=True, timeout=4.0):
    note("write:" + tag, bytes(data))
    notify_event.clear()
    last_notify["b"] = None
    try:
        await asyncio.wait_for(
            client.write_gatt_char(CHAR, bytes(data), response=False), timeout=8)
    except Exception as e:
        note("write_error:" + tag, repr(e))
        return None
    if feedback:
        try:
            await asyncio.wait_for(notify_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            note("no_feedback", tag)
    await asyncio.sleep(wait)
    return last_notify["b"]

def reply_kind(nb):
    """Classify the notify that answered a write: 'ack' | 'err' | other."""
    if nb is None: return "none"
    if len(nb) == 5 and nb[2] == 0x05:
        return "err" if nb[4] >= 0x03 else "ack"
    if len(nb) >= 4 and nb[2] == 0x82:
        return "ack"
    return "other"

def vm(speed, steer, flags, tail=0x00):
    return bytes([0x0D, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x03, 0x00,
                  speed & 0xFF, steer & 0xFF, flags & 0xFF, tail & 0xFF])

LED = lambda color: bytes([0x08, 0x00, 0x81, 0x3F, 0x11, 0x51, 0x00, color])
MOTOR = lambda port, p: bytes([0x08, 0x00, 0x81, port, 0x00, 0x51, 0x00, p & 0xFF])

async def find_hub(timeout_s=600, hint="press the green button"):
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
        print(f"  ...rescanning ({hint})", flush=True)
    return None

async def connect_session(pair=False):
    dev = await find_hub()
    if not dev:
        return None
    client = BleakClient(dev, timeout=25,
                         disconnected_callback=lambda c: note("disconnected", "cb"))
    await asyncio.wait_for(client.connect(), timeout=30)
    note("connected", client.is_connected)
    if pair:
        try:
            await asyncio.wait_for(client.pair(), timeout=45)
            note("paired", "ok")
        except Exception as e:
            note("pair_error", repr(e))
    await asyncio.wait_for(client.start_notify(CHAR, on_notify), timeout=15)
    note("notify_started", True)
    await asyncio.sleep(1.2)
    return client

async def vm_handshake(client):
    await w(client, VM_SUBSCRIBE, "vm_subscribe", wait=0.5)
    await w(client, VM_STATE_REQ, "vm_state_req", wait=1.0)
    note("vm_state_initial", vm_state["name"] + " " + str(vm_state["raw"]))
    if vm_state["name"] == "STOPPED":
        await w(client, VM_STOP, "vm_stop", wait=0.6)
        await w(client, VM_START, "vm_start", wait=1.2)
    await w(client, VM_ARM, "vm_arm", wait=0.6)
    await w(client, VM_STATE_REQ, "vm_state_req2", wait=0.8)
    note("vm_state_after", vm_state["name"] + " " + str(vm_state["raw"]))

async def drive_probe(client):
    """One lights-on drive frame. Returns 'ack' or 'err'."""
    nb = await w(client, vm(0, 0, 0x00), "drive_probe_lights_on", wait=1.2)
    kind = reply_kind(nb)
    note("drive_probe_result", kind)
    return kind

async def full_test(client):
    """The real deal: calibration + 20 Hz stream + motors + demo."""
    note("phase", "FULL TEST — watch the car")
    # calibration (single frames, generous waits)
    await w(client, vm(0, 0, 0x10), "calib1", wait=1.6)
    await w(client, vm(0, 0, 0x08), "calib2", wait=2.6)

    async def stream(label, dur, speed=0, steer=0, flags=0x00):
        note("phase", f"stream {label}: {dur}s speed={speed} steer={steer} flags={flags:#04x}")
        end = asyncio.get_event_loop().time() + dur
        while asyncio.get_event_loop().time() < end:
            try:
                await client.write_gatt_char(CHAR, vm(speed, steer, flags), response=False)
            except Exception as e:
                note("stream_write_error", repr(e))
                return
            await asyncio.sleep(0.05)

    await stream("lights_on", 2.0, flags=0x00)
    await stream("blink_off", 0.5, flags=0x04)
    await stream("blink_on", 0.5, flags=0x00)
    await stream("blink_off2", 0.5, flags=0x04)
    await stream("blink_on2", 0.5, flags=0x00)
    await stream("steer_right40", 1.2, steer=40)
    await stream("steer_left40", 1.2, steer=-40)
    await stream("steer_center", 0.8, steer=0)
    await stream("drive_fwd_30", 2.0, speed=30)
    await stream("stop", 1.0, speed=0)
    await stream("lights_off", 1.0, flags=0x04)

    # direct motors
    await w(client, MOTOR(0x32, 0x1E), "motorA_spin", wait=1.0, feedback=False)
    await w(client, MOTOR(0x32, 0x7F), "motorA_brake", wait=0.8, feedback=False)
    await w(client, MOTOR(0x33, 0x1E), "motorB_spin", wait=1.0, feedback=False)
    await w(client, MOTOR(0x33, 0x7F), "motorB_brake", wait=0.8, feedback=False)

    note("stats", f"acks={stats['ack']} errors={stats['err']}")
    # clean end
    await w(client, vm(0, 0, 0x04), "final_off", wait=0.5)
    await w(client, HUB_ACTION_DISCONNECT, "hub_action_disconnect", wait=1.0)
    try:
        await asyncio.wait_for(client.disconnect(), timeout=10)
    except Exception:
        pass
    note("done", "clean exit, session ended")

async def end_session(client):
    try:
        await client.write_gatt_char(CHAR, HUB_ACTION_DISCONNECT, response=False)
        note("cleanup", "hub_action_disconnect sent")
        await asyncio.sleep(0.5)
        await asyncio.wait_for(client.disconnect(), timeout=8)
    except Exception:
        pass

async def main():
    # ---------- PHASE A: existing bond ----------
    note("phase", "A: connect with existing bond (turn the hub ON now)")
    client = await connect_session(pair=False)
    if client is None:
        note("abort", "hub not found in phase A")
        savelog(); return
    try:
        nb = await w(client, LED(0x06), "led_sanity_A", wait=0.4)
        if reply_kind(nb) in ("ack", "other"):
            await vm_handshake(client)
            result = await drive_probe(client)
            if result == "ack":
                note("phase", "A: VM HEALTHY — v13 session-end fixed it")
                await full_test(client)
                savelog(); return
            note("phase", f"A: VM still refuses drive frames ({result}) -> re-pair")
        else:
            note("phase", "A: link not encrypted (no LED ack) -> re-pair")
        await end_session(client)
    except Exception as e:
        note("phaseA_error", repr(e))
        await end_session(client)

    # ---------- PHASE B: remove bond, re-pair ----------
    note("phase", "B: removing bond and re-pairing")
    if ADDR:
        r = subprocess.run(["bluetoothctl", "remove", ADDR], capture_output=True, text=True)
        note("bond_removed", (r.stdout + r.stderr).strip()[:80])
    else:
        note("bond_removed", "skipped: MOVEHUB_ADDR not set")
    await asyncio.sleep(2)

    client = await connect_session(pair=True)
    if client is None:
        note("abort", "hub not found in phase B")
        savelog(); return
    try:
        nb = await w(client, LED(0x06), "led_sanity_B", wait=0.4)
        if reply_kind(nb) not in ("ack", "other"):
            note("fatal", "phase B: no link even after re-pair")
            await end_session(client)
            savelog(); return
        await vm_handshake(client)
        result = await drive_probe(client)
        if result == "ack":
            note("phase", "B: VM HEALTHY after re-pair")
            await full_test(client)
        else:
            note("phase", f"B: VM STILL refuses drive frames ({result}) — reference driver next")
            await end_session(client)
    except Exception as e:
        note("phaseB_error", repr(e))
        await end_session(client)
    savelog()
    print("log entries:", len(log))

asyncio.run(main())
