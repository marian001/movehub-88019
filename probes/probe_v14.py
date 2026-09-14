#!/usr/bin/env python3
"""M0b probe v14 — REAL driver pattern: continuous 20 Hz drive stream.
Hypothesis: the drive VM only accepts drive frames as a continuous stream
(reference driver writes at 20 Hz nonstop); one-shot frames get ERR 0x05.
Plan: connect -> LED sanity -> VM subscribe/state/arm -> calibration ->
20 Hz writer for 12 s doing: lights on (2s) -> blink (2s) -> steer sweep ->
lights off -> session end. Motor A/B also direct-tested mid-stream."""
import asyncio, datetime, json, traceback, os
from bleak import BleakScanner, BleakClient

ADDR = os.environ.get("MOVEHUB_ADDR", "")  # your hub MAC; empty = discover by name
CHAR = "00001624-1212-efde-1623-785feabcd123"
LOG_PATH = "probe_m0_log_v14.json"

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
vm_state = {"name": "UNKNOWN", "raw": None}
ack_count = {"drive": 0, "err": 0}

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
    if len(b) >= 6 and b[2] == 0x45 and b[3] == 0x36:
        raw = (b[4], b[5])
        vm_state["raw"] = raw
        vm_state["name"] = VM_STATES.get(raw, f"UNKNOWN{raw}")
        note("notify_vmstate", b, quiet=True)
    elif len(b) == 5 and b[2] == 0x05:
        # generic error/ack — count but don't spam
        if b[4] == 0x01 or b[4] == 0x02:
            ack_count["drive"] += 1
        else:
            ack_count["err"] += 1
            note("notify_err", b, quiet=True)
    else:
        note("notify", b, quiet=True)
    notify_event.set()

async def w(client, data, tag, wait=0.3, feedback=True, timeout=4.0):
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
            await asyncio.wait_for(notify_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            note("no_feedback", tag)
            ok = False
    await asyncio.sleep(wait)
    return ok

def vm(speed, steer, flags, tail=0x00, mode=0x03):
    return bytes([0x0D, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, mode,
                  speed & 0xFF, steer & 0xFF, flags & 0xFF, tail & 0xFF])

LED = lambda color: bytes([0x08, 0x00, 0x81, 0x3F, 0x11, 0x51, 0x00, color])
MOTOR = lambda port, p: bytes([0x08, 0x00, 0x81, port, 0x00, 0x51, 0x00, p & 0xFF])

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

async def stream_phase(client, label, duration_s, speed=0, steer=0, flags=0x00):
    """Write drive frames at 20 Hz for duration_s."""
    note("phase", f"{label}: 20 Hz stream {duration_s}s (speed={speed} steer={steer} flags={flags:#04x})")
    interval = 0.05
    end = asyncio.get_event_loop().time() + duration_s
    while asyncio.get_event_loop().time() < end:
        try:
            await client.write_gatt_char(CHAR, vm(speed, steer, flags), response=False)
        except Exception as e:
            note("stream_write_error", repr(e))
            return False
        await asyncio.sleep(interval)
    return True

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
        await asyncio.sleep(1.5)

        await w(client, LED(0x06), "led_green_sanity", wait=0.4)

        # VM handshake
        note("phase", "VM handshake")
        await w(client, VM_SUBSCRIBE, "vm_subscribe", wait=0.5)
        await w(client, VM_STATE_REQ, "vm_state_req", wait=1.0)
        note("vm_state_initial", vm_state["name"] + " " + str(vm_state["raw"]))
        if vm_state["name"] == "STOPPED":
            await w(client, VM_STOP, "vm_stop", wait=0.6)
            await w(client, VM_START, "vm_start", wait=1.2)
        await w(client, VM_ARM, "vm_arm", wait=0.6)
        await w(client, VM_STATE_REQ, "vm_state_req2", wait=0.8)
        note("vm_state_after", vm_state["name"] + " " + str(vm_state["raw"]))

        # calibration via stream (each frame once, with waits, per reference)
        note("phase", "calibration (steering sweeps)")
        await w(client, vm(0, 0, 0x10), "calib1_once", wait=1.6)
        await w(client, vm(0, 0, 0x08), "calib2_once", wait=2.6)

        # THE TEST: continuous stream
        await stream_phase(client, "lights_on", 2.0, flags=0x00)
        await stream_phase(client, "blink_off", 0.5, flags=0x04)
        await stream_phase(client, "blink_on", 0.5, flags=0x00)
        await stream_phase(client, "blink_off2", 0.5, flags=0x04)
        await stream_phase(client, "blink_on2", 0.5, flags=0x00)
        await stream_phase(client, "steer_right", 1.2, steer=40)
        await stream_phase(client, "steer_left", 1.2, steer=-40)
        await stream_phase(client, "steer_center", 0.8, steer=0)
        await stream_phase(client, "drive_forward_30", 2.0, speed=30)
        await stream_phase(client, "stop", 1.0, speed=0)
        await stream_phase(client, "lights_off", 1.0, flags=0x04)

        note("stats", f"acks={ack_count['drive']} errors={ack_count['err']}")

        # direct motors mid-session (still fine?)
        await w(client, MOTOR(0x32, 0x1E), "motorA_spin", wait=1.0, feedback=False)
        await w(client, MOTOR(0x32, 0x7F), "motorA_brake", wait=0.8, feedback=False)
        await w(client, MOTOR(0x33, 0x1E), "motorB_spin", wait=1.0, feedback=False)
        await w(client, MOTOR(0x33, 0x7F), "motorB_brake", wait=0.8, feedback=False)

        # final + session end
        await w(client, vm(0, 0, 0x04), "final_off", wait=0.5)
        await w(client, HUB_ACTION_DISCONNECT, "hub_action_disconnect", wait=1.0)
        await asyncio.wait_for(client.disconnect(), timeout=10)
        note("done", "clean exit with session ended")
    except Exception as e:
        note("fatal", repr(e))
        traceback.print_exc()
        try:
            await client.write_gatt_char(CHAR, HUB_ACTION_DISCONNECT, response=False)
            note("cleanup", "sent hub_action_disconnect after error")
        except Exception:
            pass
    savelog()
    print("log entries:", len(log))

asyncio.run(main())
