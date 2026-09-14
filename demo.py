#!/usr/bin/env python3
"""Demo script for the movehub library — end-to-end live test.
Sequence: calibration -> lights ON -> drive forward -> reverse -> brake ->
lights OFF -> direct motors A/B -> status LED cycle -> battery voltage.
Safe shutdown at the end (HUB_ACTION_DISCONNECT).
"""
import asyncio
from movehub import MoveHub, MOTOR_A, MOTOR_B, LED_COLORS

async def main():
    print("looking for the hub (press the green button)...", flush=True)
    async with MoveHub() as hub:
        print("connected! handshake...", flush=True)
        await hub.handshake()
        await hub.calibrate()
        print("calibration done — lights ON, driving!", flush=True)
        await hub.start_stream()

        hub.set_drive(speed=30, steer=0, lights=True)
        await asyncio.sleep(2.0)
        hub.set_drive(speed=-30, steer=0)
        await asyncio.sleep(2.0)
        hub.set_drive(speed=0, steer=0, brake=True)
        await asyncio.sleep(1.0)
        hub.set_drive(speed=0, steer=0, lights=False)
        await asyncio.sleep(0.5)

        print("direct motors A/B...", flush=True)
        await hub.motor_power(MOTOR_A, 40)
        await asyncio.sleep(1.2)
        await hub.motor_power(MOTOR_A, 0x7F)
        await hub.motor_power(MOTOR_B, 40)
        await asyncio.sleep(1.2)
        await hub.motor_power(MOTOR_B, 0x7F)

        print("status LED cycle...", flush=True)
        for c in ("red", "green", "blue", "yellow", "pink"):
            await hub.set_led(c)
            await asyncio.sleep(0.7)

        v = await hub.read_voltage()
        print(f"battery voltage: {v} mV" if v else "voltage: no response", flush=True)

        print("safe shutdown...", flush=True)
    print("done — session ended cleanly.", flush=True)

asyncio.run(main())
