package com.naiveclient;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.Vibrator;
import android.text.TextUtils;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {

    private static final String PREFS_NAME = "MusicControllerPrefs";
    private static final String KEY_SERVER_ADDR = "server_address";

    private EditText etServerIp;
    private Button btnConnect;
    private TextView tvStatusBadge;
    private Button btnPrev;
    private Button btnNext;
    private Button btnPlayPause;
    private Button btnVolDown;
    private Button btnVolUp;
    private Button btnSleep;
    private Button btnShutdown;
    private TextView tvInfoConnection;
    private TextView tvInfoPlatform;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private Vibrator vibrator;

    private boolean isConnected = false;
    private boolean isPlaying = false;
    private String currentServer = "";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        vibrator = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);

        initViews();
        loadSavedServer();
        setupListeners();
    }

    private void initViews() {
        etServerIp = (EditText) findViewById(R.id.et_server_ip);
        btnConnect = (Button) findViewById(R.id.btn_connect);
        tvStatusBadge = (TextView) findViewById(R.id.tv_status_badge);
        btnPrev = (Button) findViewById(R.id.btn_prev);
        btnNext = (Button) findViewById(R.id.btn_next);
        btnPlayPause = (Button) findViewById(R.id.btn_play_pause);
        btnVolDown = (Button) findViewById(R.id.btn_vol_down);
        btnVolUp = (Button) findViewById(R.id.btn_vol_up);
        btnSleep = (Button) findViewById(R.id.btn_sleep);
        btnShutdown = (Button) findViewById(R.id.btn_shutdown);
        tvInfoConnection = (TextView) findViewById(R.id.tv_info_connection);
        tvInfoPlatform = (TextView) findViewById(R.id.tv_info_platform);
    }

    private void loadSavedServer() {
        SharedPreferences sp = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        String saved = sp.getString(KEY_SERVER_ADDR, "");
        if (!TextUtils.isEmpty(saved)) {
            etServerIp.setText(saved);
            currentServer = saved;
            // 自动尝试静默握手连接
            connectToServer(saved, true);
        }
    }

    private void setupListeners() {
        btnConnect.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                vibrateFeedback();
                String input = etServerIp.getText().toString().trim();
                if (TextUtils.isEmpty(input)) {
                    showToast("请输入电脑 IP 地址与端口");
                    return;
                }
                // 去除可能误输入的 http:// 前缀或末尾斜线
                input = input.replace("http://", "").replace("https://", "");
                if (input.endsWith("/")) {
                    input = input.substring(0, input.length() - 1);
                }
                etServerIp.setText(input);
                currentServer = input;
                saveServer(input);
                connectToServer(input, false);
            }
        });

        btnPrev.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                vibrateFeedback();
                isPlaying = true;
                updatePlayPauseButtonUI();
                sendCommand("prev_song", "已发送：上一首");
            }
        });

        btnNext.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                vibrateFeedback();
                isPlaying = true;
                updatePlayPauseButtonUI();
                sendCommand("next_song", "已发送：下一首");
            }
        });

        btnPlayPause.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                vibrateFeedback();
                isPlaying = !isPlaying;
                updatePlayPauseButtonUI();
                sendCommand("pause_play", isPlaying ? "已播放" : "已暂停");
            }
        });

        btnVolDown.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                vibrateFeedback();
                sendCommand("volume_down", "音量 -");
            }
        });

        btnVolUp.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                vibrateFeedback();
                sendCommand("volume_up", "音量 +");
            }
        });

        btnSleep.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                vibrateFeedback();
                showSleepConfirmDialog();
            }
        });

        btnShutdown.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                vibrateFeedback();
                showShutdownConfirmDialog();
            }
        });
    }

    private void updatePlayPauseButtonUI() {
        if (isPlaying) {
            btnPlayPause.setText("⏸ 正在播放 · 点击暂停");
        } else {
            btnPlayPause.setText("▶ 已暂停 · 点击播放");
        }
    }

    private void saveServer(String addr) {
        SharedPreferences sp = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        sp.edit().putString(KEY_SERVER_ADDR, addr).apply();
    }

    private void connectToServer(final String host, final boolean isSilent) {
        if (!isSilent) {
            showToast("正在连接电脑 " + host + " ...");
        }
        executor.execute(new Runnable() {
            @Override
            public void run() {
                boolean success = false;
                String platform = "Windows";
                try {
                    String targetUrl = "http://" + host + "/mobile_connect";
                    URL url = new URL(targetUrl);
                    HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                    conn.setRequestMethod("GET");
                    conn.setConnectTimeout(3000);
                    conn.setReadTimeout(3000);
                    conn.connect();

                    int responseCode = conn.getResponseCode();
                    if (responseCode == 200) {
                        InputStream is = conn.getInputStream();
                        BufferedReader reader = new BufferedReader(new InputStreamReader(is, "UTF-8"));
                        StringBuilder sb = new StringBuilder();
                        String line;
                        while ((line = reader.readLine()) != null) {
                            sb.append(line);
                        }
                        reader.close();

                        JSONObject json = new JSONObject(sb.toString());
                        if (json.has("platform")) {
                            platform = json.optString("platform", "Windows");
                        }
                        success = true;
                    }
                    conn.disconnect();
                } catch (Exception e) {
                    success = false;
                }

                final boolean finalSuccess = success;
                final String finalPlatform = platform;
                mainHandler.post(new Runnable() {
                    @Override
                    public void run() {
                        isConnected = finalSuccess;
                        if (finalSuccess) {
                            tvStatusBadge.setBackgroundResource(R.drawable.bg_badge_connected);
                            tvStatusBadge.setText(R.string.status_connected);
                            tvStatusBadge.setTextColor(getResources().getColor(R.color.badge_online_text));
                            tvInfoConnection.setText("运行状态: ● 正常在线 (已成功握手)");
                            tvInfoPlatform.setText("控制目标: " + finalPlatform + " 系统音乐播放器");
                            if (!isSilent) {
                                showToast("已成功连接电脑！");
                            }
                        } else {
                            tvStatusBadge.setBackgroundResource(R.drawable.bg_badge_disconnected);
                            tvStatusBadge.setText(R.string.status_disconnected);
                            tvStatusBadge.setTextColor(getResources().getColor(R.color.badge_offline_text));
                            tvInfoConnection.setText("运行状态: ○ 等待连接电脑 (离线)");
                            if (!isSilent) {
                                showToast("连接失败，请确认电脑已开启控制端且在同一WiFi");
                            }
                        }
                    }
                });
            }
        });
    }

    private void sendCommand(final String action, final String successMsg) {
        if (TextUtils.isEmpty(currentServer)) {
            showToast("请先输入并连接电脑 IP 地址");
            return;
        }

        executor.execute(new Runnable() {
            @Override
            public void run() {
                boolean sent = false;
                try {
                    String targetUrl = "http://" + currentServer + "/?action=" + action;
                    URL url = new URL(targetUrl);
                    HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                    conn.setRequestMethod("GET");
                    conn.setConnectTimeout(2500);
                    conn.setReadTimeout(2500);
                    conn.connect();
                    int code = conn.getResponseCode();
                    sent = (code == 200);
                    conn.disconnect();
                } catch (Exception e) {
                    sent = false;
                }

                final boolean finalSent = sent;
                mainHandler.post(new Runnable() {
                    @Override
                    public void run() {
                        if (finalSent) {
                            if (!isConnected) {
                                isConnected = true;
                                tvStatusBadge.setBackgroundResource(R.drawable.bg_badge_connected);
                                tvStatusBadge.setText(R.string.status_connected);
                                tvStatusBadge.setTextColor(getResources().getColor(R.color.badge_online_text));
                                tvInfoConnection.setText("运行状态: ● 正常在线");
                            }
                        } else {
                            showToast("指令发送失败，请检查网络或电脑是否在线");
                        }
                    }
                });
            }
        });
    }

    private void showSleepConfirmDialog() {
        AlertDialog.Builder builder = new AlertDialog.Builder(this);
        builder.setTitle(R.string.dialog_sleep_title);
        builder.setMessage(R.string.dialog_sleep_msg);
        builder.setPositiveButton(R.string.dialog_ok, new DialogInterface.OnClickListener() {
            @Override
            public void onClick(DialogInterface dialog, int which) {
                sendCommand("sleep", "已发送一键睡眠指令");
                showToast("电脑已触发休眠/睡眠");
            }
        });
        builder.setNegativeButton(R.string.dialog_cancel, null);
        builder.show();
    }

    private void showShutdownConfirmDialog() {
        AlertDialog.Builder builder = new AlertDialog.Builder(this);
        builder.setTitle(R.string.dialog_shutdown_title);
        builder.setMessage(R.string.dialog_shutdown_msg);
        builder.setPositiveButton(R.string.dialog_ok, new DialogInterface.OnClickListener() {
            @Override
            public void onClick(DialogInterface dialog, int which) {
                sendCommand("shutdown", "已发送安全关机指令");
                showToast("电脑将在 5 秒后安全关机");
            }
        });
        builder.setNegativeButton(R.string.dialog_cancel, null);
        builder.show();
    }

    private void vibrateFeedback() {
        try {
            if (vibrator != null && vibrator.hasVibrator()) {
                vibrator.vibrate(35);
            }
        } catch (Exception ignored) {
        }
    }

    private void showToast(String msg) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        executor.shutdown();
    }
}
