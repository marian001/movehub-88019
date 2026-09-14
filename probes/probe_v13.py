#!/usr/bin/env python3
"""M0b probe v13 — the missing piece: END SESSION on disconnect.
Root cause found: probes v8-v12 closed the BLE link without sending
HUB_ACTION_DISCONNECT (04 00 02 02), leaving the hub's virtual drive port
bound to a dead session. Next connection -> drive frames ERR 0x05.
This probe: connect -> VM handshake (subscribe/state/arm) -> calibration ->
drive sanity (lights on/off) -> flags sweep -> tail sweep -> demo ->
*** HUB_ACTION_DISCONNECT *** -> BLE disconnect.
Power-cycle the hub first for a clean slate (recommended), then green button.
"""
import asyncio, datetime, json, traceback, os
from bleak import BleakScanner, BleakClient

ADDR = os.environ.get("MOVEHUB_ADDR", "")  # your hub MAC; empty = discover by name
CHAR = "00001624-1212-efde-1623-785feabcd123"
LOG_PATH = "probe_m0_log_v13.json"

VM_STOP  = bytes([0x0A, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x00, 0x00, 0x00])
VM_START = bytes([0x08, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x01])
VM_ARM   = bytes([0x09, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x04, 0x01])
VM_SUBSCRIBE = bytes([0x0A, 0x00, 0x41, 0x36, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01])
VM_STATE_REQ = bytes([0x05, 0x00, 0x21, 0x36, 0x00])
HUB_ACTION_DISCONNECT = bytes([0x04, 0x00, 0x02, 0x02])

VM_STATES = {(0x01,0x00):"RUNNING", (0x02,0x00):"STOPPING",
             (0x00,0x01):"STOPPED", (0x03,0x01):"EXECUTING"}

log = []
notify_event = asyncio.Event()
vm_state = {"name": "UNKNOWN", "raw": None}

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
    b = bytes(data)
    note("notify", b)
    if len(b) >= 6 and b[2] == 0x45 and b[3] == 0x36:
        raw = (b[4], b[5])
        vm_state["raw"] = raw
        vm_state["name"] = VM_STATES.get(raw, f"UNKNOWN{raw}")
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
        await asyncio.sleep(1.5)

        await w(client, LED(0x06), "led_green_sanity", wait=0.4)

        # ---------- VM handshake ----------
        note("phase", "VM handshake")
        await w(client, VM_SUBSCRIBE, "vm_subscribe", wait=0.5)
        await w(client, VM_STATE_REQ, "vm_state_req", wait=1.0)
        note("vm_state_initial", vm_state["name"] + " " + str(vm_state["raw"]))
        if vm_state["name"] == "STOPPED":
            note("phase", "VM STOPPED -> restart")
            await w(client, VM_STOP, "vm_stop", wait=0.6)
            await w(client, VM_START, "vm_start", wait=1.2)
        await w(client, VM_ARM, "vm_arm", wait=0.6)
        await w(client, VM_STATE_REQ, "vm_state_req2", wait=0.8)
        note("vm_state_after", vm_state["name"] + " " + str(vm_state["raw"]))

        # ---------- calibration ----------
        note("phase", "calibration — steering SWEEPS now")
        await w(client, vm(0, 0, 0x10), "calib1", wait=1.6)
        await w(client, vm(0, 0, 0x08), "calib2", wait=2.6)

        # ---------- drive sanity ----------
        note("phase", "drive sanity: lights on/off (must ACK)")
        await w(client, vm(0, 0, 0x00), "lights_on", wait=1.2)
        await w(client, vm(0, 0, 0x04), "lights_off", wait=0.8)

        # ---------- flags sweep ----------
        note("phase", "flags sweep — WATCH LIGHTS + STEERING")
        for bit, name in ((0x20, "f20"), (0x40, "f40"), (0x80, "f80"),
                          (0x24, "f24"), (0x28, "f28")):
            await w(client, vm(0, 0, 0x00), f"base_on_{name}", wait=0.6)
            await w(client, vm(0, 0, bit), f"flags_{name}", wait=2.0)
            await w(client, vm(0, 0, 0x04), f"off_{name}", wait=0.5)

        # ---------- tail sweep ----------
        note("phase", "tail byte sweep")
        for tail in (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80):
            await w(client, vm(0, 0, 0x00), f"t_base_{tail:02x}", wait=0.5)
            await w(client, vm(0, 0, 0x00, tail=tail), f"tail_{tail:02x}", wait=1.6)
            await w(client, vm(0, 0, 0x04), f"t_off_{tail:02x}", wait=0.4)

        # ---------- demo ----------
        note("phase", "demo: blink x3")
        for i in range(3):
            await w(client, vm(0, 0, 0x00), f"demo_on_{i}", wait=0.7)
            await w(client, vm(0, 0, 0x04), f"demo_off_{i}", wait=0.7)

        # ---------- THE FIX: end session properly ----------
        note("phase", "end session properly (HUB_ACTION_DISCONNECT)")
        await w(client, vm(0, 0, 0x04), "final_lights_off", wait=0.5)
        await w(client, HUB_ACTION_DISCONNECT, "hub_action_disconnect", wait=1.0)
        await asyncio.wait_for(client.disconnect(), timeout=10)
        note("done", "clean exit with session ended")
    except Exception as e:
        note("fatal", repr(e))
        traceback.print_exc()
        # still try to end the session
        try:
            await client.write_gatt_char(CHAR, HUB_ACTION_DISCONNECT, response=False)
            note("cleanup", "sent hub_action_disconnect after error")
        except Exception:
            pass
    savelog()
    print("log entries:", len(log))

asyncio.run(main())
