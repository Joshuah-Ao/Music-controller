package com.naiveclient;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.drawable.Icon;
import android.media.MediaMetadata;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.SystemClock;
import android.text.TextUtils;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 前台媒体控制服务：
 * 1. 基于 Android 原生 MediaSession + Notification.MediaStyle，同时提供【状态栏/通知栏控制】与【锁屏界面控制】；
 * 2. 支持在锁屏界面与通知栏直接操作：上一首、播放/暂停、下一首、❤️ 喜欢 (Ctrl+Alt+L)、拖动播放进度条；
 * 3. 定时向电脑端拉取 /media_status 并将真实曲名、歌手、时长与进度同步给锁屏卡片与主界面。
 */
public class MediaControlService extends Service {

    public static final String PREFS_NAME = "MusicControllerPrefs";
    public static final String KEY_SERVER_ADDR = "server_address";
    public static final String KEY_MEDIA_CONTROL_ENABLED = "media_control_enabled";

    public static final String ACTION_START = "com.naiveclient.action.START";
    public static final String ACTION_STOP = "com.naiveclient.action.STOP";
    public static final String ACTION_PREV = "com.naiveclient.action.PREV";
    public static final String ACTION_PLAY_PAUSE = "com.naiveclient.action.PLAY_PAUSE";
    public static final String ACTION_NEXT = "com.naiveclient.action.NEXT";
    public static final String ACTION_LIKE = "com.naiveclient.action.LIKE";
    public static final String ACTION_SEEK = "com.naiveclient.action.SEEK";
    public static final String ACTION_SEEK_DELTA = "com.naiveclient.action.SEEK_DELTA";
    public static final String ACTION_REFRESH = "com.naiveclient.action.REFRESH";

    public static final String EXTRA_SERVER_ADDR = "extra_server_addr";
    public static final String EXTRA_POSITION_SEC = "extra_position_sec";
    public static final String EXTRA_DELTA_SEC = "extra_delta_sec";

    private static final String CHANNEL_ID = "music_remote_media_control";
    private static final int NOTIFICATION_ID = 10010;

    public interface StatusListener {
        void onMediaStatusUpdated(MediaStatus status);
        void onServiceStateChanged(boolean running);
    }

    private static volatile StatusListener statusListener = null;
    private static volatile boolean serviceRunning = false;
    private static volatile boolean uiVisible = false;
    private static volatile MediaStatus latestStatus = new MediaStatus();

    private MediaSession mediaSession;
    private NotificationManager notificationManager;
    private Bitmap largeIcon;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    private String serverAddr = "";
    private MediaStatus currentStatus = new MediaStatus();
    private boolean foregroundStarted = false;
    private int consecutiveFailures = 0;

    private long ignorePlayStateUntilMs = 0L;
    private long ignoreLikeStateUntilMs = 0L;
    private long ignorePositionUntilMs = 0L;
    private long pendingUnlikeConfirmUntilMs = 0L;

    private String lastMetaKey = "";
    private boolean lastNotifPlaying = false;
    private boolean lastNotifLiked = false;
    private boolean lastNotifOnline = false;
    private long lastPlaybackStateUpdateMs = 0L;

    public static void setStatusListener(StatusListener listener) {
        statusListener = listener;
        uiVisible = (listener != null);
    }

    public static boolean isServiceRunning() {
        return serviceRunning;
    }

    public static MediaStatus getLatestStatus() {
        return latestStatus.copy();
    }

