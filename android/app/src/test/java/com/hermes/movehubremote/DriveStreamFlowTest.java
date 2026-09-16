package com.hermes.movehubremote;

import org.junit.Test;
import static org.junit.Assert.*;

/** Reproduce the "connected but Porsche motors dead" report (2026-09-16):
 *  is the 20 Hz drive stream path from Panel.hold -> setDrive -> tick enqueued,
 *  and does a nonzero speed frame actually leave the queue when idle? */
public class DriveStreamFlowTest {
    /** SessionQueue: idle->add->take must hand out the frame exactly once. */
    @Test public void queueHandsOutDriveFrameOnceWhenIdle() {
        SessionQueue q = new SessionQueue();
        assertTrue(q.idle());
        byte[] drive = HubProtocol.drive(40, 0, 0);
        q.add(drive);
        assertFalse(q.idle());
        assertArrayEquals("queue must hand out the exact drive frame (it clones defensively)",
                drive, q.take());
        assertNull("must not hand out a second frame while one is active", q.take());
        assertNotNull(q.complete());
        assertTrue(q.idle());
    }

    /** The exact frame Panel.hold(UP) must produce: speed +40, steer kept. */
    @Test public void upArrowProducesNonzeroSpeedFrame() {
        byte[] f = HubProtocol.drive(40, 0, 0);
        assertEquals(13, f.length);
        // 0d 00 81 36 11 51 00 03 00 | speed | steer | flags | tail
        assertEquals(0x0d, f[0] & 255);
        assertEquals(0x81, f[2] & 255);
        assertEquals(0x36, f[3] & 255);
        assertEquals(0x03, f[7] & 255);   // mode 03 = drive
        assertEquals(40, f[9]);           // speed byte
        assertEquals(0, f[10]);           // steer byte
    }

    /** State clamping must not zero a legal held value. */
    @Test public void stateDriveKeepsHeldValue() {
        ControlRouter.State s = new ControlRouter.State();
        s.ready = true;
        s.drive(40, 0);
        assertEquals(40, s.speed);
        assertEquals(0, s.steer);
    }
}
