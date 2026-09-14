#!/usr/bin/env python3
"""movehub — open-source Python driver for LEGO Technic Move Hub 88019 (42176).
Pure LWP3 over BLE (bleak). No LEGO app, no firmware change.

Set the MOVEHUB_ADDR env var to your hub's MAC address (optional;
when empty the scanner auto-discovers the hub by its BLE name).

Verified on 2026-09-13 (probes v3-v15):
  - connect -> (pair once) -> notify -> wait port map (~1.2 s)
  - VM handshake: subscribe -> state -> start-if-stopped -> ARM
  - calibrate (steering sweeps physically!)
  - drive frames as a continuous 20 Hz stream (also keepalive)
  - direct motor power A/B/C, status LED, lights on/off (VM flag)
  - one-shot voltage read
  - ALWAYS end session with HUB_ACTION_DISCONNECT before BLE disconnect —
    otherwise the VM refuses all drive frames in the NEXT session (ERR 0x05),
    and that state survives power-cycles.
"""
import asyncio, os
from typing import Optional

from bleak import BleakClient, BleakScanner

ADDR = os.environ.get("MOVEHUB_ADDR", "")  # optional; empty = auto-discover by name
CHAR = "00001624-1212-efde-1623-785feabcd123"

# --- verified color IDs for the status LED (port 0x3F) ---
LED_COLORS = {
    "off": 0x00, "pink": 0x01, "purple": 0x02, "blue": 0x03,
    "lightblue": 0x04, "cyan": 0x05, "green": 0x06, "yellow": 0x07,
    "orange": 0x08, "red": 0x09, "white": 0x0A,
}

MOTOR_A, MOTOR_B, MOTOR_C = 0x32, 0x33, 0x34
BRAKE, FLOAT = 0x7F, 0x00


class MoveHubError(RuntimeError):
    pass


