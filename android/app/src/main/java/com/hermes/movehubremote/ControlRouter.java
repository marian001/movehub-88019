package com.hermes.movehubremote;

/** Pure JVM identity/routing/safety model. No Android or BLE dependency. */
public final class ControlRouter {
    private final State[] states = {new State(), new State()};
    private Hub selected = Hub.PORSCHE;
    private boolean both;
    public State state(Hub hub) { return states[hub.ordinal()]; }
    public Hub[] targets() { return both ? Hub.values() : new Hub[]{selected}; }
    public void select(Hub hub, boolean both) {
        stopAll(); this.selected = hub; this.both = both;
    }
    public void drive(int speed, int steer) {
        for (Hub h : targets()) state(h).drive(speed, steer);
    }
    public void stopAll() { for (State s : states) s.stop(); }
    public static final class State {
        public int speed, steer;
        public boolean ready;
        public long epoch;
        public void stop() { speed = steer = 0; epoch++; }
        public void setReady(boolean ready) { stop(); this.ready = ready; }
        public void drive(int speed, int steer) {
            if (!ready) return;
            this.speed = Math.max(-100, Math.min(100, speed));
            this.steer = Math.max(-70, Math.min(70, steer));
        }
    }

    public enum Hub {
        PORSCHE("Porsche", "80:C4:1B:DD:F9:4C", 5),
        LAMBORGHINI("Lamborghini", "F0:10:A5:1F:5F:F1", 1);
        public final String label, address;
        public final int color;
        Hub(String label, String address, int color) {
            this.label = label; this.address = address; this.color = color;
        }
        public boolean matches(String address) { return this.address.equalsIgnoreCase(address); }
    }
}
