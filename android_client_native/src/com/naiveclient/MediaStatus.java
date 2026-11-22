package com.naiveclient;

import android.os.SystemClock;
import org.json.JSONObject;
import java.util.Locale;

public class MediaStatus {
    public boolean online = false;
    public boolean available = false;
    public String title = "";
    public String artist = "";
    public String album = "";
    public String app = "";
    public boolean isPlaying = false;
    public boolean isLiked = false;
    public double positionSec = 0.0;
    public double durationSec = 0.0;
    public boolean hasTimeline = false;
    public boolean canSeek = false;
    public double playbackRate = 1.0;
    public boolean cdpConnected = false;
    public String likeSource = "local";
    public String cdpHint = "";
    public long syncedAtElapsedMs = 0L;

    public static MediaStatus fromJson(JSONObject json) {
        MediaStatus st = new MediaStatus();
        st.online = true;
        st.available = json.optBoolean("available", false);
        st.title = json.optString("title", "");
        st.artist = json.optString("artist", "");
        st.album = json.optString("album", "");
        st.app = json.optString("app", "");
        st.isPlaying = json.optBoolean("is_playing", false);
        st.isLiked = json.optBoolean("is_liked", false);
        st.positionSec = json.optDouble("position", 0.0);
        st.durationSec = json.optDouble("duration", 0.0);
        st.hasTimeline = json.optBoolean("has_timeline", false) && st.durationSec > 0.5;
        st.canSeek = json.optBoolean("can_seek", false) || st.hasTimeline;
        st.playbackRate = json.optDouble("playback_rate", 1.0);
        if (st.playbackRate <= 0.0) {
            st.playbackRate = 1.0;
        }
        st.cdpConnected = json.optBoolean("cdp_connected", false);
        st.likeSource = json.optString("like_source", "local");
        st.cdpHint = json.optString("cdp_hint", "");
        st.syncedAtElapsedMs = SystemClock.elapsedRealtime();
        return st;
    }

    public MediaStatus copy() {
        MediaStatus c = new MediaStatus();
        c.online = this.online;
        c.available = this.available;
        c.title = this.title;
        c.artist = this.artist;
        c.album = this.album;
        c.app = this.app;
        c.isPlaying = this.isPlaying;
        c.isLiked = this.isLiked;
        c.positionSec = this.positionSec;
        c.durationSec = this.durationSec;
        c.hasTimeline = this.hasTimeline;
        c.canSeek = this.canSeek;
        c.playbackRate = this.playbackRate;
        c.cdpConnected = this.cdpConnected;
        c.likeSource = this.likeSource;
        c.cdpHint = this.cdpHint;
        c.syncedAtElapsedMs = this.syncedAtElapsedMs;
        return c;
    }

    public double getCurrentPositionSec() {
        double pos = positionSec;
        if (isPlaying && hasTimeline && durationSec > 0.5 && syncedAtElapsedMs > 0L) {
            double dt = (SystemClock.elapsedRealtime() - syncedAtElapsedMs) / 1000.0;
            if (dt > 0.0) {
                pos += dt * (playbackRate > 0.0 ? playbackRate : 1.0);
            }
        }
        if (durationSec > 0.5 && pos > durationSec) {
            pos = durationSec;
        }
        if (pos < 0.0) {
            pos = 0.0;
        }
        return pos;
    }

    public long getCurrentPositionMs() {
        return (long) Math.round(getCurrentPositionSec() * 1000.0);
    }

    public long getDurationMs() {
        return hasTimeline ? (long) Math.round(durationSec * 1000.0) : 0L;
    }

    public static String formatTime(double seconds) {
        if (Double.isNaN(seconds) || Double.isInfinite(seconds) || seconds < 0) {
            return "00:00";
        }
        long total = (long) Math.floor(seconds);
        long hours = total / 3600;
        long mins = (total % 3600) / 60;
        long secs = total % 60;
        if (hours > 0) {
            return String.format(Locale.US, "%d:%02d:%02d", hours, mins, secs);
        }
        return String.format(Locale.US, "%02d:%02d", mins, secs);
    }
}
