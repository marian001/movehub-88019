package com.hermes.movehubremote;

import android.annotation.SuppressLint;
import android.bluetooth.*;
import android.bluetooth.le.*;
import android.content.*;
import android.os.*;
import android.util.Log;
import java.util.Arrays;
import java.util.Collections;
import java.util.UUID;

/** One strictly addressed session, queue, callbacks and 20 Hz stream per hub.
 * All mutable state is confined to the main looper. Never share a GATT queue.
 */
@SuppressLint("MissingPermission")
public final class MoveHubGatt {
    private static final String TAG = "MoveHubGatt";
    private static final UUID SVC = UUID.fromString("00001623-1212-efde-1623-785feabcd123");
    private static final UUID CHR = UUID.fromString("00001624-1212-efde-1623-785feabcd123");
    private static final UUID CCC = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");
    public static final int MOTOR_A = 0x32, MOTOR_B = 0x33, MOTOR_C = 0x34, BRAKE = 127;
    public interface Listener {
        void onStatus(String text);
        void onReady();
        void onDisconnected();
        void onVoltage(int mv);
    }
    private final Context ctx;
    private final ControlRouter.Hub hub;
    private final ControlRouter.State state;
    private final Listener listener;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final SessionQueue queue = new SessionQueue();
    private final BluetoothAdapter adapter;
    private BluetoothGatt gatt;
    private BluetoothGattCharacteristic characteristic;
    private ScanCallback scanner;
    private boolean closed = true, closing, scanning, streaming, descriptorBusy;
    private boolean receiverRegistered, subscribed, vmSeen, vmStopped, armAck, armSent, voltagePending;
    private boolean lights = true, robot, endWritten, tickDead, retryOnce;
    private int generation, phase, writeSerial;
    private Runnable writeTimeout;
    private static String hex(byte[] f) { StringBuilder s = new StringBuilder(); for (byte b : f) s.append(String.format("%02x", b)); return s.toString(); }

    public MoveHubGatt(Context context, ControlRouter.Hub hub, ControlRouter.State state, Listener listener) {
        ctx = context.getApplicationContext(); this.hub = hub; this.state = state; this.listener = listener;
        BluetoothManager manager = (BluetoothManager) ctx.getSystemService(Context.BLUETOOTH_SERVICE);
        adapter = manager == null ? null : manager.getAdapter();
    }
    private void status(String text) { Log.i(TAG, hub.label + ": " + text); listener.onStatus(text); }
    private void later(long delay, Runnable action) {
        final int session = generation, step = phase;
        main.postDelayed(() -> {
            if (!closed && !closing && session == generation && step == phase) action.run();
        }, delay);
    }
    public boolean isClosed() { return closed; }
    public boolean isReady() { return state.ready && !closing; }
    public boolean isClosing() { return closing; }

