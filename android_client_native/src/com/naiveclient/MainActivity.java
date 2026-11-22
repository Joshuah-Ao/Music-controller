package com.naiveclient;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.text.TextUtils;
import android.view.View;
import android.widget.Button;
import android.widget.CompoundButton;
import android.widget.EditText;
import android.widget.SeekBar;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity implements MediaControlService.StatusListener {

    private static final String PREFS_NAME = MediaControlService.PREFS_NAME;
    private static final String KEY_SERVER_ADDR = MediaControlService.KEY_SERVER_ADDR;
    private static final String KEY_MEDIA_CONTROL_ENABLED = MediaControlService.KEY_MEDIA_CONTROL_ENABLED;
    private static final int REQ_POST_NOTIFICATIONS = 1001;

    private EditText etServerIp;
    private Button btnConnect;
    private TextView tvStatusBadge;

    private TextView tvSongTitle;
    private TextView tvSongArtist;
    private TextView tvSongApp;
    private SeekBar seekProgress;
    private TextView tvTimeCurrent;
    private TextView tvTimeTotal;
    private TextView tvTimelineHint;
    private Button btnSeekBack;
    private Button btnSeekForward;

    private Button btnPrev;
    private Button btnNext;
    private Button btnPlayPause;
    private Button btnVolDown;
    private Button btnLike;
    private Button btnVolUp;
    private Button btnSleep;
    private Button btnShutdown;

    private Switch switchMediaControl;
    private TextView tvMediaServiceStatus;

    private TextView tvInfoConnection;
    private TextView tvInfoPlatform;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private Vibrator vibrator;

    private boolean isConnected = false;
    private String currentServer = "";
    private boolean mediaControlEnabled = true;
    private boolean ignoreSwitchCallback = false;

    private MediaStatus uiStatus = new MediaStatus();
    private boolean isUserSeeking = false;
    private long ignorePlayStateUntilMs = 0L;
    private long ignoreLikeStateUntilMs = 0L;
    private long ignorePositionUntilMs = 0L;

    private final Runnable progressTickRunnable = new Runnable() {
        @Override
        public void run() {
            if (!isUserSeeking && uiStatus.hasTimeline && uiStatus.durationSec > 0.5 && uiStatus.isPlaying) {
                renderSeekBar(uiStatus.getCurrentPositionSec(), uiStatus.durationSec);
            }
            mainHandler.postDelayed(this, 500L);
        }
    };

    // 当用户关闭状态栏服务时，主界面自身仍负责前台轮询进度条
    private final Runnable fallbackPollRunnable = new Runnable() {
        @Override
        public void run() {
            if (!MediaControlService.isServiceRunning() && !TextUtils.isEmpty(currentServer)) {
                pollStatusDirectly(false);
            }
            mainHandler.postDelayed(this, 2000L);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        vibrator = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);

        initViews();
        loadPreferences();
        setupListeners();
        updateMediaServiceCardUI();
    }

    @Override
    protected void onStart() {
        super.onStart();
        MediaControlService.setStatusListener(this);
        if (MediaControlService.isServiceRunning()) {
            onMediaStatusUpdated(MediaControlService.getLatestStatus());
            Intent refresh = new Intent(this, MediaControlService.class);
            refresh.setAction(MediaControlService.ACTION_REFRESH);
            MediaControlService.startServiceCompat(this, refresh);
        }
        updateMediaServiceCardUI();
        mainHandler.removeCallbacks(progressTickRunnable);
        mainHandler.postDelayed(progressTickRunnable, 500L);
        mainHandler.removeCallbacks(fallbackPollRunnable);
        mainHandler.postDelayed(fallbackPollRunnable, 1000L);
    }

    @Override
    protected void onStop() {
        super.onStop();
        MediaControlService.setStatusListener(null);
        mainHandler.removeCallbacks(progressTickRunnable);
        mainHandler.removeCallbacks(fallbackPollRunnable);
    }

    private void initViews() {
        etServerIp = (EditText) findViewById(R.id.et_server_ip);
        btnConnect = (Button) findViewById(R.id.btn_connect);
        tvStatusBadge = (TextView) findViewById(R.id.tv_status_badge);

        tvSongTitle = (TextView) findViewById(R.id.tv_song_title);
        tvSongArtist = (TextView) findViewById(R.id.tv_song_artist);
        tvSongApp = (TextView) findViewById(R.id.tv_song_app);
        seekProgress = (SeekBar) findViewById(R.id.seek_progress);
        tvTimeCurrent = (TextView) findViewById(R.id.tv_time_current);
        tvTimeTotal = (TextView) findViewById(R.id.tv_time_total);
        tvTimelineHint = (TextView) findViewById(R.id.tv_timeline_hint);
        btnSeekBack = (Button) findViewById(R.id.btn_seek_back);
        btnSeekForward = (Button) findViewById(R.id.btn_seek_forward);

        btnPrev = (Button) findViewById(R.id.btn_prev);
        btnNext = (Button) findViewById(R.id.btn_next);
        btnPlayPause = (Button) findViewById(R.id.btn_play_pause);
        btnVolDown = (Button) findViewById(R.id.btn_vol_down);
        btnLike = (Button) findViewById(R.id.btn_like);
        btnVolUp = (Button) findViewById(R.id.btn_vol_up);
        btnSleep = (Button) findViewById(R.id.btn_sleep);
        btnShutdown = (Button) findViewById(R.id.btn_shutdown);

        switchMediaControl = (Switch) findViewById(R.id.switch_media_control);
        tvMediaServiceStatus = (TextView) findViewById(R.id.tv_media_service_status);

        tvInfoConnection = (TextView) findViewById(R.id.tv_info_connection);
        tvInfoPlatform = (TextView) findViewById(R.id.tv_info_platform);

        seekProgress.setEnabled(false);
    }

    private void loadPreferences() {
        SharedPreferences sp = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        mediaControlEnabled = sp.getBoolean(KEY_MEDIA_CONTROL_ENABLED, true);
        ignoreSwitchCallback = true;
        switchMediaControl.setChecked(mediaControlEnabled);
        ignoreSwitchCallback = false;

        String saved = sp.getString(KEY_SERVER_ADDR, "");
        if (!TextUtils.isEmpty(saved)) {
            etServerIp.setText(saved);
            currentServer = saved;
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

        seekProgress.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar seekBar, int progress, boolean fromUser) {
                if (fromUser && uiStatus.durationSec > 0.5) {
                    double previewSec = (progress / 1000.0) * uiStatus.durationSec;
                    tvTimeCurrent.setText(MediaStatus.formatTime(previewSec));
                }
            }

            @Override
            public void onStartTrackingTouch(SeekBar seekBar) {
                isUserSeeking = true;
            }

            @Override
            public void onStopTrackingTouch(SeekBar seekBar) {
                isUserSeeking = false;
                if (uiStatus.durationSec > 0.5) {
                    double targetSec = (seekBar.getProgress() / 1000.0) * uiStatus.durationSec;
                    vibrateFeedback();
                    performSeekTo(targetSec);
                }
            }
        });

        btnSeekBack.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                vibrateFeedback();
                performSeekDelta(-10.0);
            }
        });

        btnSeekForward.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                vibrateFeedback();
                performSeekDelta(10.0);
            }
        });

        btnPrev.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                vibrateFeedback();
                uiStatus.isPlaying = true;
                ignorePlayStateUntilMs = SystemClock.elapsedRealtime() + 1500L;
                updatePlayPauseButtonUI();
                sendCommand("prev_song", "⏮ 上一首", true);
            }
        });

        btnNext.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                vibrateFeedback();
                uiStatus.isPlaying = true;
                ignorePlayStateUntilMs = SystemClock.elapsedRealtime() + 1500L;
                updatePlayPauseButtonUI();
                sendCommand("next_song", "⏭ 下一首", true);
            }
        });

        btnPlayPause.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                vibrateFeedback();
                long now = SystemClock.elapsedRealtime();
                uiStatus.positionSec = uiStatus.getCurrentPositionSec();
                uiStatus.syncedAtElapsedMs = now;
                uiStatus.isPlaying = !uiStatus.isPlaying;
                ignorePlayStateUntilMs = now + 1500L;
                updatePlayPauseButtonUI();
                sendCommand("pause_play", uiStatus.isPlaying ? "▶ 已播放" : "⏸ 已暂停", true);
            }
        });

        btnVolDown.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                vibrateFeedback();
                sendCommand("volume_down", "🔉 音量 -", false);
            }
        });

        btnLike.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                vibrateFeedback();
                if (uiStatus.isLiked) {
                    showUnlikeConfirmDialog();
                    return;
                }
                uiStatus.isLiked = true;
                ignoreLikeStateUntilMs = SystemClock.elapsedRealtime() + 1500L;
                updateLikeButtonUI();
                sendCommand("like_song", "❤️ 已喜欢当前歌曲 (Ctrl+Alt+L)", true);
            }
        });

        btnLike.setOnLongClickListener(new View.OnLongClickListener() {
            @Override
            public boolean onLongClick(View v) {
                vibrateFeedback();
                syncLikeStateOnly();
                return true;
            }
        });

        btnVolUp.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                vibrateFeedback();
                sendCommand("volume_up", "🔊 音量 +", false);
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

        switchMediaControl.setOnCheckedChangeListener(new CompoundButton.OnCheckedChangeListener() {
            @Override
            public void onCheckedChanged(CompoundButton buttonView, boolean isChecked) {
                if (ignoreSwitchCallback) {
                    return;
                }
                vibrateFeedback();
                mediaControlEnabled = isChecked;
                saveMediaControlEnabled(isChecked);
                if (isChecked) {
                    if (TextUtils.isEmpty(currentServer)) {
                        showToast("请先输入并连接电脑 IP 后再启用");
                        updateMediaServiceCardUI();
                        return;
                    }
                    ensureNotificationPermissionAndStartService(true);
                } else {
                    stopMediaControlService();
                }
            }
        });
    }

    private boolean hasNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33) {
            return checkSelfPermission("android.permission.POST_NOTIFICATIONS") == PackageManager.PERMISSION_GRANTED;
        }
        return true;
    }

    private void ensureNotificationPermissionAndStartService(boolean promptIfDenied) {
        if (TextUtils.isEmpty(currentServer)) {
            return;
        }
        if (Build.VERSION.SDK_INT >= 33 && !hasNotificationPermission()) {
            if (promptIfDenied) {
                requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, REQ_POST_NOTIFICATIONS);
            }
            updateMediaServiceCardUI();
            return;
        }
        startMediaControlService();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_POST_NOTIFICATIONS) {
            boolean granted = grantResults != null && grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED;
            if (granted) {
                startMediaControlService();
                showToast("已开启状态栏与锁屏控制");
            } else {
                showToast("未授予通知权限，无法显示状态栏与锁屏控制栏");
                updateMediaServiceCardUI();
            }
        }
    }

    private void startMediaControlService() {
        if (TextUtils.isEmpty(currentServer)) {
            return;
        }
        Intent intent = new Intent(this, MediaControlService.class);
        intent.setAction(MediaControlService.ACTION_START);
        intent.putExtra(MediaControlService.EXTRA_SERVER_ADDR, currentServer);
        MediaControlService.startServiceCompat(this, intent);
        updateMediaServiceCardUI();
    }

    private void stopMediaControlService() {
        Intent intent = new Intent(this, MediaControlService.class);
        intent.setAction(MediaControlService.ACTION_STOP);
        try {
            startService(intent);
        } catch (Exception ignored) {
        }
        updateMediaServiceCardUI();
    }

    private void updateMediaServiceCardUI() {
        boolean running = MediaControlService.isServiceRunning();
        if (running) {
            tvMediaServiceStatus.setText("状态: ● 已开启 · 状态栏与锁屏常驻媒体控制中");
            tvMediaServiceStatus.setTextColor(getResources().getColor(R.color.badge_online_text));
        } else if (mediaControlEnabled && Build.VERSION.SDK_INT >= 33 && !hasNotificationPermission()) {
            tvMediaServiceStatus.setText("状态: ⚠ 请允许通知权限以展示状态栏与锁屏卡片");
            tvMediaServiceStatus.setTextColor(getResources().getColor(R.color.text_accent));
        } else if (mediaControlEnabled) {
            tvMediaServiceStatus.setText("状态: 连接电脑后自动开启状态栏与锁屏控制");
            tvMediaServiceStatus.setTextColor(getResources().getColor(R.color.text_accent));
        } else {
            tvMediaServiceStatus.setText("状态: ○ 已关闭");
            tvMediaServiceStatus.setTextColor(getResources().getColor(R.color.text_secondary));
        }
    }

    private void updatePlayPauseButtonUI() {
        if (uiStatus.isPlaying) {
            btnPlayPause.setText("⏸ 正在播放 · 点击暂停");
        } else {
            btnPlayPause.setText("▶ 已暂停 · 点击播放");
        }
    }

    private void updateLikeButtonUI() {
        if (uiStatus.isLiked) {
            btnLike.setText(R.string.btn_liked);
            btnLike.setBackgroundResource(R.drawable.bg_btn_like_active);
        } else {
            btnLike.setText(R.string.btn_like);
            btnLike.setBackgroundResource(R.drawable.bg_btn_like);
        }
    }

    private void renderSeekBar(double posSec, double durSec) {
        if (durSec > 0.5) {
            double clamped = Math.max(0.0, Math.min(durSec, posSec));
            int progress = (int) Math.round((clamped / durSec) * 1000.0);
            seekProgress.setEnabled(true);
            seekProgress.setProgress(progress);
            tvTimeCurrent.setText(MediaStatus.formatTime(clamped));
            tvTimeTotal.setText(MediaStatus.formatTime(durSec));
        } else {
            seekProgress.setEnabled(false);
            seekProgress.setProgress(0);
            tvTimeCurrent.setText("--:--");
            tvTimeTotal.setText("--:--");
        }
    }

    private void applyMediaStatusToUI(MediaStatus st) {
        long now = SystemClock.elapsedRealtime();

        if (st.online) {
            if (!isConnected) {
                isConnected = true;
                updateConnectionBadge(true);
            }
        } else if (isConnected) {
            isConnected = false;
            updateConnectionBadge(false);
        }

        if (now < ignorePlayStateUntilMs) {
            st.isPlaying = uiStatus.isPlaying;
        }
        if (now < ignoreLikeStateUntilMs) {
            st.isLiked = uiStatus.isLiked;
        }
        if (now < ignorePositionUntilMs && uiStatus.hasTimeline && st.hasTimeline) {
            st.positionSec = uiStatus.getCurrentPositionSec();
            st.syncedAtElapsedMs = now;
        }

        uiStatus = st;
        updatePlayPauseButtonUI();
        updateLikeButtonUI();

        if (st.available) {
            tvSongTitle.setText(TextUtils.isEmpty(st.title) ? "正在播放媒体" : st.title);
            tvSongArtist.setText(TextUtils.isEmpty(st.artist) ? (st.isPlaying ? "正在播放中" : "已暂停") : st.artist);
            tvSongApp.setText(TextUtils.isEmpty(st.app) ? "PC 媒体" : st.app);

            if (st.hasTimeline && st.durationSec > 0.5) {
                if ("cdp".equals(st.likeSource)) {
                    tvTimelineHint.setText("已直连网易云 CDP · 进度拖动与红心状态实时同步");
                } else if (st.cdpConnected) {
                    tvTimelineHint.setText("已直连网易云 CDP · 支持拖动进度条与 ±10s 微调");
                } else {
                    tvTimelineHint.setText("拖动进度条或点击 ±10s 实时跳转电脑播放进度");
                }
                if (!isUserSeeking) {
                    renderSeekBar(st.getCurrentPositionSec(), st.durationSec);
                }
            } else {
                if (!isUserSeeking) {
                    renderSeekBar(0.0, 0.0);
                }
                if (!TextUtils.isEmpty(st.cdpHint)) {
                    tvTimelineHint.setText(st.cdpHint);
                } else {
                    tvTimelineHint.setText("当前播放器未开放系统进度条，快捷键控制正常生效");
                }
            }
        } else {
            tvSongTitle.setText(st.online ? "等待播放器就绪" : "等待连接电脑");
            tvSongArtist.setText(st.online ? "请先在电脑打开网易云等音乐软件" : "输入电脑 IP 并点击连接");
            tvSongApp.setText(st.online ? "待机中" : "未连接");
            if (!isUserSeeking) {
                renderSeekBar(0.0, 0.0);
            }
            tvTimelineHint.setText("支持拖动进度条与 ±10s 快进快退");
        }
    }

    @Override
    public void onMediaStatusUpdated(final MediaStatus status) {
        mainHandler.post(new Runnable() {
            @Override
            public void run() {
                applyMediaStatusToUI(status);
            }
        });
    }

    @Override
    public void onServiceStateChanged(final boolean running) {
        mainHandler.post(new Runnable() {
            @Override
            public void run() {
                if (!running && mediaControlEnabled) {
                    // 用户从通知栏点击“关闭”后同步开关状态
                    mediaControlEnabled = false;
                    saveMediaControlEnabled(false);
                    ignoreSwitchCallback = true;
                    switchMediaControl.setChecked(false);
                    ignoreSwitchCallback = false;
                }
                updateMediaServiceCardUI();
            }
        });
    }

    private void updateConnectionBadge(boolean online) {
        if (online) {
            tvStatusBadge.setBackgroundResource(R.drawable.bg_badge_connected);
            tvStatusBadge.setText(R.string.status_connected);
            tvStatusBadge.setTextColor(getResources().getColor(R.color.badge_online_text));
            tvInfoConnection.setText("运行状态: ● 正常在线");
        } else {
            tvStatusBadge.setBackgroundResource(R.drawable.bg_badge_disconnected);
            tvStatusBadge.setText(R.string.status_disconnected);
            tvStatusBadge.setTextColor(getResources().getColor(R.color.badge_offline_text));
            tvInfoConnection.setText("运行状态: ○ 等待连接电脑 (离线)");
        }
    }

    private void saveServer(String addr) {
        SharedPreferences sp = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        sp.edit().putString(KEY_SERVER_ADDR, addr).apply();
    }

    private void saveMediaControlEnabled(boolean enabled) {
        SharedPreferences sp = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        sp.edit().putBoolean(KEY_MEDIA_CONTROL_ENABLED, enabled).apply();
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
                        updateConnectionBadge(finalSuccess);
                        if (finalSuccess) {
                            tvInfoConnection.setText("运行状态: ● 正常在线 (已成功握手)");
                            tvInfoPlatform.setText("控制目标: " + finalPlatform + " 系统音乐播放器");
                            if (!isSilent) {
                                showToast("已成功连接电脑！");
                            }
                            if (mediaControlEnabled) {
                                ensureNotificationPermissionAndStartService(!isSilent);
                            } else {
                                pollStatusDirectly(true);
                            }
                        } else {
                            if (!isSilent) {
                                showToast("连接失败，请确认电脑已开启控制端且在同一WiFi");
                            }
                        }
                    }
                });
            }
        });
    }

    private void performSeekTo(final double targetSec) {
        if (TextUtils.isEmpty(currentServer)) {
            showToast("请先输入并连接电脑 IP 地址");
            return;
        }
        long now = SystemClock.elapsedRealtime();
        uiStatus.positionSec = targetSec;
        uiStatus.syncedAtElapsedMs = now;
        ignorePositionUntilMs = now + 1500L;
        renderSeekBar(targetSec, uiStatus.durationSec);

        if (MediaControlService.isServiceRunning()) {
            Intent intent = new Intent(this, MediaControlService.class);
            intent.setAction(MediaControlService.ACTION_SEEK);
            intent.putExtra(MediaControlService.EXTRA_POSITION_SEC, targetSec);
            MediaControlService.startServiceCompat(this, intent);
            return;
        }

        executor.execute(new Runnable() {
            @Override
            public void run() {
                final JSONObject json = httpGetJson(String.format(Locale.US, "http://%s/media_seek?position=%.2f", currentServer, targetSec));
                mainHandler.post(new Runnable() {
                    @Override
                    public void run() {
                        if (json == null) {
                            showToast("连接失败，请检查电脑是否在线");
                            return;
                        }
                        boolean ok = json.optBoolean("ok", false);
                        String msg = json.optString("message", "");
                        if (ok) {
                            if (!TextUtils.isEmpty(msg)) {
                                showToast("⏩ " + msg);
                            }
                        } else {
                            showToast(TextUtils.isEmpty(msg) ? "当前播放器暂不支持拖动进度条" : msg);
                            ignorePositionUntilMs = 0L;
                            pollStatusDirectly(true);
                        }
                    }
                });
            }
        });
    }

    private void performSeekDelta(final double deltaSec) {
        if (TextUtils.isEmpty(currentServer)) {
            showToast("请先输入并连接电脑 IP 地址");
            return;
        }
        long now = SystemClock.elapsedRealtime();
        if (uiStatus.hasTimeline && uiStatus.durationSec > 0.5) {
            double target = Math.max(0.0, Math.min(uiStatus.durationSec, uiStatus.getCurrentPositionSec() + deltaSec));
            uiStatus.positionSec = target;
            uiStatus.syncedAtElapsedMs = now;
            ignorePositionUntilMs = now + 1500L;
            renderSeekBar(target, uiStatus.durationSec);
        }

        if (MediaControlService.isServiceRunning()) {
            Intent intent = new Intent(this, MediaControlService.class);
            intent.setAction(MediaControlService.ACTION_SEEK_DELTA);
            intent.putExtra(MediaControlService.EXTRA_DELTA_SEC, deltaSec);
            MediaControlService.startServiceCompat(this, intent);
            return;
        }

        executor.execute(new Runnable() {
            @Override
            public void run() {
                final JSONObject json = httpGetJson(String.format(Locale.US, "http://%s/media_seek?delta=%.2f", currentServer, deltaSec));
                mainHandler.post(new Runnable() {
                    @Override
                    public void run() {
                        if (json == null) {
                            showToast("连接失败，请检查电脑是否在线");
                            return;
                        }
                        boolean ok = json.optBoolean("ok", false);
                        String msg = json.optString("message", "");
                        if (ok) {
                            if (!TextUtils.isEmpty(msg)) {
                                showToast(msg);
                            }
                            pollStatusDirectly(true);
                        } else {
                            showToast(TextUtils.isEmpty(msg) ? "当前播放器暂不支持快进/快退" : msg);
                            ignorePositionUntilMs = 0L;
                            pollStatusDirectly(true);
                        }
                    }
                });
            }
        });
    }

    private void pollStatusDirectly(final boolean force) {
        if (TextUtils.isEmpty(currentServer)) {
            return;
        }
        executor.execute(new Runnable() {
            @Override
            public void run() {
                final JSONObject json = httpGetJson("http://" + currentServer + "/media_status" + (force ? "?force=1" : ""));
                mainHandler.post(new Runnable() {
                    @Override
                    public void run() {
                        if (json != null) {
                            applyMediaStatusToUI(MediaStatus.fromJson(json));
                        }
                    }
                });
            }
        });
    }

    private void sendCommand(final String action, final String successMsg, final boolean refreshAfter) {
        if (TextUtils.isEmpty(currentServer)) {
            showToast("请先输入并连接电脑 IP 地址");
            return;
        }

        executor.execute(new Runnable() {
            @Override
            public void run() {
                boolean sent = false;
                try {
                    String targetUrl = "http://" + currentServer + "/?action=" + action + "&format=json";
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
                                updateConnectionBadge(true);
                            }
                            if (!TextUtils.isEmpty(successMsg)) {
                                showToast(successMsg);
                            }
                            if (refreshAfter) {
                                mainHandler.postDelayed(new Runnable() {
                                    @Override
                                    public void run() {
                                        if (MediaControlService.isServiceRunning()) {
                                            Intent refresh = new Intent(MainActivity.this, MediaControlService.class);
                                            refresh.setAction(MediaControlService.ACTION_REFRESH);
                                            MediaControlService.startServiceCompat(MainActivity.this, refresh);
                                        } else {
                                            pollStatusDirectly(true);
                                        }
                                    }
                                }, 380L);
                            }
                        } else {
                            showToast("指令发送失败，请检查网络或电脑是否在线");
                        }
                    }
                });
            }
        });
    }

    private JSONObject httpGetJson(String targetUrl) {
        HttpURLConnection conn = null;
        try {
            URL url = new URL(targetUrl);
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("GET");
            conn.setConnectTimeout(2500);
            conn.setReadTimeout(2500);
            conn.connect();
            if (conn.getResponseCode() == 200) {
                BufferedReader reader = new BufferedReader(new InputStreamReader(conn.getInputStream(), "UTF-8"));
                StringBuilder sb = new StringBuilder();
                String line;
                while ((line = reader.readLine()) != null) {
                    sb.append(line);
                }
                reader.close();
                return new JSONObject(sb.toString());
            }
        } catch (Exception ignored) {
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
        return null;
    }

    private void showUnlikeConfirmDialog() {
        AlertDialog.Builder builder = new AlertDialog.Builder(this);
        builder.setTitle(R.string.dialog_unlike_title);
        String msg;
        if (!TextUtils.isEmpty(uiStatus.title)) {
            msg = "确认取消喜欢《" + uiStatus.title + "》吗？将向电脑发送 Ctrl + Alt + L。";
        } else {
            msg = getString(R.string.dialog_unlike_msg);
        }
        builder.setMessage(msg);
        builder.setPositiveButton(R.string.dialog_unlike_ok, new DialogInterface.OnClickListener() {
            @Override
            public void onClick(DialogInterface dialog, int which) {
                uiStatus.isLiked = false;
                ignoreLikeStateUntilMs = SystemClock.elapsedRealtime() + 1500L;
                updateLikeButtonUI();
                sendCommand("unlike_song", "💔 已取消喜欢 (Ctrl+Alt+L)", true);
            }
        });
        builder.setNegativeButton(R.string.dialog_cancel, null);
        builder.show();
    }

    private void syncLikeStateOnly() {
        if (TextUtils.isEmpty(currentServer)) {
            showToast("请先输入并连接电脑 IP 地址");
            return;
        }
        final boolean nextState = !uiStatus.isLiked;
        uiStatus.isLiked = nextState;
        ignoreLikeStateUntilMs = SystemClock.elapsedRealtime() + 1500L;
        updateLikeButtonUI();

        executor.execute(new Runnable() {
            @Override
            public void run() {
                final JSONObject json = httpGetJson("http://" + currentServer + "/like_state?liked=" + (nextState ? "1" : "0"));
                mainHandler.post(new Runnable() {
                    @Override
                    public void run() {
                        if (json != null) {
                            showToast(nextState ? "❤️ 已同步标记为“已喜欢”（再次点击将二次确认）" : "🤍 已重置喜欢标记（未发快捷键）");
                            if (MediaControlService.isServiceRunning()) {
                                Intent refresh = new Intent(MainActivity.this, MediaControlService.class);
                                refresh.setAction(MediaControlService.ACTION_REFRESH);
                                MediaControlService.startServiceCompat(MainActivity.this, refresh);
                            }
                        } else {
                            showToast("同步状态失败，请检查电脑是否在线");
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
                sendCommand("sleep", "电脑已触发休眠/睡眠", false);
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
                sendCommand("shutdown", "电脑将在 5 秒后安全关机", false);
            }
        });
        builder.setNegativeButton(R.string.dialog_cancel, null);
        builder.show();
    }

    private void vibrateFeedback() {
        try {
            if (vibrator != null && vibrator.hasVibrator()) {
                if (Build.VERSION.SDK_INT >= 26) {
                    vibrator.vibrate(VibrationEffect.createOneShot(30, VibrationEffect.DEFAULT_AMPLITUDE));
                } else {
                    vibrator.vibrate(30);
                }
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
        mainHandler.removeCallbacksAndMessages(null);
        executor.shutdown();
    }
}
