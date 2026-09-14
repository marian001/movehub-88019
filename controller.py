#!/usr/bin/env python3
"""Controller — Technic Move Hub 88019.
Interactive keyboard control (terminal, raw mode):

  AUTO mode (VM drive, like the Porsche):
    W/S ........ speed +/- 10 (held, like a throttle)
    A/D ........ steering +/- 10
    L .......... lights on/off
    B .......... brake (momentary, holds the brake lights)
    SPACE ...... emergency STOP (speed 0)
    C .......... steering calibration
  ROBOT mode (direct motors, for custom builds):
    I/K ........ motor A +/- 10     J/L ...... motor B +/- 10
    U/O ........ motor C (servo) +/- 10
    X .......... all STOP (brake)
  both:
    arrows ..... same as W/S/A/D (up = faster, down = slower, left/right = steer)
    V .......... battery voltage
    T .......... IMU (accelerometer XYZ)
    M .......... toggle AUTO/ROBOT mode (LED: cyan/orange)
    Q/ESC ...... quit (safe shutdown; Ctrl+C also exits cleanly)
    ? .......... this help

Watchdog (independent task): in AUTO mode it stops the car 2 s after the
last input. The drive stream keeps running (keepalive).

Script mode: --script "wwd@" replays the keys with a 0.35 s delay
('@' = 1.2 s pause), --final-wait N = N s of silence at the end
(watchdog test).
"""
import asyncio
import os
import sys
import termios
import tty

from movehub import (MoveHub, MOTOR_A, MOTOR_B, MOTOR_C, BRAKE, FLOAT,
                     LED_COLORS)


def script_keys(script: str):
    """Parse --script into (key, delay_s) pairs. '@' = pause (no key)."""
    out = []
    for c in script:
        if c == "@":
            out.append((None, 1.2))
        else:
            out.append((c, 0.35))
    return out


class KeyReader:
    """Raw terminal via loop.add_reader + asyncio.Queue.

    NOTE (bug from the first version): sys.stdin.read(1) inside
    run_in_executor with a timeout leaks threads — every timeout leaves a
    thread hanging in read() and a pressed key ends up in an already
    cancelled future (the keys "stopped working"). add_reader reads the fd
    directly from the event loop — no threads, nothing gets lost."""

    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.old = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.loop = None

    def __enter__(self):
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        self.loop = asyncio.get_event_loop()
        self.loop.add_reader(self.fd, self._on_readable)
        return self

    def _on_readable(self):
        try:
            data = os.read(self.fd, 1)
        except OSError:
            return
        if data:
            self.queue.put_nowait(data.decode("utf-8", errors="ignore"))

    def __exit__(self, *a):
        try:
            self.loop.remove_reader(self.fd)
        except Exception:
            pass
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    async def get(self) -> str:
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=0.05)
        except asyncio.TimeoutError:
            return ""


class _NoReader:
    """Script mode — no terminal access at all."""
    async def get(self):
        await asyncio.sleep(0.1)
        return ""
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


HELP = """\
=== AUTO (VM drive) ===   W/S speed  A/D steering  L lights  B brake
C calibrate  SPACE STOP  M -> ROBOT mode
=== ROBOT (motors) ===    I/K motor A  J/L motor B  U/O motor C  X STOP
arrows = W/S/A/D   V voltage  T IMU  M -> AUTO mode
Q/ESC quit"""


