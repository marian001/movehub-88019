package com.hermes.movehubremote;

import org.junit.Test;
import static org.junit.Assert.*;

public class ControlRouterTest {
    @Test public void disconnectedHubsRejectMotion() throws Exception {
        ControlRouter router = new ControlRouter();
        try {
            Object state = ControlRouter.class.getMethod("state", ControlRouter.Hub.class)
                    .invoke(router, ControlRouter.Hub.PORSCHE);
            state.getClass().getMethod("drive", int.class, int.class).invoke(state, 70, 40);
            assertEquals(0, state.getClass().getField("speed").getInt(state));
        } catch (NoSuchMethodException e) { fail("Missing per-hub fail-closed safety state"); }
    }

    @Test public void releaseClearsMotionBeforeReconnect() throws Exception {
        ControlRouter.State s = new ControlRouter().state(ControlRouter.Hub.PORSCHE);
        s.ready = true;
        s.drive(70, 40);
        try { s.getClass().getMethod("stop").invoke(s); }
        catch (NoSuchMethodException e) { fail("Release has no motion reset"); }
        assertEquals(0, s.speed);
        assertEquals(0, s.steer);
    }

    @Test public void bothRoutingStopsAndNeverRestoresMotionOnTargetChange() throws Exception {
        ControlRouter r = new ControlRouter();
        for (ControlRouter.Hub h : ControlRouter.Hub.values()) r.state(h).ready = true;
        try {
            r.getClass().getMethod("select", ControlRouter.Hub.class, boolean.class)
                    .invoke(r, ControlRouter.Hub.PORSCHE, true);
            r.getClass().getMethod("drive", int.class, int.class).invoke(r, 40, -20);
            assertEquals(40, r.state(ControlRouter.Hub.PORSCHE).speed);
            assertEquals(40, r.state(ControlRouter.Hub.LAMBORGHINI).speed);
            r.getClass().getMethod("select", ControlRouter.Hub.class, boolean.class)
                    .invoke(r, ControlRouter.Hub.LAMBORGHINI, false);
            assertEquals(0, r.state(ControlRouter.Hub.PORSCHE).speed);
            assertEquals(0, r.state(ControlRouter.Hub.LAMBORGHINI).speed);
            r.getClass().getMethod("drive", int.class, int.class).invoke(r, -30, 10);
            assertEquals(0, r.state(ControlRouter.Hub.PORSCHE).speed);
            assertEquals(-30, r.state(ControlRouter.Hub.LAMBORGHINI).speed);
        } catch (NoSuchMethodException e) { fail("Missing explicit BOTH routing / target-change reset"); }
    }

    @Test public void disconnectInvalidatesHeldInputAndReconnectStartsAtZero() throws Exception {
        ControlRouter.State s = new ControlRouter().state(ControlRouter.Hub.PORSCHE);
        s.ready = true; s.drive(80, -40);
        try {
            long epoch = s.getClass().getField("epoch").getLong(s);
            s.getClass().getMethod("setReady", boolean.class).invoke(s, false);
            s.getClass().getMethod("setReady", boolean.class).invoke(s, true);
            assertEquals(0, s.speed);
            assertEquals(0, s.steer);
            assertTrue(s.getClass().getField("epoch").getLong(s) > epoch);
        } catch (NoSuchMethodException | NoSuchFieldException e) {
            fail("Missing lifecycle reset and stale-gesture generation");
        }
    }

    @Test public void safetyQueueDropsPendingMovementButNotInflightWrite() throws Exception {
        Class<?> type;
        try { type = Class.forName("com.hermes.movehubremote.SessionQueue"); }
        catch (ClassNotFoundException e) { fail("Missing per-session serialized safety queue"); return; }
        Object q = type.getConstructor().newInstance();
        type.getMethod("add", byte[].class).invoke(q, new byte[]{1});
        assertArrayEquals(new byte[]{1}, (byte[]) type.getMethod("take").invoke(q));
        type.getMethod("add", byte[].class).invoke(q, new byte[]{2});
        type.getMethod("clearPending").invoke(q);
        type.getMethod("add", byte[].class).invoke(q, new byte[]{0});
        assertNull(type.getMethod("take").invoke(q));
        type.getMethod("complete").invoke(q);
        assertArrayEquals(new byte[]{0}, (byte[]) type.getMethod("take").invoke(q));
    }

    @Test public void protocolOnlyAllowsKnownMotorPortsAndNonzeroSensorDelta() throws Exception {
        Class<?> p;
        try { p = Class.forName("com.hermes.movehubremote.HubProtocol"); }
        catch (ClassNotFoundException e) { fail("Missing safe protocol frame factory"); return; }
        for (int port = 0x38; port <= 0x40; port++) {
            try { p.getMethod("motor", int.class, int.class).invoke(null, port, 30); fail("Unsafe output port allowed"); }
            catch (java.lang.reflect.InvocationTargetException e) { assertTrue(e.getCause() instanceof IllegalArgumentException); }
        }
        byte[] off = (byte[]) p.getMethod("voltage", boolean.class).invoke(null, false);
        assertEquals(100, off[5] & 255);
        assertEquals(0, off[9]);
        assertArrayEquals(new byte[]{4,0,2,2}, (byte[]) p.getField("END").get(null));
    }

    @Test public void knownHubsHaveStrictDistinctAddresses() throws Exception {
        Class<?> hub;
        try { hub = Class.forName("com.hermes.movehubremote.ControlRouter$Hub"); }
        catch (ClassNotFoundException e) { fail("Missing strict dual-hub identity model"); return; }
        Object[] values = hub.getEnumConstants();
        assertEquals(2, values.length);
        assertEquals("80:C4:1B:DD:F9:4C", hub.getField("address").get(values[0]));
        assertEquals("F0:10:A5:1F:5F:F1", hub.getField("address").get(values[1]));
    }
}
