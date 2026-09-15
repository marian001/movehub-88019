package com.hermes.movehubremote;

import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import java.util.ArrayDeque;
import java.util.UUID;

/**
 * BLE + LWP3 driver for the LEGO Technic Move Hub 88019 (set 42176).
 * Java port of the verified Python driver (github.com/marian001/movehub-88019).
 *
 * Critical rules baked in:
 *  - bond (createBond) after connecting, before use
 *  - VM handshake (subscribe -> state -> ARM) before any drive frame
 *  - drive frames as a continuous 20 Hz stream (keepalive)
 *  - ALWAYS end the session with HUB_ACTION_DISCONNECT before BLE disconnect,
 *    otherwise the hub's drive VM refuses all drive frames (ERR 0x05) in the
 *    next session — a state that survives power-cycles.
 */
public class MoveHubGatt {

    private static final String TAG = "MoveHubGatt";

    private static final UUID SVC =
            UUID.fromString("00001623-1212-efde-1623-785feabcd123");
    private static final UUID CHR =
            UUID.fromString("00001624-1212-efde-1623-785feabcd123");
    private static final UUID CCC =
            UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");

    public static final int MOTOR_A = 0x32, MOTOR_B = 0x33, MOTOR_C = 0x34;
    public static final int BRAKE = 0x7F, FLOAT = 0x00;

    public interface Listener {
        void onStatus(String text);      // free-form status line
        void onReady();                  // connected + handshake + calibrated
        void onDisconnected();
        void onVoltage(int mv);
    }

    private final Context ctx;
    private final Listener listener;
    private final Handler main = new Handler(Looper.getMainLooper());

    private BluetoothAdapter adapter;
    private BluetoothGatt gatt;
    private BluetoothGattCharacteristic characteristic;
    private boolean closing = false;
    private boolean closed = true;

    // write queue (one write at a time, NO_RESPONSE)
    private final ArrayDeque<byte[]> queue = new ArrayDeque<>();
    private boolean writeBusy = false;

    // drive state (streamed at 20 Hz)
    private volatile int speed = 0, steer = 0, flags = 0x00;
    private Runnable streamTick;
    private volatile boolean streaming = false;

    // ------------------------------------------------------------------ scan

    private final ScanCallback scanCb = new ScanCallback() {
        @Override
        public void onScanResult(int callbackType, ScanResult result) {
            String name = result.getScanRecord() != null
                    ? result.getScanRecord().getDeviceName() : null;
            boolean byName = name != null && name.contains("Technic Move");
            boolean byMfg = result.getScanRecord() != null
                    && result.getScanRecord().getManufacturerSpecificData(0x0397) != null;
            if (byName || byMfg) {
                stopScan();
                listener.onStatus("found " + (name != null ? name : result.getDevice().getAddress())
                        + " — connecting...");
                connect(result.getDevice());
            }
        }

        @Override
        public void onScanFailed(int errorCode) {
            listener.onStatus("scan failed (" + errorCode + ")");
        }
    };

    private boolean scanning = false;

    public MoveHubGatt(Context ctx, Listener listener) {
        this.ctx = ctx.getApplicationContext();
        this.listener = listener;
        BluetoothManager bm = (BluetoothManager) this.ctx.getSystemService(Context.BLUETOOTH_SERVICE);
        adapter = bm != null ? bm.getAdapter() : null;
    }

    /** Start scanning for the hub and auto-connect. */
    @SuppressLint("MissingPermission")
    public void scanAndConnect() {
        if (adapter == null) {
            listener.onStatus("no Bluetooth adapter");
            return;
        }
        if (!adapter.isEnabled()) {
            listener.onStatus("Bluetooth is off — enable it first");
            return;
        }
        closing = false;
        ScanSettings settings = new ScanSettings.Builder()
                .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
                .build();
        scanning = true;
        // unfiltered scan; we match by name or LEGO manufacturer id (919) in the callback
        adapter.getBluetoothLeScanner().startScan(null, settings, scanCb);
        listener.onStatus("scanning — press the green button on the hub...");
    }

    @SuppressLint("MissingPermission")
    private void stopScan() {
        if (scanning && adapter != null && adapter.getBluetoothLeScanner() != null) {
            try {
                adapter.getBluetoothLeScanner().stopScan(scanCb);
            } catch (Exception ignored) {
            }
        }
        scanning = false;
    }