async def main():
    script = None
    if "--script" in sys.argv:
        script = script_keys(sys.argv[sys.argv.index("--script") + 1])
        print(f"SCRIPT mode: replaying {len(script)} steps", flush=True)
    if script is not None and "--final-wait" in sys.argv:
        script = script + [(None, float(sys.argv[sys.argv.index("--final-wait") + 1]))]
    if script is None and not os.isatty(sys.stdin.fileno()):
        print("WARNING: stdin is not a terminal — keys will not work. "
              "Use --script for autonomous mode.", flush=True)

    print("looking for the hub (press the green button)...", flush=True)
    hub = MoveHub()
    await hub.connect()
    print("connected! handshake + calibration...", flush=True)
    await hub.handshake()
    await hub.calibrate()
    await hub.start_stream()
    print(HELP, flush=True)

    state = {
        "auto_mode": True,
        "speed": 0, "steer": 0, "lights": True,
        "m_a": 0, "m_b": 0, "m_c": 0,
        "last_input": asyncio.get_event_loop().time(),
    }

    def apply_drive():
        hub.set_drive(state["speed"], state["steer"], lights=state["lights"])

    apply_drive()

    async def watchdog():
        """Independent task: AUTO mode -> stop 2 s after the last input."""
        while True:
            await asyncio.sleep(0.5)
            if state["auto_mode"] and state["speed"] != 0 and \
                    asyncio.get_event_loop().time() - state["last_input"] > 2.0:
                state["speed"] = 0
                apply_drive()
                print("watchdog: auto-stop", flush=True)

    wd_task = asyncio.create_task(watchdog())

    reader_ctx = KeyReader() if (script is None and os.isatty(sys.stdin.fileno())) else _NoReader()
    try:
        with reader_ctx as kr:
            si = 0
            while True:
                if script is not None:
                    if si >= len(script):
                        break
                    key, delay = script[si]
                    si += 1
                    await asyncio.sleep(delay)
                    if key is None:
                        continue
                    ch = key
                else:
                    ch = await kr.get()
                if ch:
                    state["last_input"] = asyncio.get_event_loop().time()

                # ESC = quit; ESC+[+A..D = arrows -> map to WASD
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
                elif ch == "m":
                    state["auto_mode"] = not state["auto_mode"]
                    if state["auto_mode"]:
                        state["speed"] = state["steer"] = 0
                        apply_drive()
                    else:
                        for m in (MOTOR_A, MOTOR_B, MOTOR_C):
                            await hub.motor_power(m, BRAKE)
                    await hub.set_led("cyan" if state["auto_mode"] else "orange")
                    print(f"mode: {'AUTO (drive)' if state['auto_mode'] else 'ROBOT (motors)'}",
                          flush=True)
                elif ch == "v":
                    v = await hub.read_voltage()
                    print(f"voltage: {v} mV" if v else "voltage: no response", flush=True)
                elif ch == "t":
                    a = await hub.read_accel()
                    print(f"accel XYZ: {a} mG" if a else "accel: no response", flush=True)
                elif ch == "c" and state["auto_mode"]:
                    print("calibrating steering...", flush=True)
                    await hub.calibrate()
                    print("done", flush=True)
                elif state["auto_mode"]:
                    if ch == "w":
                        state["speed"] = max(-100, min(100, state["speed"] + 10))
                        print(f"speed {state['speed']}", flush=True)
                    elif ch == "s":
                        state["speed"] = max(-100, min(100, state["speed"] - 10))
                        print(f"speed {state['speed']}", flush=True)
                    elif ch == "a":
                        state["steer"] = max(-70, min(70, state["steer"] - 10))
                        print(f"steer {state['steer']}", flush=True)
                    elif ch == "d":
                        state["steer"] = max(-70, min(70, state["steer"] + 10))
                        print(f"steer {state['steer']}", flush=True)
                    elif ch == "l":
                        state["lights"] = not state["lights"]
                        apply_drive()
                        print(f"lights {'on' if state['lights'] else 'off'}", flush=True)
                    elif ch == "b":
                        hub.set_drive(state["speed"], state["steer"],
                                      lights=state["lights"], brake=True)
                        await asyncio.sleep(0.4)
                        apply_drive()
                    elif ch == " ":
                        state["speed"] = 0
                        print("STOP", flush=True)
                    if ch in ("w", "s", "a", "d", " "):
                        apply_drive()
                else:  # ROBOT mode
                    if ch == "i":
                        state["m_a"] = max(-100, min(100, state["m_a"] + 10))
                        print(f"motor A {state['m_a']}", flush=True)
                    elif ch == "k":
                        state["m_a"] = max(-100, min(100, state["m_a"] - 10))
                        print(f"motor A {state['m_a']}", flush=True)
                    elif ch == "j":
                        state["m_b"] = max(-100, min(100, state["m_b"] + 10))
                        print(f"motor B {state['m_b']}", flush=True)
                    elif ch == "l":
                        state["m_b"] = max(-100, min(100, state["m_b"] - 10))
                        print(f"motor B {state['m_b']}", flush=True)
                    elif ch == "u":
                        state["m_c"] = max(-100, min(100, state["m_c"] + 10))
                        print(f"motor C {state['m_c']}", flush=True)
                    elif ch == "o":
                        state["m_c"] = max(-100, min(100, state["m_c"] - 10))
                        print(f"motor C {state['m_c']}", flush=True)
                    elif ch == "x":
                        state["m_a"] = state["m_b"] = state["m_c"] = 0
                        for m in (MOTOR_A, MOTOR_B, MOTOR_C):
                            await hub.motor_power(m, BRAKE)
                        print("STOP", flush=True)
                    if ch in ("i", "k", "j", "l", "u", "o"):
                        await hub.motor_power(MOTOR_A, state["m_a"] or FLOAT)
                        await hub.motor_power(MOTOR_B, state["m_b"] or FLOAT)
                        await hub.motor_power(MOTOR_C, state["m_c"] or FLOAT)
                await asyncio.sleep(0.01)
    finally:
        # ALSO on Ctrl+C / exception: clean shutdown (VM lock prevention)
        wd_task.cancel()
        print("safe shutdown...", flush=True)
        try:
            await hub.close()
            print("bye — session ended cleanly.", flush=True)
        except Exception as e:
            print(f"shutdown error: {e!r}", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # finally in main() already did the clean shutdown