    public void scanAndConnect() {
        if (!closed) return;
        state.setReady(false);
        try {
            if (adapter == null || !adapter.isEnabled() || adapter.getBluetoothLeScanner() == null) {
                status("Bluetooth je vypnutý alebo nedostupný — zapnite ho v nastaveniach.");
                listener.onDisconnected(); return;
            }
            generation++; phase++; closed = false; closing = false; scanning = true;
            queue.reset(); robot = false; streaming = false; lights = true; retryOnce = false;
            subscribed = vmSeen = vmStopped = armAck = armSent = endWritten = false;
            final int session = generation;
            scanner = new ScanCallback() {
                @Override public void onScanResult(int type, ScanResult result) {
                    main.post(() -> {
                        if (session != generation || !scanning || closing || closed) return;
                        if (!hub.matches(result.getDevice().getAddress())) return;
                        stopScan();
                        status("Nájdený presný hub — pripájam " + hub.address);
                        try {
                            gatt = result.getDevice().connectGatt(ctx, false, callbacks, BluetoothDevice.TRANSPORT_LE);
                            if (gatt == null) fail("Android odmietol spojenie.");
                            else {
                                // Root cause 2026-09-16: the 45 s scan deadline kept running
                                // through pair+handshake+calibration; a hub found late in the
                                // window (green button pressed by hand) was killed mid-session
                                // ~90 ms before Ready. Obsolete it and restart the clock from
                                // the moment GATT is up: 30 s covers pair+CCC+handshake (~9 s).
                                phase++;
                                later(30000, () -> { if (!state.ready) fail("Čas pripojenia/párovania vypršal. Skúste znova; potvrďte párovanie v Androide."); });
                            }
                        } catch (RuntimeException e) { fail("Pripojenie: " + e.getMessage()); }
                    });
                }
                @Override public void onScanFailed(int code) {
                    main.post(() -> { if (session == generation && scanning) fail("Skenovanie zlyhalo: " + code); });
                }
            };
            adapter.getBluetoothLeScanner().startScan(
                Collections.singletonList(new ScanFilter.Builder().setDeviceAddress(hub.address).build()),
                new ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(), scanner);
            status("Hľadám " + hub.address + " — stlačte zelené tlačidlo tohto hubu.");
            later(45000, () -> { if (!state.ready) fail("Čas pripojenia/párovania vypršal. Skúste znova; potvrďte párovanie v Androide."); });
        } catch (RuntimeException e) { fail("Bluetooth oprávnenia / skenovanie: " + e.getMessage()); }
    }
    private void stopScan() {
        if (scanning && adapter != null && scanner != null) {
            try { if (adapter.getBluetoothLeScanner() != null) adapter.getBluetoothLeScanner().stopScan(scanner); }
            catch (RuntimeException ignored) { }
        }
        scanning = false; scanner = null;
    }
    private final BroadcastReceiver bondReceiver = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            BluetoothDevice device = intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE);
            if (device == null || !hub.matches(device.getAddress()) || closed || closing || gatt == null) return;
            int bond = intent.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, -1);
            if (bond == BluetoothDevice.BOND_BONDED) enableNotifications();
            else if (bond == BluetoothDevice.BOND_NONE) fail("Párovanie zlyhalo. Potvrďte systémovú výzvu a skúste znova.");
        }
    };
    private final BluetoothGattCallback callbacks = new BluetoothGattCallback() {
        @Override public void onConnectionStateChange(BluetoothGatt source, int result, int connectionState) {
            main.post(() -> {
                if (source != gatt || closed) return;
                if (connectionState == BluetoothProfile.STATE_DISCONNECTED) {
                    status(closing ? "Odpojený." : "Spojenie sa prerušilo — ovládanie vynulované.");
                    closeNow(); return;
                }
                if (closing) return;
                if (result != BluetoothGatt.GATT_SUCCESS) { fail("GATT spojenie: " + result); return; }
                if (connectionState == BluetoothProfile.STATE_CONNECTED) {
                    status("Pripojený — zisťujem služby...");
                    try { if (!source.discoverServices()) fail("Zisťovanie služieb odmietnuté."); }
                    catch (RuntimeException e) { fail("Služby: " + e.getMessage()); }
                }
            });
        }
        @Override public void onServicesDiscovered(BluetoothGatt source, int result) {
            main.post(() -> {
                if (source != gatt || closed || closing) return;
                if (result != BluetoothGatt.GATT_SUCCESS) { fail("Zisťovanie služieb zlyhalo: " + result); return; }
                BluetoothGattService service = source.getService(SVC);
                characteristic = service == null ? null : service.getCharacteristic(CHR);
                if (characteristic == null) { fail("Chýba služba LWP3."); return; }
                try {
                    if (source.getDevice().getBondState() == BluetoothDevice.BOND_BONDED) { enableNotifications(); return; }
                    status("Párovanie — potvrďte systémovú výzvu Androidu.");
                    IntentFilter filter = new IntentFilter(BluetoothDevice.ACTION_BOND_STATE_CHANGED);
                    if (Build.VERSION.SDK_INT >= 33) ctx.registerReceiver(bondReceiver, filter, Context.RECEIVER_EXPORTED);
                    else ctx.registerReceiver(bondReceiver, filter);
                    receiverRegistered = true;
                    if (source.getDevice().getBondState() != BluetoothDevice.BOND_BONDING && !source.getDevice().createBond())
                        fail("Android odmietol párovanie; nepokračujem bez šifrovania.");
                } catch (RuntimeException e) { fail("Párovanie: " + e.getMessage()); }
            });
        }
        @Override public void onDescriptorWrite(BluetoothGatt source, BluetoothGattDescriptor descriptor, int result) {
            main.post(() -> {
                if (source != gatt || closed || !descriptorBusy) return;
                descriptorBusy = false;
                if (closing) { pump(); return; }
                if (result != BluetoothGatt.GATT_SUCCESS) { fail("Notifikácie zlyhali: " + result); return; }
                status("Notifikácie zapnuté — čakám na hub...");
                later(1200, MoveHubGatt.this::handshake);
            });
        }
        @Override public void onCharacteristicWrite(BluetoothGatt source, BluetoothGattCharacteristic c, int result) {
            main.post(() -> {
                if (source != gatt || closed) return;
                Log.i(TAG, hub.label + ": [DBG] write result=" + result + " serial=" + writeSerial);
                if (writeTimeout != null) main.removeCallbacks(writeTimeout);
                byte[] sent = queue.complete();
                if (sent == null) return;
                if (result != BluetoothGatt.GATT_SUCCESS) {
                    if (!closing) { fail("GATT zápis zlyhal: " + result); return; }
                    status("Chyba pri bezpečnom zatváraní: " + result);
                }
                if (Arrays.equals(sent, HubProtocol.END)) {
                    endWritten = result == BluetoothGatt.GATT_SUCCESS;
                    final BluetoothGatt ending = gatt;
                    main.postDelayed(() -> { if (ending == gatt && closing) disconnectTransport(); }, 200);
                } else pump();
            });
        }
        @Override public void onCharacteristicChanged(BluetoothGatt source, BluetoothGattCharacteristic c) {
            byte[] data = c.getValue();
            if (data != null) notifyOnMain(source, data.clone());
        }
        @Override public void onCharacteristicChanged(BluetoothGatt source, BluetoothGattCharacteristic c, byte[] data) {
            notifyOnMain(source, data.clone());
        }
    };
    private void notifyOnMain(BluetoothGatt source, byte[] data) {
        main.post(() -> { if (source == gatt && !closed && !closing) handleNotify(data); });
    }
    private void enableNotifications() {
        if (subscribed || closed || closing || gatt == null || characteristic == null) return;
        try {
            if (gatt.getDevice().getBondState() != BluetoothDevice.BOND_BONDED) { fail("Spojenie nie je spárované."); return; }
            BluetoothGattDescriptor descriptor = characteristic.getDescriptor(CCC);
            if (descriptor == null || !gatt.setCharacteristicNotification(characteristic, true)) { fail("Chýba CCC / notifikácie odmietnuté."); return; }
            subscribed = true; descriptorBusy = true;
            descriptor.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
            if (!gatt.writeDescriptor(descriptor)) { descriptorBusy = false; fail("CCC zápis odmietnutý."); }
            later(6000, () -> { if (descriptorBusy) fail("CCC zápis bez odpovede."); });
        } catch (RuntimeException e) { descriptorBusy = false; fail("Notifikácie: " + e.getMessage()); }
    }
    private void handshake() {
        status("Inicializujem VM...");
        enqueue(HubProtocol.SUBSCRIBE);
        later(400, () -> enqueue(HubProtocol.STATE));
        later(1100, () -> {
            if (!vmSeen) { fail("Hub neposlal stav VM — nepovolím pohyb."); return; }
            armSent = true;
            if (vmStopped) {
                // Root cause 2026-09-16 (Lambo motored dead): STOP+START+ARM sent
                // back-to-back were all ACKed but the VM never left STOPPED (0x02)
                // — "ACKs and does nothing". probe_v11's verified restart timing
                // (STOP → 0.6 s → START → 1.0 s → ARM) actually restarts the VM.
                enqueue(HubProtocol.VM_STOP);
                later(600, () -> enqueue(HubProtocol.VM_START));
                later(1600, () -> {
                    enqueue(HubProtocol.ARM);
                    later(700, () -> {
                        if (!armAck) { fail("VM sa nespustil — skúste znova pripojiť."); return; }
                        calibrateInternal();
                    });
                });
            } else {
                enqueue(HubProtocol.ARM);
                later(700, () -> {
                    if (!armAck) { fail("VM ARM bez potvrdenia — nepovolím kalibráciu."); return; }
                    calibrateInternal();
                });
            }
        });
    }
    private void calibrateInternal() {
        stopStream(); state.setReady(false); queue.clearPending(); robot = false;
        status("Kalibrácia: USB-C odpojené, kolesá vo vzduchu! Riadenie sa hýbe.");
        enqueue(HubProtocol.drive(0, 0, 0x10));
        later(1600, () -> enqueue(HubProtocol.drive(0, 0, 0x08)));
        later(4200, () -> {
            state.setReady(true); startStream(); setLed(hub.color);
            status("Pripravený — držte ovládač; uvoľnenie zastaví."); listener.onReady();
        });
    }
    public void calibrate() {
        if (!isReady()) return;
        emergencyStop(); phase++; calibrateInternal();
    }
    private final Runnable tick = new Runnable() {
        @Override public void run() {
            if (!streaming || !isReady() || robot) return;
            if (queue.idle() && !descriptorBusy) {
                enqueue(HubProtocol.drive(state.speed, state.steer, lights ? 0 : 4));
                if (++tickCount % 10 == 1) Log.i(TAG, hub.label + ": stream tick " + tickCount + " speed=" + state.speed + " steer=" + state.steer + " q=" + queue.idle());
                if (tickCount > 5 && state.speed == 0 && state.steer == 0) Log.i(TAG, hub.label + ": [DBG] zero-speed stream still running (tick " + tickCount + ")");
            }
            main.postDelayed(this, 50);
        }
    };
    private int tickCount;
    private void startStream() {
        if (!isReady() || robot || streaming) return;
        streaming = true; main.post(tick);
    }
    private void stopStream() { streaming = false; main.removeCallbacks(tick); }
    public void setDrive(int speed, int steer, boolean lights, boolean brake) {
        if (!isReady() || robot) return;
        Log.i(TAG, hub.label + ": [DBG] setDrive speed=" + speed + " steer=" + steer + " brake=" + brake + " robot=" + robot);
        this.lights = lights;
        if (brake) { emergencyStop(); return; }
        state.drive(speed, steer); startStream();
    }
    public void setLights(boolean on) {
        lights = on;
        // Lights are reachable ONLY through VM drive flags (port 0x35 rejects every
        // write). AUTO applies them via the 20 Hz stream; ROBOT has no stream, so
        // push a one-shot flags frame. It floats drive motors / centers the servo —
        // callers re-apply direct motor power right after (MainActivity Panel).
        if (!closed && !closing && robot && state.ready) enqueue(HubProtocol.drive(0, 0, on ? 0 : 4));
    }
    public void setRobot(boolean enabled) {
        if (!isReady()) return;
        emergencyStop(); robot = enabled;
        if (robot) stopStream(); else { state.stop(); startStream(); }
    }
    public void motorPower(int motor, int power) {
        if (!isReady() || !robot) return;
        enqueue(HubProtocol.motor(motor, power));
    }
    public void setLed(int color) { if (!closed && !closing) enqueue(HubProtocol.led(color)); }
    public void readVoltage() {
        if (!isReady() || voltagePending) return;
        voltagePending = true; enqueue(HubProtocol.voltage(true));
        later(2000, () -> {
            if (voltagePending) { voltagePending = false; enqueue(HubProtocol.voltage(false)); status("Napätie: bez odpovede hubu."); }
        });
    }
    private void handleNotify(byte[] data) {
        if (data.length < 5) return;
        Log.i(TAG, hub.label + ": [DBG] notify " + hex(data) + " (vmSeen=" + vmSeen + " armAck=" + armAck + ")");
        if (data[2] == 0x05) { fail("Hub hlási LWP3 chybu " + (data[4] & 255)); return; }
        Integer feedback = HubProtocol.parsePortFeedback(data);
        if (feedback != null && armSent && feedback == 1) armAck = true;
        Integer vm = HubProtocol.parseVmState(data);
        if (vm != null) {
            vmSeen = true;
            vmStopped = HubProtocol.VM_STOPPED.equals(vm);
        }
        if (data[2] == 0x45 && data[3] == 0x3c && data.length >= 6 && voltagePending) {
            voltagePending = false;
            listener.onVoltage((data[4] & 255) | ((data[5] & 255) << 8));
            enqueue(HubProtocol.voltage(false));
        }
    }
    private void enqueue(byte[] frame) {
        if (gatt == null || characteristic == null || closed) return;
        queue.add(frame); pump();
    }
    private void pump() {
        if (closed || gatt == null || characteristic == null || descriptorBusy) return;
        byte[] frame = queue.take();
        if (frame == null) return;
        int serial = ++writeSerial;
        final BluetoothGatt source = gatt;
        try {
            characteristic.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE);
            characteristic.setValue(frame);
            if (!gatt.writeCharacteristic(characteristic)) {
                queue.complete();
                if (!closing) fail("Android odmietol GATT zápis.");
                else main.postDelayed(this::pump, 50);
                return;
            }
        } catch (RuntimeException e) {
            queue.complete(); if (!closing) fail("GATT zápis: " + e.getMessage()); return;
        }
        writeTimeout = () -> {
            if (source != gatt || serial != writeSerial || closed) return;
            // Never pretend an unacknowledged write completed and send another motion frame.
            Log.i(TAG, hub.label + ": [DBG] write timeout serial=" + serial + " frame=" + hex(frame));
            if (!closing) fail("GATT zápis bez potvrdenia — bezpečné zatvorenie.");
        };
        main.postDelayed(writeTimeout, 2000);
    }
    /** Clears every pending nonzero command without overlapping the active GATT write. */
    public void emergencyStop() {
        state.stop();
        if (closed || closing) return;
        if (!state.ready) { safeClose(); return; } // abort scan/handshake/calibration too
        queue.clearPending(); voltagePending = false;
        enqueue(HubProtocol.drive(0, 0, (lights ? 0 : 4) | 1));
        for (int port = MOTOR_A; port <= MOTOR_C; port++) enqueue(HubProtocol.motor(port, BRAKE));
        enqueue(HubProtocol.voltage(false));
    }
    private void fail(String reason) {
        Log.i(TAG, hub.label + ": [DBG] fail: " + reason + " (retryOnce=" + retryOnce + ")");
        status(reason);
        if (!retryOnce && (reason.startsWith("CCC") || reason.startsWith("Notifikácie"))) {
            retryOnce = true;   // transient Android stack state — one silent reconnect (verified 2026-09-16)
            closeNow();
            status("Opakujem spojenie (stabilizácia Bluetooth)...");
            main.postDelayed(this::scanAndConnect, 1200);
            return;
        }
        safeClose();
    }
    /** Normal close serializes stop, all brakes and mandatory END before disconnect.
     * Link loss / broken Android callbacks cannot guarantee physical delivery.
     */
    public void safeClose() {
        if (closing) return;
        stopScan(); stopStream(); state.setReady(false); phase++; voltagePending = false;
        if (closed) return;
        closing = true; queue.clearPending();
        if (gatt == null) { closeNow(); return; }
        if (characteristic != null) {
            enqueue(HubProtocol.drive(0, 0, 5));
            for (int port = MOTOR_A; port <= MOTOR_C; port++) enqueue(HubProtocol.motor(port, BRAKE));
            enqueue(HubProtocol.END);
        }
        final BluetoothGatt ending = gatt;
        main.postDelayed(() -> {
            if (gatt != ending || closed) return;
            if (!endWritten) {
                status("Ukončenie bez potvrdenia — skúšam núdzový END; skontrolujte hub.");
                // Last-resort END only, never motion; stack may reject a stuck write.
                try {
                    if (characteristic != null) {
                        characteristic.setValue(HubProtocol.END);
                        characteristic.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE);
                        ending.writeCharacteristic(characteristic);
                    }
                } catch (RuntimeException ignored) { }
            }
            main.postDelayed(() -> { if (ending == gatt) disconnectTransport(); }, 300);
        }, 3500);
        main.postDelayed(() -> { if (ending == gatt) closeNow(); }, 5500);
    }
    private void disconnectTransport() {
        if (gatt == null) return;
        try { gatt.disconnect(); } catch (RuntimeException e) { closeNow(); }
    }
    private void closeNow() {
        stopScan(); stopStream(); generation++; phase++; state.setReady(false);
        if (writeTimeout != null) main.removeCallbacks(writeTimeout);
        if (gatt != null) { try { gatt.close(); } catch (RuntimeException ignored) { } }
        gatt = null; characteristic = null; descriptorBusy = false; queue.reset();
        if (receiverRegistered) { try { ctx.unregisterReceiver(bondReceiver); } catch (RuntimeException ignored) { } }
        receiverRegistered = false; closing = false; closed = true;
        listener.onDisconnected();
    }
}