class MoveHub:
    """Async context manager. Usage:

        async with MoveHub() as hub:
            await hub.calibrate()
            await hub.start_stream()
            hub.set_drive(speed=30, steer=20)
            await asyncio.sleep(2)
            hub.set_drive(0, 0, lights=False)
            print("battery:", await hub.read_voltage(), "mV")
    """

    def __init__(self, address: str = ADDR, pair_if_needed: bool = False):
        self.address = address
        self.pair_if_needed = pair_if_needed
        self.client: Optional[BleakClient] = None
        self._stream_task: Optional[asyncio.Task] = None
        self._drive = {"speed": 0, "steer": 0, "flags": 0x00}
        self._log: list = []

    # ---------- low level ----------

    def _note(self, tag, data):
        self._log.append((tag, bytes(data).hex() if isinstance(data, (bytes, bytearray)) else str(data)))

    def _on_notify(self, _char, data):
        self._note("notify", data)

    async def _w(self, data: bytes, quiet: bool = False):
        if not self.client or not self.client.is_connected:
            raise MoveHubError("not connected")
        if not quiet:
            self._note("write", data)
        await self.client.write_gatt_char(CHAR, data, response=False)

    # ---------- frames ----------

    @staticmethod
    def _vm_arm() -> bytes:
        return bytes([0x09, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x04, 0x01])

    @staticmethod
    def _vm_start() -> bytes:
        return bytes([0x08, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x01])

    @staticmethod
    def _vm_subscribe() -> bytes:
        return bytes([0x0A, 0x00, 0x41, 0x36, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01])

    @staticmethod
    def _vm_state_req() -> bytes:
        return bytes([0x05, 0x00, 0x21, 0x36, 0x00])

    @staticmethod
    def _drive_frame(speed: int, steer: int, flags: int, tail: int = 0x00) -> bytes:
        return bytes([0x0D, 0x00, 0x81, 0x36, 0x11, 0x51, 0x00, 0x03, 0x00,
                      speed & 0xFF, steer & 0xFF, flags & 0xFF, tail & 0xFF])

    # ---------- connect / close ----------

    async def connect(self, scan_timeout_s: float = 600.0):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + scan_timeout_s
        dev = None
        while loop.time() < deadline:
            try:
                dev = await BleakScanner.find_device_by_filter(
                    lambda d, ad: (d.address and d.address.upper() == self.address)
                    or "Technic Move" in (ad.local_name or ""),
                    timeout=15.0)
            except Exception:
                await asyncio.sleep(2)
                continue
            if dev:
                break
            print("  ...rescanning (press the green button)", flush=True)
        if not dev:
            raise MoveHubError("hub not found")

        self.client = BleakClient(dev, timeout=25)
        await asyncio.wait_for(self.client.connect(), timeout=30)
        if self.pair_if_needed:
            try:
                await asyncio.wait_for(self.client.pair(), timeout=45)
            except Exception as e:  # bond usually already exists
                self._note("pair", repr(e))
        await self.client.start_notify(CHAR, self._on_notify)
        await asyncio.sleep(1.2)  # let the port map arrive
        self._note("connected", self.client.is_connected)
        return self

    async def close(self):
        """Safe shutdown. NEVER skip: without HUB_ACTION_DISCONNECT the next
        session's VM refuses all drive frames with ERR 0x05."""
        try:
            if self._stream_task:
                self._stream_task.cancel()
                try:
                    await self._stream_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._stream_task = None
            if self.client and self.client.is_connected:
                await self.set_lights(False)
                await self._w(bytes([0x04, 0x00, 0x02, 0x02]))  # HUB_ACTION_DISCONNECT
                await asyncio.sleep(0.5)
                await asyncio.wait_for(self.client.disconnect(), timeout=10)
        except Exception as e:
            self._note("close_error", repr(e))

    async def __aenter__(self):
        return await self.connect()

    async def __aexit__(self, *exc):
        await self.close()

    # ---------- VM ----------

    async def handshake(self):
        """Subscribe + ARM the drive VM. Call after connect."""
        await self._w(self._vm_subscribe())
        await asyncio.sleep(0.4)
        await self._w(self._vm_state_req())
        await asyncio.sleep(0.6)
        await self._w(self._vm_arm())
        await asyncio.sleep(0.6)

    async def calibrate(self):
        """Steering calibration — the steering physically sweeps. Call on a
        new build (end positions depend on the mechanics)."""
        await self._w(self._drive_frame(0, 0, 0x10))
        await asyncio.sleep(1.6)
        await self._w(self._drive_frame(0, 0, 0x08))
        await asyncio.sleep(2.6)

    # ---------- drive stream (20 Hz) ----------

    async def start_stream(self):
        if self._stream_task and not self._stream_task.done():
            return
        self._stream_task = asyncio.create_task(self._stream_loop())

    async def _stream_loop(self):
        while True:
            d = self._drive
            try:
                await self._w(self._drive_frame(d["speed"], d["steer"], d["flags"]),
                             quiet=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.5)
            await asyncio.sleep(0.05)  # 20 Hz

    def set_drive(self, speed: int = 0, steer: int = 0,
                  lights: bool = True, brake: bool = False, eco: bool = False):
        """speed -100..100, steer -70..70 (Porsche end stops)."""
        if abs(speed) > 100:
            raise ValueError("speed -100..100")
        if abs(steer) > 70:
            raise ValueError("steer -70..70")
        flags = (0x00 if lights else 0x04) | (0x01 if brake else 0) \
                | (0x02 if eco else 0)
        self._drive = {"speed": speed, "steer": steer, "flags": flags}

    # ---------- outputs ----------

    async def set_lights(self, on: bool):
        self._drive["flags"] = (self._drive["flags"] & ~0x04) | (0x00 if on else 0x04)

    async def motor_power(self, motor: int, power: int):
        """Direct motor power -100..100; BRAKE (0x7F) or FLOAT (0x00) to stop."""
        await self._w(bytes([0x08, 0x00, 0x81, motor, 0x00, 0x51, 0x00, power & 0xFF]))

    async def set_led(self, color):
        if isinstance(color, str):
            color = LED_COLORS[color]
        await self._w(bytes([0x08, 0x00, 0x81, 0x3F, 0x11, 0x51, 0x00, color]))

    # ---------- sensors ----------

    async def read_port(self, port: int, mode: int = 0, delta: int = 0x64,
                        wait_s: float = 3.0) -> Optional[bytes]:
        """One-shot sensor read via PortInputFormatSetup (the only working
        path on this firmware — PortValueRequest is ignored, delta 0xFFFF
        is silently ignored; verified v9/v15). Returns the payload bytes of
        the first `45 <port>` notification, or None."""
        before = len(self._log)
        await self._w(bytes([0x0A, 0x00, 0x41, port, mode,
                             delta & 0xFF, (delta >> 8) & 0xFF, 0x00, 0x00,
                             0x01]), quiet=True)
        payload = None
        for _ in range(int(wait_s * 10)):
            await asyncio.sleep(0.1)
            for tag, v in self._log[before:]:
                if tag == "notify":
                    b = bytes.fromhex(v)
                    if len(b) >= 6 and b[2] == 0x45 and b[3] == port:
                        payload = b[4:]
                        break
            if payload is not None:
                break
        # notifications off (verified v9 off frame)
        await self._w(bytes([0x0A, 0x00, 0x41, port, mode,
                             0x00, 0x00, 0x00, 0x00, 0x00]), quiet=True)
        return payload

    async def read_voltage(self) -> Optional[int]:
        """One-shot battery voltage in mV (port 0x3C, mode 0)."""
        p = await self.read_port(0x3C, mode=0, delta=0x64)
        return int.from_bytes(p[0:2], "little") if p and len(p) >= 2 else None

    async def read_accel(self) -> Optional[tuple]:
        """One-shot accelerometer sample in mG (port 0x38, mode 0: X/Y/Z
        32-bit int LE). NOTE: WRITES (0x81 output) to 0x38 crash the hub —
        this only uses the safe input-format path (verified v9)."""
        p = await self.read_port(0x38, mode=0, delta=100)
        if p and len(p) >= 12:
            xyz = [int.from_bytes(p[i:i + 4], "little", signed=True)
                   for i in (0, 4, 8)]
            return tuple(xyz)
        return None
