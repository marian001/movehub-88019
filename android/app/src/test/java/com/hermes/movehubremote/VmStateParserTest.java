package com.hermes.movehubremote;

import org.junit.Test;
import static org.junit.Assert.*;

/** Root cause 2026-09-16: a freshly connected hub reports its VM state as
 *  0c 00 45 36 02 ... (STOPPED) — never the 45 36 03 (EXECUTING) family the
 *  app waited for, so vmSeen never became true and every connect failed.
 *  Verified live + probes/logs v11/v15 + docs/protocol.md §4.0:
 *  state byte 01=RUNNING, 02=STOPPED, 03=EXECUTING. */
public class VmStateParserTest {
    private static byte[] state(int first) {
        return new byte[]{0x0c, 0x00, 0x45, 0x36, (byte) first, 0, 1};
    }

    @Test public void stoppedStateIsRecognizedAndRequiresRestart() throws Exception {
        Class<?> p = Class.forName("com.hermes.movehubremote.HubProtocol");
        Object r = p.getMethod("parseVmState", byte[].class).invoke(null, (Object) state(0x02));
        assertNotNull("Fresh-connect STOPPED state (45 36 02) must be recognized", r);
        assertTrue("STOPPED must be reported as stopped", p.getField("VM_STOPPED").get(null).equals(r));
    }

    @Test public void runningStateIsRecognizedWithoutRestart() throws Exception {
        Class<?> p = Class.forName("com.hermes.movehubremote.HubProtocol");
        Object r = p.getMethod("parseVmState", byte[].class).invoke(null, (Object) state(0x01));
        assertEquals("RUNNING must be recognized and not flagged stopped",
                     p.getField("VM_RUNNING").get(null), r);
    }

    @Test public void executingStateIsRecognizedWithoutRestart() throws Exception {
        Class<?> p = Class.forName("com.hermes.movehubremote.HubProtocol");
        Object r = p.getMethod("parseVmState", byte[].class).invoke(null, (Object) state(0x03));
        assertEquals("EXECUTING must be recognized and not flagged stopped",
                     p.getField("VM_EXECUTING").get(null), r);
    }

    @Test public void unknownStateIsRejected() throws Exception {
        Class<?> p = Class.forName("com.hermes.movehubremote.HubProtocol");
        assertNull("Unknown state byte must not be accepted", p.getMethod("parseVmState", byte[].class)
                .invoke(null, (Object) state(0x07)));
    }

    @Test public void shortOrWrongFramesAreRejected() throws Exception {
        Class<?> p = Class.forName("com.hermes.movehubremote.HubProtocol");
        assertNull(p.getMethod("parseVmState", byte[].class).invoke(null, (Object) new byte[]{0x0c, 0x00, 0x45, 0x36}));
        assertNull(p.getMethod("parseVmState", byte[].class).invoke(null, (Object) new byte[]{0x0c, 0x00, 0x45, 0x37, 0x02, 0, 1}));
    }

    /** Root cause #2 (2026-09-16): PortOutputCommandFeedback 82 36 status 0x04
     *  ("in progress", normal under the 20 Hz drive stream) was treated as a
     *  refusal 350 ms after Ready. Healthy sessions only ever show 01/0A on
     *  idle writes (probes v5-v15 + live); refusals arrive as Error frames
     *  (05 00 05 81 <code>), never as 82 feedback. */
    @Test public void portFeedbackParsingCoversInProgressStatus() throws Exception {
        Class<?> p = Class.forName("com.hermes.movehubremote.HubProtocol");
        java.lang.reflect.Method m = p.getMethod("parsePortFeedback", byte[].class);
        assertEquals(4, m.invoke(null, (Object) new byte[]{0x05, 0x00, (byte) 0x82, 0x36, 0x04}));
        assertEquals(1, m.invoke(null, (Object) new byte[]{0x05, 0x00, (byte) 0x82, 0x36, 0x01}));
        assertEquals(0x0A, m.invoke(null, (Object) new byte[]{0x05, 0x00, (byte) 0x82, 0x36, 0x0A}));
        assertNull(m.invoke(null, (Object) new byte[]{0x05, 0x00, (byte) 0x82, 0x37, 0x04}));
        assertNull(m.invoke(null, (Object) new byte[]{0x0c, 0x00, 0x45, 0x36, 0x02, 0, 1}));
    }
}
