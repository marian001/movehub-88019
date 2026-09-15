package com.hermes.movehubremote;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.widget.Button;
import android.widget.SeekBar;
import android.widget.TextView;
import android.widget.Toast;

public class MainActivity extends Activity implements MoveHubGatt.Listener {

    private MoveHubGatt hub;
    private TextView status, speedVal, steerVal;
    private Button connect, voltage, calibrate, mode, lights, brake;
    private View autoPanel, robotPanel;
    private boolean lightsOn = true;
    private boolean autoMode = true;
    private int speed = 0, steer = 0;

    private static final int REQ_PERMS = 1;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        hub = new MoveHubGatt(this, this);

        status = findViewById(R.id.status);
        speedVal = findViewById(R.id.speedVal);
        steerVal = findViewById(R.id.steerVal);
        connect = findViewById(R.id.connect);
        voltage = findViewById(R.id.voltage);
        calibrate = findViewById(R.id.calibrate);
        mode = findViewById(R.id.mode);
        lights = findViewById(R.id.lights);
        brake = findViewById(R.id.brake);
        autoPanel = findViewById(R.id.autoPanel);
        robotPanel = findViewById(R.id.robotPanel);

        connect.setOnClickListener(v -> connectClicked());
        voltage.setOnClickListener(v -> {
            if (hub != null) hub.readVoltage();
        });
        calibrate.setOnClickListener(v -> {
            if (hub != null) {
                Toast.makeText(this, "calibrating — steering will sweep!", Toast.LENGTH_SHORT).show();
                hub.calibrate();
            }
        });
        mode.setOnClickListener(v -> toggleMode());

        findViewById(R.id.fwd).setOnClickListener(v -> {
            speed = clamp(speed + 10, -100, 100);
            applyDrive();
        });
        findViewById(R.id.back).setOnClickListener(v -> {
            speed = clamp(speed - 10, -100, 100);
            applyDrive();
        });
        findViewById(R.id.left).setOnClickListener(v -> {
            steer = clamp(steer - 10, -70, 70);
            applyDrive();
        });
        findViewById(R.id.right).setOnClickListener(v -> {
            steer = clamp(steer + 10, -70, 70);
            applyDrive();
        });
        findViewById(R.id.stop).setOnClickListener(v -> {
            speed = 0;
            steer = 0;
            applyDrive();
        });
        lights.setOnClickListener(v -> {
            lightsOn = !lightsOn;
            lights.setText(lightsOn ? "LIGHTS ON" : "LIGHTS OFF");
            if (hub != null) hub.setLights(lightsOn);
        });
        brake.setOnClickListener(v -> {
            if (hub != null) {
                hub.setDrive(speed, steer, lightsOn, true);
                status.postDelayed(() -> {
                    if (hub != null) hub.setDrive(speed, steer, lightsOn, false);
                }, 400);
            }
        });

        findViewById(R.id.stopAll).setOnClickListener(v -> {
            if (hub != null) {
                hub.motorPower(MoveHubGatt.MOTOR_A, MoveHubGatt.BRAKE);
                hub.motorPower(MoveHubGatt.MOTOR_B, MoveHubGatt.BRAKE);
                hub.motorPower(MoveHubGatt.MOTOR_C, MoveHubGatt.BRAKE);
                ((SeekBar) findViewById(R.id.sbA)).setProgress(100);
                ((SeekBar) findViewById(R.id.sbB)).setProgress(100);
                ((SeekBar) findViewById(R.id.sbC)).setProgress(100);
            }
        });

