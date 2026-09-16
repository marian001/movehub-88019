package com.hermes.movehubremote;

/** Only verified LWP3 operations: no generic output/probe API. */
public final class HubProtocol {
    private HubProtocol() {}
    // VM states on port 0x36, byte 4 of the 45 36 stream (protocol.md §4.0):
    // a fresh session reports 02 STOPPED; 01 RUNNING; 03 EXECUTING while driving.
    public static final Integer VM_RUNNING = 1, VM_STOPPED = 2, VM_EXECUTING = 3;
    /** Parses a VM state frame (45 36 <state> ...); null when not one. */
    public static Integer parseVmState(byte[] data) {
        if (data == null || data.length < 6 || data[2] != 0x45 || data[3] != 0x36) return null;
        int state = data[4] & 255;
        if (state == 1) return VM_RUNNING;
        if (state == 2) return VM_STOPPED;
        if (state == 3) return VM_EXECUTING;
        return null;
    }
    /** Parses a PortOutputCommandFeedback frame for the VM port (82 36 <status>); null when not one. */
    public static Integer parsePortFeedback(byte[] data) {
        if (data == null || data.length < 5 || data[2] != (byte) 0x82 || data[3] != 0x36) return null;
        return data[4] & 255;
    }
    public static final byte[] END = {4,0,2,2};
    public static final byte[] SUBSCRIBE = {10,0,0x41,0x36,0,1,0,0,0,1};
    public static final byte[] STATE = {5,0,0x21,0x36,0};
    public static final byte[] ARM = {9,0,(byte)0x81,0x36,0x11,0x51,0,4,1};
    public static final byte[] VM_STOP = {10,0,(byte)0x81,0x36,0x11,0x51,0,0,0,0};
    public static final byte[] VM_START = {8,0,(byte)0x81,0x36,0x11,0x51,0,1};
    public static byte[] drive(int speed, int steer, int flags) {
        if (speed < -100 || speed > 100 || steer < -70 || steer > 70)
            throw new IllegalArgumentException("Drive outside safe limits");
        return new byte[]{13,0,(byte)0x81,0x36,0x11,0x51,0,3,0,(byte)speed,(byte)steer,(byte)flags,0};
    }
    public static byte[] motor(int port, int power) {
        if (port < 0x32 || port > 0x34 || (power != 127 && (power < -100 || power > 100)))
            throw new IllegalArgumentException("Only motor A/B/C power is allowed");
        return new byte[]{8,0,(byte)0x81,(byte)port,0,0x51,0,(byte)power};
    }
    public static byte[] voltage(boolean on) {
        return new byte[]{10,0,0x41,0x3c,0,100,0,0,0,(byte)(on ? 1 : 0)};
    }
    // Explicit verified LED exception, not arbitrary writes to 0x38+ sensors.
    public static byte[] led(int color) {
        if (color < 0 || color > 10) throw new IllegalArgumentException("LED color");
        return new byte[]{8,0,(byte)0x81,0x3f,0x11,0x51,0,(byte)color};
    }
}