    public static void startServiceCompat(Context ctx, Intent intent) {
        try {
            if (Build.VERSION.SDK_INT >= 26) {
                ctx.startForegroundService(intent);
            } else {
                ctx.startService(intent);
            }
        } catch (Exception ignored) {
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        serviceRunning = true;
        notificationManager = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        largeIcon = BitmapFactory.decodeResource(getResources(), R.drawable.ic_launcher);
        createNotificationChannel();
        initMediaSession();

        SharedPreferences sp = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        serverAddr = sp.getString(KEY_SERVER_ADDR, "");
        notifyServiceState(true);
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= 26 && notificationManager != null) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID,
                    "音乐遥控器状态栏与锁屏控制",
                    NotificationManager.IMPORTANCE_LOW
            );
            channel.setDescription("在状态栏和锁屏界面快捷控制电脑播放、切歌、喜欢与进度条");
            channel.setShowBadge(false);
            channel.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
            notificationManager.createNotificationChannel(channel);
        }
    }

    private void initMediaSession() {
        mediaSession = new MediaSession(this, "MusicControllerSession");
        mediaSession.setFlags(
                MediaSession.FLAG_HANDLES_MEDIA_BUTTONS |
                MediaSession.FLAG_HANDLES_TRANSPORT_CONTROLS
        );
        mediaSession.setCallback(new MediaSession.Callback() {
            @Override
            public void onPlay() {
                handlePlayPauseToggle();
            }

            @Override
            public void onPause() {
                handlePlayPauseToggle();
            }

            @Override
            public void onSkipToPrevious() {
                handleStepAction("prev_song", "⏮ 上一首");
            }

            @Override
            public void onSkipToNext() {
                handleStepAction("next_song", "⏭ 下一首");
            }

            @Override
            public void onSeekTo(long posMs) {
                handleSeekTo(posMs / 1000.0);
            }

            @Override
            public void onCustomAction(String action, Bundle extras) {
                if (ACTION_LIKE.equals(action)) {
                    handleLikeAction();
                } else if (ACTION_STOP.equals(action)) {
                    stopSelfSafely();
                }
            }
        });
        mediaSession.setActive(true);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && intent.hasExtra(EXTRA_SERVER_ADDR)) {
            String addr = intent.getStringExtra(EXTRA_SERVER_ADDR);
            if (!TextUtils.isEmpty(addr)) {
                serverAddr = addr;
            }
        }
        if (TextUtils.isEmpty(serverAddr)) {
            SharedPreferences sp = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            serverAddr = sp.getString(KEY_SERVER_ADDR, "");
        }

        String action = intent != null ? intent.getAction() : ACTION_START;
        if (ACTION_STOP.equals(action)) {
            // 需先确保前台服务生命周期合法再停止（避免 Android 8+ startForegroundService 抛异常）
            if (!foregroundStarted) {
                startForegroundCompat(buildNotification());
            }
            stopSelfSafely();
            return START_NOT_STICKY;
        }

        if (!foregroundStarted) {
            updateMediaSession(true);
            startForegroundCompat(buildNotification());
        }

        if (ACTION_PREV.equals(action)) {
            handleStepAction("prev_song", "⏮ 上一首");
        } else if (ACTION_PLAY_PAUSE.equals(action)) {
            handlePlayPauseToggle();
        } else if (ACTION_NEXT.equals(action)) {
            handleStepAction("next_song", "⏭ 下一首");
        } else if (ACTION_LIKE.equals(action)) {
            handleLikeAction();
        } else if (ACTION_SEEK.equals(action) && intent != null) {
            double posSec = intent.getDoubleExtra(EXTRA_POSITION_SEC, 0.0);
            handleSeekTo(posSec);
        } else if (ACTION_SEEK_DELTA.equals(action) && intent != null) {
            double deltaSec = intent.getDoubleExtra(EXTRA_DELTA_SEC, 0.0);
            handleSeekDelta(deltaSec);
        } else {
            // ACTION_START 或 ACTION_REFRESH
            schedulePoll(0L);
        }

        return START_STICKY;
    }

    private void startForegroundCompat(Notification notification) {
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
            } else {
                startForeground(NOTIFICATION_ID, notification);
            }
            foregroundStarted = true;
        } catch (Exception e) {
            try {
                startForeground(NOTIFICATION_ID, notification);
                foregroundStarted = true;
            } catch (Exception ignored) {
            }
        }
    }

    private void stopSelfSafely() {
        mainHandler.removeCallbacks(pollRunnable);
        if (mediaSession != null) {
            mediaSession.setActive(false);
        }
        try {
            stopForeground(true);
        } catch (Exception ignored) {
        }
        foregroundStarted = false;
        serviceRunning = false;
        notifyServiceState(false);
        stopSelf();
    }

    private final Runnable pollRunnable = new Runnable() {
        @Override
        public void run() {
            pollMediaStatus(false);
        }
    };

    private void schedulePoll(long delayMs) {
        mainHandler.removeCallbacks(pollRunnable);
        mainHandler.postDelayed(pollRunnable, Math.max(0L, delayMs));
    }

    private long nextPollIntervalMs() {
        if (consecutiveFailures >= 3) {
            return 8000L;
        }
        return uiVisible ? 2000L : 4000L;
    }

    private void handlePlayPauseToggle() {
        long now = SystemClock.elapsedRealtime();
        currentStatus.positionSec = currentStatus.getCurrentPositionSec();
        currentStatus.syncedAtElapsedMs = now;
        currentStatus.isPlaying = !currentStatus.isPlaying;
        ignorePlayStateUntilMs = now + 1500L;

        publishState(true);
        sendRemoteAction("pause_play", null, true);
    }

    private void handleStepAction(String action, String toastMsg) {
        long now = SystemClock.elapsedRealtime();
        currentStatus.isPlaying = true;
        pendingUnlikeConfirmUntilMs = 0L;
        ignorePlayStateUntilMs = now + 1500L;

        publishState(true);
        sendRemoteAction(action, toastMsg, true);
    }

    private void handleLikeAction() {
        long now = SystemClock.elapsedRealtime();
        if (!currentStatus.isLiked) {
            pendingUnlikeConfirmUntilMs = 0L;
            currentStatus.isLiked = true;
            ignoreLikeStateUntilMs = now + 1500L;
            publishState(true);
            sendRemoteAction("like_song", "❤️ 已喜欢当前歌曲 (Ctrl+Alt+L)", true);
            return;
        }

        // 状态栏/锁屏卡片防误触：已喜欢时需在 3.5 秒内再按一次确认取消喜欢
        if (now <= pendingUnlikeConfirmUntilMs) {
            pendingUnlikeConfirmUntilMs = 0L;
            currentStatus.isLiked = false;
            ignoreLikeStateUntilMs = now + 1500L;
            publishState(true);
            sendRemoteAction("unlike_song", "💔 已取消喜欢 (Ctrl+Alt+L)", true);
        } else {
            pendingUnlikeConfirmUntilMs = now + 3500L;
            showToast("⚠ 确认取消喜欢？3秒内再按一次红心确认");
        }
    }

    private void handleSeekTo(final double targetSec) {
        if (TextUtils.isEmpty(serverAddr)) {
            return;
        }
        long now = SystemClock.elapsedRealtime();
        double clamped = Math.max(0.0, targetSec);
        if (currentStatus.durationSec > 0.5 && clamped > currentStatus.durationSec) {
            clamped = currentStatus.durationSec;
        }
        currentStatus.positionSec = clamped;
        currentStatus.syncedAtElapsedMs = now;
        ignorePositionUntilMs = now + 1500L;
        publishState(true);

        final double finalTarget = clamped;
        executor.execute(new Runnable() {
            @Override
            public void run() {
                final JSONObject json = httpGetJson(String.format(Locale.US, "http://%s/media_seek?position=%.2f", serverAddr, finalTarget));
                mainHandler.post(new Runnable() {
                    @Override
                    public void run() {
                        if (json == null) {
                            showToast("连接电脑失败，进度跳转未生效");
                            return;
                        }
                        boolean ok = json.optBoolean("ok", false);
                        if (!ok) {
                            String msg = json.optString("message", "当前播放器暂不支持拖动进度条");
                            showToast(msg);
                            ignorePositionUntilMs = 0L;
                            schedulePoll(100L);
                        } else {
                            if (json.has("position")) {
                                currentStatus.positionSec = json.optDouble("position", finalTarget);
                                currentStatus.syncedAtElapsedMs = SystemClock.elapsedRealtime();
                                publishState(true);
                            }
                        }
                    }
                });
            }
        });
    }

    private void handleSeekDelta(final double deltaSec) {
        if (TextUtils.isEmpty(serverAddr)) {
            return;
        }
        long now = SystemClock.elapsedRealtime();
        if (currentStatus.hasTimeline && currentStatus.durationSec > 0.5) {
            double target = Math.max(0.0, Math.min(currentStatus.durationSec, currentStatus.getCurrentPositionSec() + deltaSec));
            currentStatus.positionSec = target;
            currentStatus.syncedAtElapsedMs = now;
            ignorePositionUntilMs = now + 1500L;
            publishState(true);
        }

        executor.execute(new Runnable() {
            @Override
            public void run() {
                final JSONObject json = httpGetJson(String.format(Locale.US, "http://%s/media_seek?delta=%.2f", serverAddr, deltaSec));
                mainHandler.post(new Runnable() {
                    @Override
                    public void run() {
                        if (json == null) {
                            showToast("连接电脑失败，进度调整未生效");
                            return;
                        }
                        boolean ok = json.optBoolean("ok", false);
                        String msg = json.optString("message", "");
                        if (ok) {
                            double dur = json.optDouble("duration", 0.0);
                            if (dur > 0.5) {
                                currentStatus.durationSec = dur;
                                currentStatus.hasTimeline = true;
                                currentStatus.positionSec = json.optDouble("position", currentStatus.positionSec);
                                currentStatus.syncedAtElapsedMs = SystemClock.elapsedRealtime();
                                publishState(true);
                            }
                            if (!TextUtils.isEmpty(msg)) {
                                showToast(msg);
                            }
                        } else {
                            showToast(TextUtils.isEmpty(msg) ? "当前播放器暂不支持快进/快退" : msg);
                            ignorePositionUntilMs = 0L;
                            schedulePoll(100L);
                        }
                    }
                });
            }
        });
    }

    private void sendRemoteAction(final String action, final String toastOnSuccess, final boolean refreshAfter) {
        if (TextUtils.isEmpty(serverAddr)) {
            showToast("请先在 App 内连接电脑 IP");
            return;
        }
        executor.execute(new Runnable() {
            @Override
            public void run() {
                JSONObject json = httpGetJson("http://" + serverAddr + "/?action=" + action + "&format=json");
                final boolean ok = (json != null);
                mainHandler.post(new Runnable() {
                    @Override
                    public void run() {
                        if (ok) {
                            consecutiveFailures = 0;
                            if (!TextUtils.isEmpty(toastOnSuccess)) {
                                showToast(toastOnSuccess);
                            }
                            if (refreshAfter) {
                                schedulePoll(380L);
                            }
                        } else {
                            showToast("指令发送失败，请检查电脑是否在线");
                        }
                    }
                });
            }
        });
    }

    private void pollMediaStatus(final boolean force) {
        if (TextUtils.isEmpty(serverAddr)) {
            schedulePoll(4000L);
            return;
        }
        executor.execute(new Runnable() {
            @Override
            public void run() {
                String url = "http://" + serverAddr + "/media_status" + (force ? "?force=1" : "");
                final JSONObject json = httpGetJson(url);
                mainHandler.post(new Runnable() {
                    @Override
                    public void run() {
                        if (!serviceRunning) {
                            return;
                        }
                        if (json != null) {
                            consecutiveFailures = 0;
                            MediaStatus incoming = MediaStatus.fromJson(json);
                            long now = SystemClock.elapsedRealtime();

                            boolean prevPlaying = currentStatus.isPlaying;
                            boolean prevLiked = currentStatus.isLiked;
                            String prevTitle = currentStatus.title != null ? currentStatus.title : "";
                            double prevEstimatedPos = currentStatus.getCurrentPositionSec();

                            if (now < ignorePlayStateUntilMs) {
                                incoming.isPlaying = currentStatus.isPlaying;
                            }
                            if (now < ignoreLikeStateUntilMs) {
                                incoming.isLiked = currentStatus.isLiked;
                            }
                            if (now < ignorePositionUntilMs && currentStatus.hasTimeline && incoming.hasTimeline) {
                                incoming.positionSec = currentStatus.getCurrentPositionSec();
                                incoming.syncedAtElapsedMs = now;
                            }

                            String newTitle = incoming.title != null ? incoming.title : "";
                            if (!prevTitle.equals(newTitle)) {
                                pendingUnlikeConfirmUntilMs = 0L;
                            }

                            boolean positionDrifted = incoming.hasTimeline && Math.abs(incoming.positionSec - prevEstimatedPos) > 2.5;
                            boolean playingChanged = (incoming.isPlaying != prevPlaying);
                            boolean likedChanged = (incoming.isLiked != prevLiked);
                            currentStatus = incoming;

                            publishState(playingChanged || likedChanged || positionDrifted);
                        } else {
                            consecutiveFailures++;
                            if (currentStatus.online) {
                                currentStatus.online = false;
                                publishState(true);
                            }
                        }
                        schedulePoll(nextPollIntervalMs());
                    }
                });
            }
        });
    }

    private void publishState(boolean forceSessionRefresh) {
        latestStatus = currentStatus.copy();
        updateMediaSession(forceSessionRefresh);
        updateNotificationIfNeeded(forceSessionRefresh);

        StatusListener listener = statusListener;
        if (listener != null) {
            listener.onMediaStatusUpdated(latestStatus.copy());
        }
    }

    private void updateMediaSession(boolean force) {
        if (mediaSession == null) {
            return;
        }

        String displayTitle = getDisplayTitle();
        String displayArtist = getDisplaySubtitle();
        long durationMs = currentStatus.getDurationMs();
        String metaKey = displayTitle + "|" + displayArtist + "|" + durationMs + "|" + currentStatus.online;

        if (force || !metaKey.equals(lastMetaKey)) {
            lastMetaKey = metaKey;
            MediaMetadata.Builder meta = new MediaMetadata.Builder()
                    .putString(MediaMetadata.METADATA_KEY_TITLE, displayTitle)
                    .putString(MediaMetadata.METADATA_KEY_ARTIST, displayArtist)
                    .putLong(MediaMetadata.METADATA_KEY_DURATION, durationMs);
            if (!TextUtils.isEmpty(currentStatus.album)) {
                meta.putString(MediaMetadata.METADATA_KEY_ALBUM, currentStatus.album);
            }
            if (largeIcon != null) {
                meta.putBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART, largeIcon);
            }
            mediaSession.setMetadata(meta.build());
        }

        long now = SystemClock.elapsedRealtime();
        if (force || (now - lastPlaybackStateUpdateMs) > 15000L
                || lastNotifPlaying != currentStatus.isPlaying
                || lastNotifLiked != currentStatus.isLiked) {
            lastPlaybackStateUpdateMs = now;
            int state = currentStatus.isPlaying ? PlaybackState.STATE_PLAYING : PlaybackState.STATE_PAUSED;
            long posMs = currentStatus.hasTimeline ? currentStatus.getCurrentPositionMs() : PlaybackState.PLAYBACK_POSITION_UNKNOWN;
            float speed = currentStatus.isPlaying ? (float) currentStatus.playbackRate : 0.0f;

            long actions = PlaybackState.ACTION_PLAY
                    | PlaybackState.ACTION_PAUSE
                    | PlaybackState.ACTION_PLAY_PAUSE
                    | PlaybackState.ACTION_SKIP_TO_PREVIOUS
                    | PlaybackState.ACTION_SKIP_TO_NEXT;
            if (currentStatus.hasTimeline && durationMs > 0L) {
                actions |= PlaybackState.ACTION_SEEK_TO;
            }

            int likeIconRes = currentStatus.isLiked ? R.drawable.ic_media_like : R.drawable.ic_media_like_outline;
            String likeTitle = currentStatus.isLiked ? "已喜欢(双击取消)" : "喜欢";

            PlaybackState.Builder pbBuilder = new PlaybackState.Builder()
                    .setActions(actions)
                    .setState(state, posMs, speed)
                    .addCustomAction(new PlaybackState.CustomAction.Builder(
                            ACTION_LIKE, likeTitle, likeIconRes).build())
                    .addCustomAction(new PlaybackState.CustomAction.Builder(
                            ACTION_STOP, "关闭控制", R.drawable.ic_media_close).build());

            mediaSession.setPlaybackState(pbBuilder.build());
        }
    }

    private void updateNotificationIfNeeded(boolean force) {
        if (notificationManager == null || !foregroundStarted) {
            return;
        }
        if (force || lastNotifPlaying != currentStatus.isPlaying
                || lastNotifLiked != currentStatus.isLiked
                || lastNotifOnline != currentStatus.online) {
            lastNotifPlaying = currentStatus.isPlaying;
            lastNotifLiked = currentStatus.isLiked;
            lastNotifOnline = currentStatus.online;
            notificationManager.notify(NOTIFICATION_ID, buildNotification());
        }
    }

    private String getDisplayTitle() {
        if (!currentStatus.online && consecutiveFailures > 0) {
            return "音乐遥控器 (连接已断开)";
        }
        if (currentStatus.available && !TextUtils.isEmpty(currentStatus.title)) {
            return currentStatus.title;
        }
        return "音乐遥控器 · 已就绪";
    }

    private String getDisplaySubtitle() {
        if (!currentStatus.online && consecutiveFailures > 0) {
            return "请检查电脑服务或点击打开 App 重连";
        }
        if (currentStatus.available) {
            StringBuilder sb = new StringBuilder();
            if (!TextUtils.isEmpty(currentStatus.artist)) {
                sb.append(currentStatus.artist);
            }
            if (!TextUtils.isEmpty(currentStatus.app)) {
                if (sb.length() > 0) sb.append(" · ");
                sb.append(currentStatus.app);
            }
            if (sb.length() > 0) {
                return sb.toString();
            }
        }
        return TextUtils.isEmpty(serverAddr) ? "PC 远程遥控服务" : ("已连接 " + serverAddr);
    }

    private Notification buildNotification() {
        Intent openIntent = new Intent(this, MainActivity.class);
        openIntent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        int piFlags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
        PendingIntent contentPi = PendingIntent.getActivity(this, 0, openIntent, piFlags);

        PendingIntent prevPi = createServiceActionPi(ACTION_PREV, 1);
        PendingIntent playPausePi = createServiceActionPi(ACTION_PLAY_PAUSE, 2);
        PendingIntent nextPi = createServiceActionPi(ACTION_NEXT, 3);
        PendingIntent likePi = createServiceActionPi(ACTION_LIKE, 4);
        PendingIntent stopPi = createServiceActionPi(ACTION_STOP, 5);

        Notification.Builder builder;
        if (Build.VERSION.SDK_INT >= 26) {
            builder = new Notification.Builder(this, CHANNEL_ID);
        } else {
            builder = new Notification.Builder(this);
            builder.setPriority(Notification.PRIORITY_LOW);
        }

        int playPauseIcon = currentStatus.isPlaying ? R.drawable.ic_media_pause : R.drawable.ic_media_play;
        String playPauseLabel = currentStatus.isPlaying ? "暂停" : "播放";
        int likeIcon = currentStatus.isLiked ? R.drawable.ic_media_like : R.drawable.ic_media_like_outline;
        String likeLabel = currentStatus.isLiked ? "已喜欢" : "喜欢";

        Notification.MediaStyle mediaStyle = new Notification.MediaStyle()
                .setMediaSession(mediaSession.getSessionToken())
                .setShowActionsInCompactView(0, 1, 2);

        builder.setSmallIcon(R.drawable.ic_stat_music)
                .setLargeIcon(largeIcon)
                .setContentTitle(getDisplayTitle())
                .setContentText(getDisplaySubtitle())
                .setSubText(currentStatus.online ? "PC 远程遥控" : "等待重连")
                .setContentIntent(contentPi)
                .setVisibility(Notification.VISIBILITY_PUBLIC)
                .setCategory(Notification.CATEGORY_TRANSPORT)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setShowWhen(false)
                .addAction(buildNotificationAction(R.drawable.ic_media_prev, "上一首", prevPi))
                .addAction(buildNotificationAction(playPauseIcon, playPauseLabel, playPausePi))
                .addAction(buildNotificationAction(R.drawable.ic_media_next, "下一首", nextPi))
                .addAction(buildNotificationAction(likeIcon, likeLabel, likePi))
                .addAction(buildNotificationAction(R.drawable.ic_media_close, "关闭", stopPi))
                .setStyle(mediaStyle);

        return builder.build();
    }

    private Notification.Action buildNotificationAction(int iconRes, String title, PendingIntent pi) {
        if (Build.VERSION.SDK_INT >= 23) {
            return new Notification.Action.Builder(Icon.createWithResource(this, iconRes), title, pi).build();
        } else {
            return new Notification.Action.Builder(iconRes, title, pi).build();
        }
    }

    private PendingIntent createServiceActionPi(String action, int requestCode) {
        Intent intent = new Intent(this, MediaControlService.class);
        intent.setAction(action);
        int piFlags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
        if (Build.VERSION.SDK_INT >= 26) {
            return PendingIntent.getForegroundService(this, requestCode, intent, piFlags);
        }
        return PendingIntent.getService(this, requestCode, intent, piFlags);
    }

    private JSONObject httpGetJson(String targetUrl) {
        HttpURLConnection conn = null;
        try {
            URL url = new URL(targetUrl);
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("GET");
            conn.setConnectTimeout(2200);
            conn.setReadTimeout(2200);
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

    private void notifyServiceState(final boolean running) {
        mainHandler.post(new Runnable() {
            @Override
            public void run() {
                StatusListener listener = statusListener;
                if (listener != null) {
                    listener.onServiceStateChanged(running);
                }
            }
        });
    }

    private void showToast(final String msg) {
        mainHandler.post(new Runnable() {
            @Override
            public void run() {
                Toast.makeText(getApplicationContext(), msg, Toast.LENGTH_SHORT).show();
            }
        });
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        mainHandler.removeCallbacks(pollRunnable);
        serviceRunning = false;
        foregroundStarted = false;
        if (mediaSession != null) {
            mediaSession.setActive(false);
            mediaSession.release();
            mediaSession = null;
        }
        executor.shutdownNow();
        notifyServiceState(false);
        super.onDestroy();
    }
}
