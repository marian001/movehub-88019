package com.hermes.movehubremote;

import java.util.ArrayDeque;

/** One instance per GATT session, owned by the Android main looper. */
public final class SessionQueue {
    private final ArrayDeque<byte[]> pending = new ArrayDeque<>();
    private byte[] active;
    public void add(byte[] frame) { pending.add(frame.clone()); }
    public byte[] take() {
        if (active != null) return null;
        active = pending.poll();
        return active;
    }
    public byte[] complete() { byte[] old = active; active = null; return old; }
    public void clearPending() { pending.clear(); }
    public boolean idle() { return active == null && pending.isEmpty(); }
    public void reset() { pending.clear(); active = null; }
}