    // --------------------------------------------------------------- connect

    @SuppressLint("MissingPermission")
    private void connect(BluetoothDevice device) {
        closed = false;
        gatt = device.connectGatt(ctx, false, gattCb, BluetoothDevice.TRANSPORT_LE);
    }

    private final BroadcastReceiver bondReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            if (BluetoothDevice.ACTION_BOND_STATE_CHANGED.equals(intent.getAction())) {
                int state = intent.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, -1);
                int prev = intent.getIntExtra(BluetoothDevice.EXTRA_PREVIOUS_BOND_STATE, -1);
                BluetoothDevice dev = intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE);
                if (dev == null || gatt == null || dev.getAddress() == null
                        || !dev.getAddress().equals(gatt.getDevice().getAddress())) return;
                if (state == BluetoothDevice.BOND_BONDED) {
                    listener.onStatus("bonded — subscribing...");
                    main.post(() -> enableNotifications());
                } else if (state == BluetoothDevice.BOND_NONE && prev != BluetoothDevice.BOND_NONE) {
                    listener.onStatus("bonding failed — trying to continue anyway...");
                    main.post(() -> enableNotifications());
                }
            }
        }
    };

    private final BluetoothGattCallback gattCb = new BluetoothGattCallback() {

        @Override
        public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
            main.post(() -> {
                if (newState == BluetoothProfile.STATE_CONNECTED) {
                    listener.onStatus("connected — discovering services...");
                    gatt.discoverServices();
                } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                    if (closing) {
                        closeGattNow();
                    } else {
                        cleanup();
                        listener.onStatus("disconnected");
                        listener.onDisconnected();
                    }
                }
            });
        }

        @Override
        public void onServicesDiscovered(BluetoothGatt g, int status) {
            main.post(() -> {
                if (status != BluetoothGatt.GATT_SUCCESS || gatt == null) {
                    listener.onStatus("service discovery failed");
                    return;
                }
                characteristic = gatt.getService(SVC) != null
                        ? gatt.getService(SVC).getCharacteristic(CHR) : null;
                if (characteristic == null) {
                    listener.onStatus("LWP3 service not found — wrong device?");
                    safeClose();
                    return;
                }
                int bond = gatt.getDevice().getBondState();
                if (bond != BluetoothDevice.BOND_BONDED) {
                    listener.onStatus("bonding (Just Works)...");
                    IntentFilter f = new IntentFilter(BluetoothDevice.ACTION_BOND_STATE_CHANGED);
                    if (Build.VERSION.SDK_INT >= 33) {
                        ctx.registerReceiver(bondReceiver, f, Context.RECEIVER_EXPORTED);
                    } else {
                        ctx.registerReceiver(bondReceiver, f);
                    }
                    boolean ok = gatt.getDevice().createBond();
                    if (!ok) {
                        listener.onStatus("createBond() rejected — continuing unencrypted...");
                        enableNotifications();
                    }
                    // bondReceiver path continues on success
                    main.postDelayed(() -> {
                        try {
                            ctx.unregisterReceiver(bondReceiver);
                        } catch (Exception ignored) {
                        }
                    }, 30000);
                } else {
                    listener.onStatus("already bonded — subscribing...");
                    enableNotifications();
                }
            });
        }

        @Override
        public void onDescriptorWrite(BluetoothGatt g, BluetoothGattDescriptor d, int status) {
            main.post(() -> {
                if (status == BluetoothGatt.GATT_SUCCESS) {
                    listener.onStatus("notifications on — waiting for port map...");
                    // let the port map arrive, then VM handshake
                    main.postDelayed(() -> handshake(), 1200);
                } else {
                    listener.onStatus("descriptor write failed (" + status + ")");
                }
            });
        }

        @Override
        public void onCharacteristicWrite(BluetoothGatt g,
                                           BluetoothGattCharacteristic c, int status) {
            main.post(() -> {
                writeBusy = false;
                pump();
            });
        }

        @Override
        public void onCharacteristicChanged(BluetoothGatt g,
                                            BluetoothGattCharacteristic c) {
            final byte[] data = c.getValue();
            main.post(() -> handleNotify(data));
        }

        @Override
        public void onCharacteristicChanged(BluetoothGatt g,
                                            BluetoothGattCharacteristic c, byte[] data) {
            // API 33+ overload — value arrives as a parameter
            final byte[] d = data;
            main.post(() -> handleNotify(d));
        }
    };

    @SuppressLint("MissingPermission")
    private void enableNotifications() {
        if (gatt == null || characteristic == null) return;
        gatt.setCharacteristicNotification(characteristic, true);
        BluetoothGattDescriptor d = characteristic.getDescriptor(CCC);
        if (d != null) {
            d.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
            gatt.writeDescriptor(d);
        }
    }

    // ------------------------------------------------------------- handshake

    private static final byte[] VM_SUBSCRIBE =
            {0x0A, 0x00, 0x41, 0x36, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01};
    private static final byte[] VM_STATE_REQ =
            {0x05, 0x00, 0x21, 0x36, 0x00};
    private static final byte[] VM_ARM =
            {(byte) 0x09, 0x00, (byte) 0x81, 0x36, 0x11, 0x51, 0x00, 0x04, 0x01};
    private static final byte[] HUB_ACTION_DISCONNECT =
            {0x04, 0x00, 0x02, 0x02};

    private void handshake() {
        if (gatt == null) return;
        listener.onStatus("VM handshake...");
        enqueue(VM_SUBSCRIBE);
        main.postDelayed(() -> { if (gatt != null) enqueue(VM_STATE_REQ); }, 400);
        main.postDelayed(() -> { if (gatt != null) enqueue(VM_ARM); }, 1000);
        // steering calibration (the steering MOVES — wheels must be free)
        main.postDelayed(() -> { if (gatt != null) calibrateInternal(); }, 1600);
    }

    /** Steering calibration — sweeps the steering physically. */
    private void calibrateInternal() {
        listener.onStatus("calibrating steering (it sweeps!)...");
        enqueue(driveFrame(0, 0, 0x10));   // INIT
        main.postDelayed(() -> { if (gatt != null) enqueue(driveFrame(0, 0, 0x08)); }, 1600);  // CALIBRATE
        main.postDelayed(() -> {
            if (gatt != null) {
                startStream();
                listener.onStatus("ready");
                listener.onReady();
            }
        }, 4200);
    }

    /** Public re-calibration (CAL button). */
    public void calibrate() {
        if (gatt == null) return;
        calibrateInternal();
    }

    // ----------------------------------------------------------- drive stream

    private static byte[] driveFrame(int speed, int steer, int flags) {
        return new byte[]{0x0D, 0x00, (byte) 0x81, 0x36, 0x11, 0x51, 0x00, 0x03, 0x00,
                (byte) (speed & 0xFF), (byte) (steer & 0xFF), (byte) (flags & 0xFF), 0x00};
    }

    private void startStream() {
        if (streaming) return;
        streaming = true;
        streamTick = new Runnable() {
            @Override
            public void run() {
                if (!streaming) return;
                if (gatt != null && !writeBusy && queue.isEmpty()) {
                    enqueue(driveFrame(speed, steer, flags));
                }
                main.postDelayed(this, 50);   // 20 Hz
            }
        };
        main.post(streamTick);
    }

    private void stopStream() {
        streaming = false;
        if (streamTick != null) main.removeCallbacks(streamTick);
    }

    /**
     * VM drive. speed -100..100, steer -70..70.
     * lights=false -> flag 0x04 (lights off), brake -> flag 0x01.
     */
    public void setDrive(int speed, int steer, boolean lights, boolean brake) {
        if (Math.abs(speed) > 100 || Math.abs(steer) > 70) return;
        int f = (lights ? 0x00 : 0x04) | (brake ? 0x01 : 0x00);
        this.speed = speed;
        this.steer = steer;
        this.flags = f;
    }

    public void setLights(boolean on) {
        flags = (flags & ~0x04) | (on ? 0x00 : 0x04);
    }

    /** Direct motor power, -100..100; BRAKE (0x7F) or FLOAT (0) to stop. */
    public void motorPower(int motor, int power) {
        if (gatt == null) return;
        enqueue(new byte[]{0x08, 0x00, (byte) 0x81, (byte) motor, 0x00, 0x51,
                0x00, (byte) (power & 0xFF)});
    }

    private static final byte[] LED_COLORS = {
            0x00, /* off */ 0x01 /* pink */, 0x02 /* purple */, 0x03 /* blue */,
            0x04 /* lightblue */, 0x05 /* cyan */, 0x06 /* green */, 0x07 /* yellow */,
            0x08 /* orange */, 0x09 /* red */, 0x0A /* white */
    };

    /** Status LED: 0=off, 3=blue, 5=cyan, 6=green, 8=orange, 9=red, 10=white... */
    public void setLed(int colorIdx) {
        if (gatt == null || colorIdx < 0 || colorIdx >= LED_COLORS.length) return;
        enqueue(new byte[]{0x08, 0x00, (byte) 0x81, 0x3F, 0x11, 0x51, 0x00,
                LED_COLORS[colorIdx]});
    }

    // --------------------------------------------------------------- voltage

    /** One-shot battery read. Result via listener.onVoltage(mV). */
    public void readVoltage() {
        if (gatt == null) return;
        // PortInputFormatSetup port 0x3C mode 0 delta 100 (0x64) notify on
        enqueue(new byte[]{0x0A, 0x00, 0x41, 0x3C, 0x00, 0x64, 0x00, 0x00, 0x00, 0x01});
        // auto-off after 2 s if nothing arrives
        main.postDelayed(() -> {
            if (gatt != null) enqueue(new byte[]
                    {0x0A, 0x00, 0x41, 0x3C, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00});
        }, 2000);
    }

    private void handleNotify(byte[] b) {
        if (b == null || b.length < 6) return;
        if (b[2] == 0x45 && b[3] == 0x3C && b.length >= 6) {
            int mv = ((b[4] & 0xFF) | ((b[5] & 0xFF) << 8));
            listener.onVoltage(mv);
            // disable notifications again (one-shot)
            enqueue(new byte[]{0x0A, 0x00, 0x41, 0x3C, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00});
        }
        // 0x45 0x36 = VM state/encoder streams; 05 00 05 81 xx = errors — ignored for now
    }

    // ------------------------------------------------------------ write queue

    @SuppressLint("MissingPermission")
    private void enqueue(byte[] frame) {
        queue.add(frame);
        pump();
    }

    @SuppressLint("MissingPermission")
    private void pump() {
        if (writeBusy || queue.isEmpty() || gatt == null || characteristic == null) return;
        writeBusy = true;
        byte[] f = queue.poll();
        characteristic.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE);
        characteristic.setValue(f);
        if (!gatt.writeCharacteristic(characteristic)) {
            writeBusy = false;
        }
    }

    // ------------------------------------------------------------------ close

    /**
     * ALWAYS call before dropping the connection. Sends lights-off +
     * HUB_ACTION_DISCONNECT, then disconnects. Prevents the ERR 0x05
     * session-lock on the next connection (a state that survives power
     * cycles of the hub).
     */
    @SuppressLint("MissingPermission")
    public void safeClose() {
        stopScan();
        stopStream();
        queue.clear();
        writeBusy = false;
        if (gatt == null) {
            closed = true;
            return;
        }
        closing = true;
        if (characteristic != null) {
            // lights off, then HUB_ACTION_DISCONNECT, then BLE disconnect
            characteristic.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE);
            characteristic.setValue(driveFrame(0, 0, 0x04));
            gatt.writeCharacteristic(characteristic);
        }
        main.postDelayed(() -> {
            if (gatt != null && characteristic != null) {
                characteristic.setValue(HUB_ACTION_DISCONNECT);
                gatt.writeCharacteristic(characteristic);
            }
        }, 300);
        main.postDelayed(() -> {
            if (gatt != null) {
                try {
                    gatt.disconnect();
                } catch (Exception ignored) {
                }
            }
        }, 900);
        main.postDelayed(this::closeGattNow, 2500);
    }

    @SuppressLint("MissingPermission")
    private void closeGattNow() {
        if (gatt != null) {
            try {
                gatt.close();
            } catch (Exception ignored) {
            }
            gatt = null;
        }
        characteristic = null;
        closed = true;
        try {
            ctx.unregisterReceiver(bondReceiver);
        } catch (Exception ignored) {
        }
    }

    private void cleanup() {
        stopStream();
        queue.clear();
        writeBusy = false;
    }

    /** Pause the 20 Hz drive stream (e.g. while in ROBOT mode). */
    public void pauseStream() {
        stopStream();
    }

    /** Resume the 20 Hz drive stream with current drive state. */
    public void resumeStream() {
        startStream();
    }

    public boolean isClosed() {
        return closed;
    }
}
