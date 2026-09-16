#!/usr/bin/env python3
"""Dual-hub controller — TWO Technic Move Hubs 88019 driven at once (BOTH mode).

Both hubs get every drive command in the same tick — the Python equivalent of
the Android app's BOTH mode (Porsche + Lamborghini driving together).

  W/S ........ speed +/- 10 (both cars, held like a throttle)
  A/D ........ steering +/- 10 (both)
  L .......... lights on/off (both)
  B .......... brake (momentary, both)
  SPACE ...... emergency STOP (both)
  C .......... steering calibration (both)
  arrows ..... same as W/S/A/D
  V .......... battery voltage (both)
  T .......... IMU (hub 1 only — both would interleave output)
  Q/ESC ...... quit (safe shutdown on BOTH hubs; Ctrl+C also exits cleanly)

Hub addresses (defaults are the two known cars, override via env):
  MOVEHUB_PORSCHE_ADDR (default 80:C4:1B:DD:F9:4C)
  MOVEHUB_LAMBO_ADDR   (default F0:10:A5:1F:5F:F1)

Connect order is sequential: hub 1 must appear (green button if it sleeps —
advertising runs a few minutes after activity) before hub 2 is scanned.

Watchdog: BOTH stops both cars 2 s after the last input.
"""
import asyncio
import os
import sys

from movehub import MoveHub

PORSCHE_ADDR = os.environ.get("MOVEHUB_PORSCHE_ADDR", "80:C4:1B:DD:F9:4C").upper()
LAMBO_ADDR = os.environ.get("MOVEHUB_LAMBO_ADDR", "F0:10:A5:1F:5F:F1").upper()

HELP = """\
=== DUAL BOTH (two cars) ===
W/S speed  A/D steering  L lights  B brake  SPACE STOP  C calibrate
arrows = W/S/A/D   V voltage (both)   T IMU (hub 1)   Q/ESC quit"""

# reuse the terminal reader from the single-hub controller
from controller import KeyReader, _NoReader


async def connect_both():
    """Sequential connect: hub 1 then hub 2 (each needs its green button if asleep)."""
    hubs = []
    for label, addr in (("hub 1 (Porsche)", PORSCHE_ADDR), ("hub 2 (Lambo)", LAMBO_ADDR)):
        print(f"looking for {label} {addr} (press its green button)...", flush=True)
        hub = MoveHub(addr)
        await hub.connect(scan_timeout_s=120.0)
        print(f"{label}: connected, handshake + calibration...", flush=True)
        await hub.handshake()
        await hub.calibrate()
        await hub.start_stream()
        hubs.append(hub)
    return hubs


async def main():
    print(f"dual-hub BOTH controller: {PORSCHE_ADDR} + {LAMBO_ADDR}", flush=True)
    hubs = await connect_both()

    state = {"speed": 0, "steer": 0, "lights": True,
             "last_input": asyncio.get_event_loop().time()}

    def apply_drive():
        for h in hubs:
            h.set_drive(state["speed"], state["steer"], lights=state["lights"])

    apply_drive()
    print(HELP, flush=True)

    async def watchdog():
        """Both cars stop 2 s after the last input (fail-closed)."""
        while True:
            await asyncio.sleep(0.5)
            if state["speed"] != 0 and \
                    asyncio.get_event_loop().time() - state["last_input"] > 2.0:
                state["speed"] = 0
                apply_drive()
                print("watchdog: both cars auto-stop", flush=True)

    wd_task = asyncio.create_task(watchdog())

    reader_ctx = KeyReader() if os.isatty(sys.stdin.fileno()) else _NoReader()
    if not os.isatty(sys.stdin.fileno()):
        print("WARNING: stdin is not a terminal — keys will not work.", flush=True)

    try:
        with reader_ctx as kr:
            while True:
                ch = await kr.get()
                if ch:
                    state["last_input"] = asyncio.get_event_loop().time()

                # ESC = quit; ESC+[+A..D = arrows -> WASD
                if ch == "\x1b":
                    await asyncio.sleep(0.03)
                    n1 = await kr.get()
                    n2 = await kr.get() if n1 else ""
                    if n1 == "[" and n2 in "ABCD":
                        ch = {"A": "w", "B": "s", "C": "d", "D": "a"}[n2]
                        state["last_input"] = asyncio.get_event_loop().time()
                    else:
                        break

                if ch in ("q",):
                    break
                elif ch == "?":
                    print(HELP, flush=True)
                elif ch == "w":
                    state["speed"] = max(-100, min(100, state["speed"] + 10))
                    print(f"speed {state['speed']} -> both", flush=True)
                elif ch == "s":
                    state["speed"] = max(-100, min(100, state["speed"] - 10))
                    print(f"speed {state['speed']} -> both", flush=True)
                elif ch == "a":
                    state["steer"] = max(-70, min(70, state["steer"] - 10))
                    print(f"steer {state['steer']} -> both", flush=True)
                elif ch == "d":
                    state["steer"] = max(-70, min(70, state["steer"] + 10))
                    print(f"steer {state['steer']} -> both", flush=True)
                elif ch == "l":
                    state["lights"] = not state["lights"]
                    apply_drive()
                    print(f"lights {'on' if state['lights'] else 'off'} -> both", flush=True)
                elif ch == "b":
                    for h in hubs:
                        h.set_drive(state["speed"], state["steer"],
                                    lights=state["lights"], brake=True)
                    await asyncio.sleep(0.4)
                    apply_drive()
                elif ch == " ":
                    state["speed"] = 0
                    print("STOP -> both", flush=True)
                elif ch == "c":
                    print("calibrating steering on both...", flush=True)
                    for h in hubs:
                        await h.calibrate()
                    print("done", flush=True)
                elif ch == "v":
                    for i, h in enumerate(hubs, 1):
                        v = await h.read_voltage()
                        print(f"hub {i}: {v} mV" if v else f"hub {i}: no response",
                              flush=True)
                elif ch == "t":
                    a = await hubs[0].read_accel()
                    print(f"hub 1 accel XYZ: {a} mG" if a else "hub 1: no response",
                          flush=True)
                if ch in ("w", "s", "a", "d", " "):
                    apply_drive()
                await asyncio.sleep(0.01)
    finally:
        # Clean shutdown on BOTH hubs — NEVER skip: without the END frame the
        # next session's VM refuses all drive frames with ERR 0x05.
        wd_task.cancel()
        print("safe shutdown (both hubs)...", flush=True)
        for i, h in enumerate(hubs, 1):
            try:
                await h.close()
                print(f"hub {i}: session ended cleanly.", flush=True)
            except Exception as e:
                print(f"hub {i}: shutdown error: {e!r}", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # finally in main() already did the clean shutdown
