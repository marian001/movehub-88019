package com.hermes.movehubremote;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.*;
import java.util.ArrayList;
import java.util.List;

/** Independent hub cards. Every gesture is hold-to-run and session-epoch gated. */
public class MainActivity extends Activity {
    private final ControlRouter router = new ControlRouter();
    private final MoveHubGatt[] hubs = new MoveHubGatt[2];
    private final Panel[] panels = new Panel[2];
    private final TextView[] statuses = new TextView[2];
    private final Button[] connects = new Button[2];
    private Panel bothPanel;
    private boolean both, foreground;
    private static final int REQ_PERMS = 1;

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        setContentView(R.layout.activity_main);
        LinearLayout cards = findViewById(R.id.cards);
        for (ControlRouter.Hub identity : ControlRouter.Hub.values()) {
            final int index = identity.ordinal();
            hubs[index] = new MoveHubGatt(this, identity, router.state(identity), new MoveHubGatt.Listener() {
                @Override public void onStatus(String text) {
                    statuses[index].setText(text);
                    refreshEnabled();
                }
                @Override public void onReady() {
                    panels[index].reset();
                    bothPanel.reset();
                    Panel active = both ? bothPanel : panels[index];
                    hubs[index].setRobot(active.robot);
                    hubs[index].setLights(active.lights);
                    refreshEnabled();
                }
                @Override public void onDisconnected() {
                    panels[index].reset();
                    if (both) stopPanel(bothPanel);
                    refreshEnabled();
                }
                @Override public void onVoltage(int mv) {
                    statuses[index].setText("Batéria: " + mv + " mV");
                }
            });
            LinearLayout card = column(cards);
            int color = index == 0 ? Color.rgb(0, 112, 130) : Color.rgb(166, 23, 106);
            text(card, identity.label + (index == 0 ? " • TYRKYSOVÝ" : " • RUŽOVÝ"), 23).setTextColor(color);
            text(card, identity.address, 14);
            statuses[index] = text(card, "Odpojený — stlačte zelené tlačidlo hubu.", 16);
            connects[index] = button(card, "PRIPOJIŤ " + identity.label, () -> connectClicked(index));
            LinearLayout tools = row(card);
            button(tools, "BATÉRIA", () -> hubs[index].readVoltage());
            button(tools, "KALIBRÁCIA", () -> confirmCalibration(index));
            panels[index] = new Panel(card, new ControlRouter.Hub[]{identity});
        }
        LinearLayout shared = column(cards);
        CheckBox bothSwitch = new CheckBox(this);
        bothSwitch.setText("BOTH — spoločné ovládanie oboch hubov");
        bothSwitch.setTextSize(19);
        shared.addView(bothSwitch);
        text(shared, "Režim BOTH vyžaduje oba pripravené huby. Pripojenia zostávajú nezávislé.", 15);
        bothPanel = new Panel(shared, ControlRouter.Hub.values());
        bothSwitch.setOnCheckedChangeListener((v, checked) -> {
            stopAll();
            both = checked;
            router.select(ControlRouter.Hub.PORSCHE, checked);
            for (ControlRouter.Hub h : ControlRouter.Hub.values()) {
                Panel p = both ? bothPanel : panels[h.ordinal()];
                hubs[h.ordinal()].setRobot(p.robot);
                hubs[h.ordinal()].setLights(p.lights);
            }
            refreshEnabled();
        });
        Button emergency = findViewById(R.id.emergency);
        emergency.setOnClickListener(v -> stopAll());
        emergency.setOnTouchListener((v, event) -> {
            if (event.getActionMasked() == MotionEvent.ACTION_DOWN) stopAll();
            return false;
        });
        refreshEnabled();
    }

    private final class Panel {
        final ControlRouter.Hub[] targets;
        final List<View> enabledViews = new ArrayList<>();
        final List<SeekBar> sliders = new ArrayList<>();
        final LinearLayout autoControls, robotControls;
        final TextView values;
        final Button mode, light;
        boolean robot, lights = true;
        final int[] motorValue = new int[3];   // last slider values, A/B/C — re-applied after one-shot light frames
        Panel(LinearLayout parent, ControlRouter.Hub[] targets) {
            this.targets = targets;
            LinearLayout options = row(parent);
            mode = button(options, "REŽIM: AUTO", () -> {
                stopPanel(this);
                robot = !robot;
                for (ControlRouter.Hub h : targets) hubs[h.ordinal()].setRobot(robot);
                updateMode();
            });
            light = button(options, "SVETLÁ: ZAP", () -> {
                lights = !lights;
                lightText();
                for (ControlRouter.Hub h : targets) {
                    MoveHubGatt hub = hubs[h.ordinal()];
                    hub.setLights(lights);          // ROBOT: sends one-shot VM flags frame
                    if (robot) for (int m = 0; m < 3; m++)   // restore direct motor power
                        hub.motorPower(MoveHubGatt.MOTOR_A + m, motorValue[m]);
                }
            });
            enabledViews.add(mode); enabledViews.add(light);
            values = text(parent, "Rýchlosť 0 • Riadenie 0", 17);
            autoControls = column(parent);
            text(autoControls, "Držte šípku alebo posuvník. Uvoľnenie zastaví pohyb.", 14);
            LinearLayout directions = row(autoControls);
            hold(button(directions, "◀", null), 0, -35);
            hold(button(directions, "▲", null), 40, 0);
            hold(button(directions, "▼", null), -40, 0);
            hold(button(directions, "▶", null), 0, 35);
            slider(autoControls, "Rýchlosť −100 … +100", 100, -1);
            slider(autoControls, "Riadenie −70 … +70", 70, -2);
            robotControls = column(parent);
            text(robotControls, "ROBOT: priamy výkon motorov. Motor C je riadenie — používajte opatrne.", 14);
            slider(robotControls, "Motor A", 100, MoveHubGatt.MOTOR_A);
            slider(robotControls, "Motor B", 100, MoveHubGatt.MOTOR_B);
            slider(robotControls, "Motor C (riadenie)", 100, MoveHubGatt.MOTOR_C);
            button(parent, "STOP / BRZDA", () -> stopPanel(both ? bothPanel : this));
            updateMode();
        }
        void lightText() { light.setText(lights ? "SVETLÁ: ZAP" : "SVETLÁ: VYP"); }
        void updateMode() {
            mode.setText(robot ? "REŽIM: ROBOT" : "REŽIM: AUTO");
            autoControls.setVisibility(robot ? View.GONE : View.VISIBLE);
            robotControls.setVisibility(robot ? View.VISIBLE : View.GONE);
        }
        boolean allowed() {
            if (!foreground || (targets.length == 2) != both) return false;
            for (ControlRouter.Hub h : targets) if (!hubs[h.ordinal()].isReady()) return false;
            return true;
        }
        long[] epochs() {
            long[] result = new long[targets.length];
            for (int i = 0; i < targets.length; i++) result[i] = router.state(targets[i]).epoch;
            return result;
        }
        boolean valid(long[] epochs) {
            if (epochs == null || !allowed()) return false;
            for (int i = 0; i < targets.length; i++)
                if (epochs[i] != router.state(targets[i]).epoch) return false;
            return true;
        }
        void drive(int speed, int steer) {
            if (!allowed() || robot) return;
            for (ControlRouter.Hub h : targets) hubs[h.ordinal()].setDrive(speed, steer, lights, false);
            values.setText("Rýchlosť " + speed + " • Riadenie " + steer);
        }
        void reset() {
            for (SeekBar slider : sliders) slider.setProgress(slider.getMax() / 2);
            java.util.Arrays.fill(motorValue, 0);
            values.setText("Rýchlosť 0 • Riadenie 0");
        }
        void hold(Button button, int speed, int steer) {
            enabledViews.add(button);
            button.setOnTouchListener(new View.OnTouchListener() {
                long[] gesture;
                @Override public boolean onTouch(View v, MotionEvent event) {
                    switch (event.getActionMasked()) {
                        case MotionEvent.ACTION_DOWN:
                            if (!allowed()) return true;
                            gesture = epochs();
                            v.getParent().requestDisallowInterceptTouchEvent(true);
                            ControlRouter.State s = router.state(targets[0]);
                            drive(speed == 0 ? s.speed : speed, steer == 0 ? s.steer : steer);
                            v.setPressed(true);
                            return true;
                        case MotionEvent.ACTION_MOVE:
                            if (!valid(gesture) || event.getX() < 0 || event.getY() < 0
                                    || event.getX() > v.getWidth() || event.getY() > v.getHeight()) {
                                if (gesture != null) stopPanel(Panel.this);
                                gesture = null; v.setPressed(false);
                            }
                            return true;
                        case MotionEvent.ACTION_UP:
                        case MotionEvent.ACTION_CANCEL:
                            if (gesture != null) stopPanel(Panel.this);
                            gesture = null; v.setPressed(false);
                            v.getParent().requestDisallowInterceptTouchEvent(false);
                            return true;
                        default: return true;
                    }
                }
            });
        }
        void slider(LinearLayout parent, String label, int zero, int motor) {
            text(parent, label, 15);
            SeekBar slider = new SeekBar(MainActivity.this);
            slider.setMax(zero * 2); slider.setProgress(zero);
            slider.setMinimumHeight(dp(48));
            parent.addView(slider, new LinearLayout.LayoutParams(-1, dp(48)));
            sliders.add(slider); enabledViews.add(slider);
            slider.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
                long[] gesture;
                @Override public void onStartTrackingTouch(SeekBar s) { gesture = allowed() ? epochs() : null; }
                @Override public void onProgressChanged(SeekBar s, int progress, boolean user) {
                    if (!user || !valid(gesture)) return;
                    int value = progress - zero;
                    if (motor >= 0) {
                        motorValue[motor - MoveHubGatt.MOTOR_A] = value;   // remember for light-toggle restore
                        if (robot) for (ControlRouter.Hub h : targets) hubs[h.ordinal()].motorPower(motor, value);
                    } else {
                        ControlRouter.State state = router.state(targets[0]);
                        drive(motor == -1 ? value : state.speed, motor == -2 ? value : state.steer);
                    }
                }
                @Override public void onStopTrackingTouch(SeekBar s) {
                    gesture = null; stopPanel(Panel.this);
                }
            });
            slider.setOnTouchListener((v, event) -> {
                if (event.getActionMasked() == MotionEvent.ACTION_DOWN)
                    v.getParent().requestDisallowInterceptTouchEvent(true);
                if (event.getActionMasked() == MotionEvent.ACTION_UP || event.getActionMasked() == MotionEvent.ACTION_CANCEL) {
                    stopPanel(this);
                    v.getParent().requestDisallowInterceptTouchEvent(false);
                }
                return false;
            });
        }
    }

    private void stopPanel(Panel panel) {
        if (panel == null) return;
        for (ControlRouter.Hub h : panel.targets) {
            hubs[h.ordinal()].emergencyStop();
            if (panels[h.ordinal()] != null) panels[h.ordinal()].reset();
        }
        panel.reset();
        if (bothPanel != null) bothPanel.reset();
    }
    private void stopAll() {
        router.stopAll();
        for (MoveHubGatt hub : hubs) if (hub != null) hub.emergencyStop();
        for (Panel panel : panels) if (panel != null) panel.reset();
        if (bothPanel != null) bothPanel.reset();
        refreshEnabled();
    }
    private void refreshEnabled() {
        for (int i = 0; i < hubs.length; i++) {
            if (connects[i] != null) {
                connects[i].setText(hubs[i].isClosing() ? "ODPÁJAM…" : hubs[i].isClosed() ? "PRIPOJIŤ" : "ODPOJIŤ / ZRUŠIŤ");
                connects[i].setEnabled(!hubs[i].isClosing());
            }
            if (panels[i] != null) for (View v : panels[i].enabledViews) v.setEnabled(panels[i].allowed());
        }
        if (bothPanel != null) for (View v : bothPanel.enabledViews) v.setEnabled(bothPanel.allowed());
    }
    private void connectClicked(int index) {
        MoveHubGatt hub = hubs[index];
        if (!hub.isClosed()) { stopPanel(both ? bothPanel : panels[index]); hub.safeClose(); refreshEnabled(); return; }
        String[] permissions = Build.VERSION.SDK_INT >= 31
                ? new String[]{Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT}
                : new String[]{Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION};
        for (String permission : permissions) if (checkSelfPermission(permission) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(permissions, REQ_PERMS); return;
        }
        if (foreground) { panels[index].reset(); hub.scanAndConnect(); refreshEnabled(); }
    }
    private void confirmCalibration(int index) {
        if (!hubs[index].isReady()) return;
        stopPanel(both ? bothPanel : panels[index]);
        new AlertDialog.Builder(this).setTitle("Kalibrácia — " + ControlRouter.Hub.values()[index].label)
                .setMessage("Odpojte USB-C a zdvihnite kolesá. Riadenie sa bude pohybovať. Pokračovať?")
                .setNegativeButton("Zrušiť", null).setPositiveButton("Kalibrovať", (d, which) -> {
                    panels[index].robot = false; panels[index].updateMode();
                    hubs[index].calibrate(); refreshEnabled();
                }).show();
    }
    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(request, permissions, results);
        if (request != REQ_PERMS) return;
        boolean granted = results.length > 0;
        for (int result : results) granted &= result == PackageManager.PERMISSION_GRANTED;
        Toast.makeText(this, granted ? "Oprávnenia povolené — stlačte PRIPOJIŤ."
                : "Povoľte Bluetooth oprávnenia v nastaveniach aplikácie.", Toast.LENGTH_LONG).show();
    }
    @Override protected void onResume() { super.onResume(); foreground = true; refreshEnabled(); }
    @Override protected void onPause() {
        foreground = false;
        stopAll();
        for (MoveHubGatt hub : hubs) if (hub != null) hub.safeClose();
        refreshEnabled();
        super.onPause();
    }
    @Override protected void onDestroy() {
        for (MoveHubGatt hub : hubs) if (hub != null) hub.safeClose();
        super.onDestroy();
    }
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private LinearLayout column(LinearLayout parent) {
        LinearLayout result = new LinearLayout(this); result.setOrientation(LinearLayout.VERTICAL);
        result.setPadding(dp(6), dp(8), dp(6), dp(8));
        parent.addView(result, new LinearLayout.LayoutParams(-1, -2)); return result;
    }
    private LinearLayout row(LinearLayout parent) {
        LinearLayout result = new LinearLayout(this); result.setOrientation(LinearLayout.HORIZONTAL);
        parent.addView(result, new LinearLayout.LayoutParams(-1, -2)); return result;
    }
    private TextView text(LinearLayout parent, String value, int size) {
        TextView view = new TextView(this); view.setText(value); view.setTextSize(size);
        parent.addView(view); return view;
    }
    private Button button(LinearLayout parent, String value, Runnable action) {
        Button view = new Button(this); view.setText(value); view.setTextSize(15); view.setMinHeight(dp(52));
        if (parent.getOrientation() == LinearLayout.HORIZONTAL)
            parent.addView(view, new LinearLayout.LayoutParams(0, -2, 1));
        else parent.addView(view, new LinearLayout.LayoutParams(-1, -2));
        if (action != null) view.setOnClickListener(v -> action.run());
        return view;
    }
}