        wireSeekbar(R.id.sbA, MoveHubGatt.MOTOR_A);
        wireSeekbar(R.id.sbB, MoveHubGatt.MOTOR_B);
        wireSeekbar(R.id.sbC, MoveHubGatt.MOTOR_C);
    }

    private void wireSeekbar(int id, int motor) {
        SeekBar sb = findViewById(id);
        sb.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar s, int progress, boolean fromUser) {
                int power = progress - 100;   // 0..200 -> -100..100
                if (hub != null && fromUser) {
                    hub.motorPower(motor, power);
                }
            }

            @Override
            public void onStartTrackingTouch(SeekBar seekBar) {
            }

            @Override
            public void onStopTrackingTouch(SeekBar seekBar) {
            }
        });
    }

    private void applyDrive() {
        speedVal.setText("speed " + speed);
        steerVal.setText("steer " + steer);
        if (hub != null) hub.setDrive(speed, steer, lightsOn, false);
    }

    private void toggleMode() {
        autoMode = !autoMode;
        autoPanel.setVisibility(autoMode ? View.VISIBLE : View.GONE);
        robotPanel.setVisibility(autoMode ? View.GONE : View.VISIBLE);
        mode.setText(autoMode ? "MODE: AUTO (drive)" : "MODE: ROBOT (motors)");
        if (hub != null) {
            if (autoMode) {
                // back to VM drive: reset + resume the 20 Hz stream
                speed = 0;
                steer = 0;
                applyDrive();
                hub.resumeStream();
                hub.setLed(5);   // cyan
            } else {
                // direct motors: quiet the VM stream, brake all motors
                hub.setDrive(0, 0, lightsOn, false);
                hub.pauseStream();
                hub.motorPower(MoveHubGatt.MOTOR_A, MoveHubGatt.BRAKE);
                hub.motorPower(MoveHubGatt.MOTOR_B, MoveHubGatt.BRAKE);
                hub.motorPower(MoveHubGatt.MOTOR_C, MoveHubGatt.BRAKE);
                ((SeekBar) findViewById(R.id.sbA)).setProgress(100);
                ((SeekBar) findViewById(R.id.sbB)).setProgress(100);
                ((SeekBar) findViewById(R.id.sbC)).setProgress(100);
                hub.setLed(8);   // orange
            }
        }
    }

    // ------------------------------------------------------------- connecting

    private void connectClicked() {
        if (Build.VERSION.SDK_INT >= 31) {
            if (checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN)
                    != PackageManager.PERMISSION_GRANTED
                    || checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT)
                    != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{
                        Manifest.permission.BLUETOOTH_SCAN,
                        Manifest.permission.BLUETOOTH_CONNECT}, REQ_PERMS);
                return;
            }
        } else if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{
                    Manifest.permission.ACCESS_FINE_LOCATION}, REQ_PERMS);
            return;
        }
        hub.scanAndConnect();
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        if (code == REQ_PERMS && results.length > 0) {
            boolean all = true;
            for (int r : results) if (r != PackageManager.PERMISSION_GRANTED) all = false;
            if (all) hub.scanAndConnect();
            else status.setText("Bluetooth permissions denied — allow them in Settings");
        }
    }

    // ----------------------------------------------------------------- hub cb

    @Override
    public void onStatus(String text) {
        runOnUiThread(() -> status.setText(text));
    }

    @Override
    public void onReady() {
        runOnUiThread(() -> {
            connect.setText("DISCONNECT");
            connect.setOnClickListener(v -> {
                hub.safeClose();
                connect.setText("CONNECT");
                connect.setOnClickListener(v2 -> connectClicked());
            });
            Toast.makeText(this, "ready!", Toast.LENGTH_SHORT).show();
        });
    }

    @Override
    public void onDisconnected() {
        runOnUiThread(() -> {
            connect.setText("CONNECT");
            connect.setOnClickListener(v -> connectClicked());
        });
    }

    @Override
    public void onVoltage(int mv) {
        runOnUiThread(() ->
                Toast.makeText(this, "battery: " + mv + " mV", Toast.LENGTH_SHORT).show());
    }

    // ------------------------------------------------------------ lifecycle

    @Override
    protected void onPause() {
        super.onPause();
        // ALWAYS a clean session end — the ERR 0x05 session-lock on the hub
        // survives power cycles, so never let the app background mid-session
        if (hub != null && !hub.isClosed()) {
            hub.safeClose();
        }
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (hub != null) {
            hub.safeClose();
        }
    }

    private static int clamp(int v, int lo, int hi) {
        return Math.max(lo, Math.min(hi, v));
    }
}
