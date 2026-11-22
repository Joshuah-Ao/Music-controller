# coding: utf-8
"""
网易云音乐局域网远程控制器 (NetEase Music Controller)
基于原作者开源代码深度优化：
1. 快捷键完全对齐网易云音乐默认/当前全局快捷键（Ctrl + Alt + 方向键 / P / L）。
2. 彻底去除已废弃的老旧 autopy 库依赖，改用 Windows 原生接口进行底层按键模拟。
3. 原生兼容模式：即使电脑未安装 Flask，也能使用 Python 标准库多线程服务直接秒开运行。
4. 集成 Windows 原生 SMTC (System Media Transport Controls) 媒体会话桥接，
   零第三方依赖实时读取正在播放曲目、播放状态、总时长与精确进度，并支持进度条拖动/快进快退控制。
"""

import atexit
import base64
import ctypes
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

try:
    from ncm_cdp import NetEaseCDPBridge
except ImportError:
    _cur_dir = os.path.abspath(os.path.dirname(__file__))
    if _cur_dir not in sys.path:
        sys.path.insert(0, _cur_dir)
    from ncm_cdp import NetEaseCDPBridge

# Windows 虚拟键码定义 (完全对齐网易云音乐当前全局快捷键)
VK_CONTROL = 0x11   # Ctrl
VK_MENU = 0x12      # Alt
VK_LEFT = 0x25      # 方向左键 (上一首)
VK_UP = 0x26        # 方向上键 (音量加)
VK_RIGHT = 0x27     # 方向右键 (下一首)
VK_DOWN = 0x28      # 方向下键 (音量减)
VK_L = 0x4C         # L 键 (喜欢当前歌曲)
VK_P = 0x50         # P 键 (播放/暂停)
VK_LWIN = 0x5B      # Win 键 (左徽标键)
VK_SPACE = 0x20     # 空格键 (用于切换英文输入法)
VK_X = 0x58         # X 键
VK_U = 0x55         # U 键
VK_S = 0x53         # S 键

# 需设置 EXTENDEDKEY 标志的扩展键
EXTENDED_KEYS = {VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_LWIN}

# 常见播放器进程名到友好名称的映射
APP_FRIENDLY_NAMES = {
    "cloudmusic.exe": "网易云音乐",
    "qqmusic.exe": "QQ音乐",
    "kugou.exe": "酷狗音乐",
    "kwmusic.exe": "酷我音乐",
    "spotify.exe": "Spotify",
    "applemusic.exe": "Apple Music",
    "foobar2000.exe": "Foobar2000",
    "musicbee.exe": "MusicBee",
    "potplayermini64.exe": "PotPlayer",
    "potplayermini.exe": "PotPlayer",
    "vlc.exe": "VLC",
    "chrome.exe": "Chrome 浏览器",
    "msedge.exe": "Edge 浏览器",
    "firefox.exe": "Firefox 浏览器",
    "microsoft.zunemusic": "Windows 媒体播放器",
    "microsoft.media.player": "Windows 媒体播放器",
}

# 持久化 Windows PowerShell 5.1 WinRT SMTC 桥接脚本
# 启动一次常驻内存，后续单次查询/进度跳转耗时仅数毫秒，且无需任何 Python 第三方依赖。
SMTC_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

function Write-JsonLine($obj) {
    $json = $obj | ConvertTo-Json -Compress -Depth 4
    [Console]::Out.WriteLine($json)
    [Console]::Out.Flush()
}

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

    [void][Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
    [void][Windows.Media.Control.GlobalSystemMediaTransportControlsSession, Windows.Media.Control, ContentType=WindowsRuntime]
    [void][Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType=WindowsRuntime]

    function Await-WinRt($WinRtTask, [Type]$ResultType) {
        $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
        $netTask = $asTask.Invoke($null, @($WinRtTask))
        if (-not $netTask.Wait(3000)) {
            throw "WinRT timeout"
        }
        return $netTask.Result
    }

    $mgr = Await-WinRt ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
    if ($null -eq $mgr) {
        Write-JsonLine @{ ready = $false; error = "SMTC manager is null" }
        exit 1
    }
    Write-JsonLine @{ ready = $true }
} catch {
    Write-JsonLine @{ ready = $false; error = $_.Exception.Message }
    exit 1
}

function Get-BestSession {
    $sessions = $null
    $cur = $null
    try { $sessions = $mgr.GetSessions() } catch {}
    try { $cur = $mgr.GetCurrentSession() } catch {}

    if ($null -eq $sessions -or $sessions.Count -eq 0) {
        return $cur
    }

    if ($null -ne $cur) {
        try {
            $pbCur = $cur.GetPlaybackInfo()
            if ($null -ne $pbCur -and [int]$pbCur.PlaybackStatus -eq 4) {
                return $cur
            }
        } catch {}
    }

    foreach ($s in $sessions) {
        try {
            $pb = $s.GetPlaybackInfo()
            if ($null -ne $pb -and [int]$pb.PlaybackStatus -eq 4) {
                return $s
            }
        } catch {}
    }

    if ($null -ne $cur) {
        return $cur
    }
    return $sessions[0]
}

function Read-SessionSnapshot($session) {
    if ($null -eq $session) {
        return @{
            available = $false
            title = ""
            artist = ""
            album = ""
            app_raw = ""
            is_playing = $false
            playback_status = 0
            position = 0.0
            duration = 0.0
            start_time = 0.0
            end_time = 0.0
            has_timeline = $false
            can_seek = $false
            playback_rate = 1.0
        }
    }

    $appRaw = ""
    try { $appRaw = [string]$session.SourceAppUserModelId } catch {}

    $statusInt = 0
    $isPlaying = $false
    $canSeek = $false
    $rate = 1.0
    try {
        $pb = $session.GetPlaybackInfo()
        if ($null -ne $pb) {
            $statusInt = [int]$pb.PlaybackStatus
            $isPlaying = ($statusInt -eq 4)
            if ($null -ne $pb.Controls) {
                $canSeek = [bool]$pb.Controls.IsPlaybackPositionEnabled
            }
            if ($null -ne $pb.PlaybackRate -and [double]$pb.PlaybackRate -gt 0) {
                $rate = [double]$pb.PlaybackRate
            }
        }
    } catch {}

    $title = ""
    $artist = ""
    $album = ""
    try {
        $mp = Await-WinRt ($session.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
        if ($null -ne $mp) {
            if ($null -ne $mp.Title) { $title = [string]$mp.Title }
            if ($null -ne $mp.Artist) { $artist = [string]$mp.Artist }
            if ($null -ne $mp.AlbumTitle) { $album = [string]$mp.AlbumTitle }
        }
    } catch {}

    $startSec = 0.0
    $endSec = 0.0
    $durSec = 0.0
    $posSec = 0.0
    $hasTimeline = $false
    try {
        $tl = $session.GetTimelineProperties()
        if ($null -ne $tl) {
            $startSec = [double]$tl.StartTime.TotalSeconds
            $endSec = [double]$tl.EndTime.TotalSeconds
            $durSec = $endSec - $startSec
            if ($durSec -lt 0) { $durSec = 0.0 }
            $posSec = [double]$tl.Position.TotalSeconds - $startSec
            if ($posSec -lt 0) { $posSec = 0.0 }

            if ($durSec -gt 0.5) {
                $hasTimeline = $true
                if ($isPlaying -and $tl.LastUpdatedTime.Year -gt 2000) {
                    $elapsed = ([DateTimeOffset]::UtcNow - $tl.LastUpdatedTime).TotalSeconds
                    if ($elapsed -gt 0 -and $elapsed -lt ($durSec + 120)) {
                        $posSec = $posSec + ($elapsed * $rate)
                    }
                }
                if ($posSec -gt $durSec) { $posSec = $durSec }
                if ($posSec -lt 0) { $posSec = 0.0 }
            }
        }
    } catch {}

    return @{
        available = $true
        title = $title
        artist = $artist
        album = $album
        app_raw = $appRaw
        is_playing = $isPlaying
        playback_status = $statusInt
        position = [Math]::Round($posSec, 2)
        duration = [Math]::Round($durSec, 2)
        start_time = [Math]::Round($startSec, 2)
        end_time = [Math]::Round($endSec, 2)
        has_timeline = $hasTimeline
        can_seek = $canSeek
        playback_rate = [Math]::Round($rate, 2)
    }
}

$inv = [System.Globalization.CultureInfo]::InvariantCulture

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $line = $line.Trim()
    if ($line -eq '') { continue }
    if ($line -eq 'EXIT') { break }

    try {
        $parts = $line.Split(' ')
        $cmd = $parts[0]

        if ($cmd -eq 'STATUS') {
            $session = Get-BestSession
            $snap = Read-SessionSnapshot $session
            $snap['ok'] = $true
            Write-JsonLine $snap
        }
        elseif ($cmd -eq 'SEEK' -or $cmd -eq 'SEEK_DELTA') {
            $session = Get-BestSession
            if ($null -eq $session) {
                Write-JsonLine @{ ok = $false; supported = $false; position = 0.0; duration = 0.0; message = "未检测到正在运行的媒体播放器" }
                continue
            }

            $snap = Read-SessionSnapshot $session
            $durSec = [double]$snap['duration']
            $startSec = [double]$snap['start_time']
            $curPos = [double]$snap['position']
            $val = 0.0
            if ($parts.Length -gt 1) {
                $val = [double]::Parse($parts[1], $inv)
            }

            if ($cmd -eq 'SEEK_DELTA') {
                if ($durSec -le 0.5) {
                    $fallbackOk = $false
                    try {
                        if ($val -gt 0) {
                            $fallbackOk = Await-WinRt ($session.TryFastForwardAsync()) ([bool])
                        } elseif ($val -lt 0) {
                            $fallbackOk = Await-WinRt ($session.TryRewindAsync()) ([bool])
                        }
                    } catch {}
                    if ($fallbackOk) {
                        Write-JsonLine @{ ok = $true; supported = $true; position = 0.0; duration = 0.0; message = "已发送快进/快退指令" }
                    } else {
                        Write-JsonLine @{ ok = $false; supported = $false; position = 0.0; duration = 0.0; message = "当前播放器未开放系统进度控制 (SMTC)" }
                    }
                    continue
                }
                $targetSec = $curPos + $val
            } else {
                $targetSec = $val
            }

            if ($targetSec -lt 0) { $targetSec = 0.0 }
            if ($durSec -gt 0.5 -and $targetSec -gt $durSec) { $targetSec = $durSec }

            $absSec = $startSec + $targetSec
            $ticks = [long][Math]::Round($absSec * 10000000.0)
            $seekOk = $false
            try {
                $seekOk = Await-WinRt ($session.TryChangePlaybackPositionAsync($ticks)) ([bool])
            } catch {
                $seekOk = $false
            }

            if ($seekOk) {
                Write-JsonLine @{
                    ok = $true
                    supported = $true
                    position = [Math]::Round($targetSec, 2)
                    duration = [Math]::Round($durSec, 2)
                    message = "进度调整成功"
                }
            } else {
                Write-JsonLine @{
                    ok = $false
                    supported = $false
                    position = [Math]::Round($curPos, 2)
                    duration = [Math]::Round($durSec, 2)
                    message = "当前播放器暂不支持外部拖动进度条 (SMTC)"
                }
            }
        }
        else {
            Write-JsonLine @{ ok = $false; error = "Unknown command" }
        }
    } catch {
        Write-JsonLine @{ ok = $false; error = $_.Exception.Message }
    }
}
"""


def _format_mmss(seconds):
    try:
        total = max(0, int(round(float(seconds))))
    except Exception:
        total = 0
    mins, secs = divmod(total, 60)
    if mins >= 60:
        hours, mins = divmod(mins, 60)
        return f"{hours:d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def _friendly_app_name(app_raw):
    if not app_raw:
        return ""
    low = app_raw.lower()
    for key, name in APP_FRIENDLY_NAMES.items():
        if key in low:
            return name
    base = app_raw.replace("\\", "/").split("/")[-1]
    if "!" in base:
        base = base.split("!")[0]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base


def _get_netease_window_title():
    """回退方案：直接读取网易云音乐主窗口标题（如 '晴天 - 周杰伦'）"""
    if sys.platform != "win32":
        return "", ""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW("OrpheusBrowserHost", None)
        if not hwnd:
            return "", ""
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return "", ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        raw = buf.value.strip()
        if not raw or raw in ("网易云音乐", "NetEase Cloud Music"):
            return "", ""
        if " - " in raw:
            title, artist = raw.rsplit(" - ", 1)
            return title.strip(), artist.strip()
        return raw, ""
    except Exception:
        return "", ""


class LikeStateStore(object):
    """按曲目记录喜欢状态，用于多端同步与取消喜欢二次确认"""

    _lock = threading.Lock()
    _states = {}
    _current_key = "__unknown__"
    _max_entries = 500

    @classmethod
    def make_key(cls, title, artist):
        t = (title or "").strip()
        a = (artist or "").strip()
        if t:
            return f"{t}||{a}"
        return "__unknown__"

    @classmethod
    def update_current_key(cls, key):
        with cls._lock:
            cls._current_key = key or "__unknown__"

    @classmethod
    def get_current_key(cls):
        with cls._lock:
            return cls._current_key

    @classmethod
    def get_liked(cls, key=None):
        with cls._lock:
            k = key if key is not None else cls._current_key
            return bool(cls._states.get(k, False))

    @classmethod
    def set_liked(cls, liked, key=None):
        with cls._lock:
            k = key if key is not None else cls._current_key
            if len(cls._states) >= cls._max_entries and k not in cls._states:
                oldest = next(iter(cls._states))
                del cls._states[oldest]
            cls._states[k] = bool(liked)
            return bool(liked)

    @classmethod
    def reset_unknown(cls):
        with cls._lock:
            cls._states["__unknown__"] = False


class SMTCBridge(object):
    """Windows SMTC 持久化媒体会话桥接器（线程安全、自带缓存与异常自动恢复）"""

    _lock = threading.Lock()
    _proc = None
    _ready = False
    _last_fail_time = 0.0
    _retry_cooldown = 15.0
    _cache_status = None
    _cache_time = 0.0
    _cache_ttl = 0.35

    @classmethod
    def start_async(cls):
        if sys.platform != "win32":
            return
        t = threading.Thread(target=cls._ensure_worker, daemon=True)
        t.start()

    @classmethod
    def _terminate_locked(cls):
        proc = cls._proc
        cls._proc = None
        cls._ready = False
        if proc is not None:
            try:
                if proc.poll() is None:
                    try:
                        proc.stdin.write(b"EXIT\n")
                        proc.stdin.flush()
                    except Exception:
                        pass
                    proc.terminate()
            except Exception:
                pass

    @classmethod
    def shutdown(cls):
        with cls._lock:
            cls._terminate_locked()

    @classmethod
    def invalidate_cache(cls):
        with cls._lock:
            cls._cache_time = 0.0

    @classmethod
    def _ensure_worker_locked(cls):
        if sys.platform != "win32":
            return False

        if cls._proc is not None and cls._proc.poll() is None and cls._ready:
            return True

        now = time.monotonic()
        if cls._last_fail_time > 0 and (now - cls._last_fail_time) < cls._retry_cooldown:
            return False

        cls._terminate_locked()

        try:
            encoded = base64.b64encode(SMTC_PS_SCRIPT.encode("utf-16-le")).decode("ascii")
            ps_exe = os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"),
                "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
            )
            if not os.path.exists(ps_exe):
                ps_exe = "powershell.exe"

            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                [ps_exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )

            ready_line = proc.stdout.readline()
            if not ready_line:
                cls._last_fail_time = time.monotonic()
                try:
                    proc.terminate()
                except Exception:
                    pass
                return False

            info = json.loads(ready_line.decode("utf-8", errors="replace").strip())
            if info.get("ready"):
                cls._proc = proc
                cls._ready = True
                cls._last_fail_time = 0.0
                return True

            cls._last_fail_time = time.monotonic()
            try:
                proc.terminate()
            except Exception:
                pass
            return False
        except Exception:
            cls._last_fail_time = time.monotonic()
            cls._terminate_locked()
            return False

    @classmethod
    def _ensure_worker(cls):
        with cls._lock:
            return cls._ensure_worker_locked()

    @classmethod
    def _send_cmd_locked(cls, cmd_line):
        if not cls._ensure_worker_locked():
            return None
        try:
            cls._proc.stdin.write((cmd_line + "\n").encode("utf-8"))
            cls._proc.stdin.flush()
            resp_bytes = cls._proc.stdout.readline()
            if not resp_bytes:
                cls._terminate_locked()
                cls._last_fail_time = time.monotonic()
                return None
            return json.loads(resp_bytes.decode("utf-8", errors="replace").strip())
        except Exception:
            cls._terminate_locked()
            cls._last_fail_time = time.monotonic()
            return None

    @classmethod
    def get_status(cls, force=False):
        with cls._lock:
            now = time.monotonic()
            if not force and cls._cache_status is not None and (now - cls._cache_time) < cls._cache_ttl:
                cached = dict(cls._cache_status)
                # 缓存命中时轻微推演播放进度，保证高频查询丝滑
                if cached.get("is_playing") and cached.get("has_timeline") and cached.get("duration", 0) > 0:
                    dt = now - cls._cache_time
                    rate = cached.get("playback_rate", 1.0) or 1.0
                    pos = min(cached["duration"], cached["position"] + dt * rate)
                    cached["position"] = round(pos, 2)
                cached["is_liked"] = LikeStateStore.get_liked(
                    LikeStateStore.make_key(cached.get("title"), cached.get("artist"))
                )
                cached["server_time"] = round(time.time(), 3)
                return cached

            res = cls._send_cmd_locked("STATUS")

        if res and res.get("ok"):
            title = (res.get("title") or "").strip()
            artist = (res.get("artist") or "").strip()
            album = (res.get("album") or "").strip()
            app_raw = (res.get("app_raw") or "").strip()
            app_name = _friendly_app_name(app_raw)
            available = bool(res.get("available"))

            # 若 SMTC 未返回曲名，尝试从网易云音乐主窗口标题兜底
            if not title:
                fb_title, fb_artist = _get_netease_window_title()
                if fb_title:
                    title = fb_title
                    artist = artist or fb_artist
                    app_name = app_name or "网易云音乐"
                    available = True

            track_key = LikeStateStore.make_key(title, artist)
            LikeStateStore.update_current_key(track_key)
            status = {
                "ok": True,
                "available": available,
                "title": title,
                "artist": artist,
                "album": album,
                "app": app_name,
                "app_raw": app_raw,
                "is_playing": bool(res.get("is_playing")),
                "is_liked": LikeStateStore.get_liked(track_key),
                "position": float(res.get("position") or 0.0),
                "duration": float(res.get("duration") or 0.0),
                "has_timeline": bool(res.get("has_timeline")),
                "can_seek": bool(res.get("can_seek")) or bool(res.get("has_timeline")),
                "playback_rate": float(res.get("playback_rate") or 1.0),
                "server_time": round(time.time(), 3),
            }
        else:
            fb_title, fb_artist = _get_netease_window_title()
            track_key = LikeStateStore.make_key(fb_title, fb_artist)
            LikeStateStore.update_current_key(track_key)
            status = {
                "ok": True,
                "available": bool(fb_title),
                "title": fb_title,
                "artist": fb_artist,
                "album": "",
                "app": "网易云音乐" if fb_title else "",
                "app_raw": "",
                "is_playing": False,
                "is_liked": LikeStateStore.get_liked(track_key),
                "position": 0.0,
                "duration": 0.0,
                "has_timeline": False,
                "can_seek": False,
                "playback_rate": 1.0,
                "server_time": round(time.time(), 3),
            }

        with cls._lock:
            cls._cache_status = dict(status)
            cls._cache_time = time.monotonic()

        return status

    @classmethod
    def seek_to(cls, position_sec):
        try:
            pos = max(0.0, float(position_sec))
        except (TypeError, ValueError):
            return {"ok": False, "supported": False, "message": "无效的进度参数"}

        with cls._lock:
            cls._cache_time = 0.0
            res = cls._send_cmd_locked(f"SEEK {pos:.3f}")

        if not res:
            return {
                "ok": False,
                "supported": False,
                "position": 0.0,
                "duration": 0.0,
                "message": "系统媒体控制服务 (SMTC) 暂不可用",
            }

        ok = bool(res.get("ok"))
        new_pos = float(res.get("position") or 0.0)
        duration = float(res.get("duration") or 0.0)
        if ok:
            msg = f"已跳转至 {_format_mmss(new_pos)}"
            print(f"[操作] 拖动进度条 -> 跳转至 {_format_mmss(new_pos)} / {_format_mmss(duration)}", flush=True)
        else:
            msg = res.get("message") or "当前播放器不支持进度条拖动"
            print(f"[提示] 进度条跳转未生效: {msg}", flush=True)

        return {
            "ok": ok,
            "supported": bool(res.get("supported")),
            "position": new_pos,
            "duration": duration,
            "message": msg,
        }

    @classmethod
    def seek_delta(cls, delta_sec):
        try:
            delta = float(delta_sec)
        except (TypeError, ValueError):
            return {"ok": False, "supported": False, "message": "无效的跳转步长"}

        with cls._lock:
            cls._cache_time = 0.0
            res = cls._send_cmd_locked(f"SEEK_DELTA {delta:.3f}")

        if not res:
            return {
                "ok": False,
                "supported": False,
                "position": 0.0,
                "duration": 0.0,
                "message": "系统媒体控制服务 (SMTC) 暂不可用",
            }

        ok = bool(res.get("ok"))
        new_pos = float(res.get("position") or 0.0)
        duration = float(res.get("duration") or 0.0)
        sign = "+" if delta >= 0 else ""
        if ok:
            if duration > 0:
                msg = f"进度 {sign}{int(round(delta))}s ({_format_mmss(new_pos)})"
            else:
                msg = f"进度 {sign}{int(round(delta))}s"
            print(f"[操作] 进度微调 ({sign}{int(round(delta))}s) -> {_format_mmss(new_pos)}", flush=True)
        else:
            msg = res.get("message") or "当前播放器不支持快进/快退"
            print(f"[提示] 进度微调未生效: {msg}", flush=True)

        return {
            "ok": ok,
            "supported": bool(res.get("supported")),
            "position": new_pos,
            "duration": duration,
            "message": msg,
        }


atexit.register(SMTCBridge.shutdown)


class MediaHub(object):
    """统一媒体状态与进度调度中心：优先网易云 CDP（精准进度拖动 + 真实红心），自动回退 Windows SMTC"""

    @classmethod
    def invalidate_cache(cls):
        SMTCBridge.invalidate_cache()
        NetEaseCDPBridge.invalidate_cache()

    @classmethod
    def _is_other_smtc_active(cls, smtc):
        if not smtc or not smtc.get("available") or not smtc.get("is_playing"):
            return False
        app_raw = (smtc.get("app_raw") or "").lower()
        app_name = smtc.get("app") or ""
        return ("cloudmusic" not in app_raw) and (app_name != "网易云音乐")

    @classmethod
    def get_status(cls, force=False):
        smtc = SMTCBridge.get_status(force=force)
        cdp = NetEaseCDPBridge.get_status(force=force)
        other_active = cls._is_other_smtc_active(smtc)

        if cdp and cdp.get("ok") and not other_active:
            title = (cdp.get("title") or "").strip() or (smtc.get("title") or "").strip()
            artist = (cdp.get("artist") or "").strip() or (smtc.get("artist") or "").strip()
            album = (smtc.get("album") or "").strip()
            track_key = LikeStateStore.make_key(title, artist)
            LikeStateStore.update_current_key(track_key)

            cdp_playing = cdp.get("is_playing")
            is_playing = bool(cdp_playing) if isinstance(cdp_playing, bool) else bool(smtc.get("is_playing"))

            if cdp.get("has_timeline") and float(cdp.get("duration") or 0.0) > 0.5:
                position = float(cdp.get("position") or 0.0)
                duration = float(cdp.get("duration") or 0.0)
                has_timeline = True
                can_seek = True
            else:
                position = float(smtc.get("position") or 0.0)
                duration = float(smtc.get("duration") or 0.0)
                has_timeline = bool(smtc.get("has_timeline"))
                can_seek = bool(smtc.get("can_seek")) or bool(cdp.get("can_seek"))

            if cdp.get("like_found") and isinstance(cdp.get("is_liked"), bool):
                is_liked = bool(cdp["is_liked"])
                like_source = "cdp"
                LikeStateStore.set_liked(is_liked, track_key)
            else:
                is_liked = LikeStateStore.get_liked(track_key)
                like_source = "local"

            return {
                "ok": True,
                "available": bool(title or has_timeline or cdp.get("has_player") or smtc.get("available")),
                "title": title,
                "artist": artist,
                "album": album,
                "app": "网易云音乐",
                "app_raw": "cloudmusic.exe",
                "is_playing": is_playing,
                "is_liked": is_liked,
                "like_source": like_source,
                "position": round(position, 2),
                "duration": round(duration, 2),
                "has_timeline": has_timeline,
                "can_seek": can_seek,
                "playback_rate": 1.0,
                "status_source": "cdp",
                "cdp_connected": True,
                "cdp_hint": "",
                "server_time": round(time.time(), 3),
            }

        # 回退至 Windows SMTC
        out = dict(smtc)
        app_raw = (out.get("app_raw") or "").lower()
        is_netease = ("cloudmusic" in app_raw) or (out.get("app") == "网易云音乐") or (not out.get("available"))
        out["like_source"] = "local"
        out["status_source"] = "smtc"
        out["cdp_connected"] = NetEaseCDPBridge.is_connected()
        out["cdp_hint"] = NetEaseCDPBridge.get_hint() if (not out.get("has_timeline") and is_netease) else ""
        return out

    @classmethod
    def seek_to(cls, position_sec):
        try:
            pos = max(0.0, float(position_sec))
        except (TypeError, ValueError):
            return {"ok": False, "supported": False, "message": "无效的进度参数"}

        smtc = SMTCBridge.get_status(force=False)
        if not cls._is_other_smtc_active(smtc):
            cdp_res = NetEaseCDPBridge.seek_to(pos)
            if cdp_res and cdp_res.get("ok"):
                new_pos = float(cdp_res.get("position") or pos)
                dur = float(cdp_res.get("duration") or 0.0)
                method = cdp_res.get("method", "cdp")
                print(f"[操作] 拖动进度条 (CDP/{method}) -> 跳转至 {_format_mmss(new_pos)} / {_format_mmss(dur)}", flush=True)
                SMTCBridge.invalidate_cache()
                return {
                    "ok": True,
                    "supported": True,
                    "source": "cdp",
                    "position": round(new_pos, 2),
                    "duration": round(dur, 2),
                    "message": f"已跳转至 {_format_mmss(new_pos)}",
                }
            if cdp_res and not cdp_res.get("ok") and NetEaseCDPBridge.is_connected():
                smtc_fallback = SMTCBridge.seek_to(pos)
                if smtc_fallback.get("ok"):
                    return smtc_fallback
                return {
                    "ok": False,
                    "supported": False,
                    "source": "cdp",
                    "position": 0.0,
                    "duration": 0.0,
                    "message": cdp_res.get("message") or "进度拖动未生效",
                }

        return SMTCBridge.seek_to(pos)

    @classmethod
    def seek_delta(cls, delta_sec):
        try:
            delta = float(delta_sec)
        except (TypeError, ValueError):
            return {"ok": False, "supported": False, "message": "无效的跳转步长"}

        smtc = SMTCBridge.get_status(force=False)
        if not cls._is_other_smtc_active(smtc):
            cdp_res = NetEaseCDPBridge.seek_delta(delta)
            if cdp_res and cdp_res.get("ok"):
                new_pos = float(cdp_res.get("position") or 0.0)
                dur = float(cdp_res.get("duration") or 0.0)
                sign = "+" if delta >= 0 else ""
                method = cdp_res.get("method", "cdp")
                print(f"[操作] 进度微调 (CDP/{method}, {sign}{int(round(delta))}s) -> {_format_mmss(new_pos)}", flush=True)
                SMTCBridge.invalidate_cache()
                return {
                    "ok": True,
                    "supported": True,
                    "source": "cdp",
                    "position": round(new_pos, 2),
                    "duration": round(dur, 2),
                    "message": f"进度 {sign}{int(round(delta))}s ({_format_mmss(new_pos)})",
                }

        return SMTCBridge.seek_delta(delta)


class MusicBox(object):
    """网易云音乐及通用媒体播放器按键控制类"""

    _key_lock = threading.Lock()

    ALLOWED_ACTIONS = {
        "next_song",
        "prev_song",
        "pause_play",
        "volume_up",
        "volume_down",
        "like_song",
        "unlike_song",
        "seek_back",
        "seek_forward",
        "sleep",
        "shutdown",
    }

    @classmethod
    def _press_keys(cls, keys):
        if sys.platform != "win32":
            return
        with cls._key_lock:
            user32 = ctypes.windll.user32
            KEYEVENTF_EXTENDEDKEY = 0x0001
            KEYEVENTF_KEYUP = 0x0002

            # 依次按下所有组合键
            for vk in keys:
                flags = KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_KEYS else 0
                user32.keybd_event(vk, 0, flags, 0)
                time.sleep(0.02)

            time.sleep(0.05)

            # 倒序释放所有组合键
            for vk in reversed(keys):
                flags = KEYEVENTF_KEYUP
                if vk in EXTENDED_KEYS:
                    flags |= KEYEVENTF_EXTENDEDKEY
                user32.keybd_event(vk, 0, flags, 0)
                time.sleep(0.02)

    @classmethod
    def next_song(cls):
        print("[操作] 下一首 -> 模拟按键: Ctrl + Alt + Right", flush=True)
        cls._press_keys((VK_CONTROL, VK_MENU, VK_RIGHT))
        LikeStateStore.reset_unknown()
        MediaHub.invalidate_cache()

    @classmethod
    def prev_song(cls):
        print("[操作] 上一首 -> 模拟按键: Ctrl + Alt + Left", flush=True)
        cls._press_keys((VK_CONTROL, VK_MENU, VK_LEFT))
        LikeStateStore.reset_unknown()
        MediaHub.invalidate_cache()

    @classmethod
    def pause_play(cls):
        print("[操作] 播放/暂停 -> 模拟按键: Ctrl + Alt + P", flush=True)
        cls._press_keys((VK_CONTROL, VK_MENU, VK_P))
        MediaHub.invalidate_cache()

    @classmethod
    def volume_up(cls):
        print("[操作] 音量加 -> 模拟按键: Ctrl + Alt + Up", flush=True)
        cls._press_keys((VK_CONTROL, VK_MENU, VK_UP))

    @classmethod
    def volume_down(cls):
        print("[操作] 音量减 -> 模拟按键: Ctrl + Alt + Down", flush=True)
        cls._press_keys((VK_CONTROL, VK_MENU, VK_DOWN))

    @classmethod
    def like_song(cls):
        cdp = NetEaseCDPBridge.get_status(force=True)
        if cdp and cdp.get("like_found") and cdp.get("is_liked") is True:
            LikeStateStore.set_liked(True)
            print("[提示] 网易云真实状态已为红心喜欢，跳过重复按键防误取消。", flush=True)
            return {"ok": True, "action": "like_song", "is_liked": True, "like_source": "cdp", "already": True}
        print("[操作] 喜欢当前歌曲 -> 模拟按键: Ctrl + Alt + L", flush=True)
        cls._press_keys((VK_CONTROL, VK_MENU, VK_L))
        LikeStateStore.set_liked(True)
        MediaHub.invalidate_cache()
        return {"ok": True, "action": "like_song", "is_liked": True}

    @classmethod
    def unlike_song(cls):
        cdp = NetEaseCDPBridge.get_status(force=True)
        if cdp and cdp.get("like_found") and cdp.get("is_liked") is False:
            LikeStateStore.set_liked(False)
            print("[提示] 网易云真实状态已为未喜欢，跳过重复按键防误收藏。", flush=True)
            return {"ok": True, "action": "unlike_song", "is_liked": False, "like_source": "cdp", "already": True}
        print("[操作] 取消喜欢当前歌曲 -> 模拟按键: Ctrl + Alt + L", flush=True)
        cls._press_keys((VK_CONTROL, VK_MENU, VK_L))
        LikeStateStore.set_liked(False)
        MediaHub.invalidate_cache()
        return {"ok": True, "action": "unlike_song", "is_liked": False}

    @classmethod
    def seek_back(cls):
        return MediaHub.seek_delta(-10.0)

    @classmethod
    def seek_forward(cls):
        return MediaHub.seek_delta(10.0)

    @classmethod
    def sleep(cls):
        """一键让电脑进入睡眠：先 Win+Space 切英文输入法，再 Win+X -> U -> S"""
        print("[操作] 收到一键睡眠指令：先切英文状态，再通过 Win+X > U > S 睡眠...", flush=True)
        # 1. 先 Win + Space 切换英文输入状态，避免中文输入法拦截快捷键
        cls._press_keys((VK_LWIN, VK_SPACE))
        time.sleep(0.3)

        # 2. 按下 Win + X 唤出系统管理菜单
        cls._press_keys((VK_LWIN, VK_X))
        time.sleep(0.4)

        # 3. 按下 U 展开关机与睡眠子菜单
        cls._press_keys((VK_U,))
        time.sleep(0.3)

        # 4. 按下 S 触发系统睡眠
        cls._press_keys((VK_S,))

    @classmethod
    def shutdown(cls):
        print("[操作] 收到关机指令，将在 5 秒后关机...", flush=True)
        os.system("shutdown -s -t 5")

    @classmethod
    def dispatch(cls, action):
        if action and action in cls.ALLOWED_ACTIONS and hasattr(cls, action):
            return getattr(cls, action)()
        return None


def _is_private_ipv4(ip):
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b, c, d = (int(x) for x in parts)
    except ValueError:
        return False
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    return False


def get_lan_candidates():
    """
    智能扫描真实局域网 IPv4 地址（自动过滤 Clash/Xray TUN、VPN、VMware/Hyper-V/WSL 等虚拟网卡）。
    返回按推荐度降序排列的 [(ip, adapter_name), ...]
    """
    virtual_keywords = (
        "vmware", "vmnet", "virtual", "vbox", "vethernet", "wsl", "hyper-v",
        "tun", "tap", "xray", "clash", "mihomo", "sing-box", "wireguard",
        "openvpn", "radmin", "zerotier", "tailscale", "hamachi", "privado",
        "loopback", "bluetooth", "蓝牙", "wintun", "utun", "vpn", "meta",
    )
    physical_keywords = (
        "wlan", "wi-fi", "wifi", "wireless", "无线局域网", "无线",
        "ethernet", "以太网", "本地连接",
    )

    candidates = []
    if sys.platform == "win32":
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            raw = subprocess.check_output(["ipconfig"], creationflags=creationflags, stderr=subprocess.DEVNULL)
            text = None
            for enc in ("mbcs", "gbk", "utf-8"):
                try:
                    text = raw.decode(enc)
                    break
                except Exception:
                    continue
            if text is None:
                text = raw.decode("utf-8", errors="ignore")

            current_adapter = ""
            current_ip = ""
            current_mask = ""
            has_real_gw = False

            def flush_adapter():
                if not current_ip or current_ip.startswith("127.") or current_ip.startswith("169.254."):
                    return
                low = current_adapter.lower()
                is_virtual = any(k in low for k in virtual_keywords) or ("unknown adapter" in low) or ("未知适配器" in low)
                if current_ip.startswith(("198.18.", "198.19.")):
                    is_virtual = True
                if current_mask == "255.255.255.252":  # /30 常见于 TUN 代理虚拟网卡
                    is_virtual = True

                score = 0
                if is_virtual:
                    score -= 100
                if any(k in low for k in physical_keywords):
                    score += 50
                if has_real_gw:
                    score += 40
                if _is_private_ipv4(current_ip):
                    score += 25
                if current_ip.startswith("192.168.") or current_ip.startswith("172.20.10."):
                    score += 10
                candidates.append((score, current_ip, current_adapter.strip()))

            for line in text.splitlines():
                if not line.strip():
                    continue
                if not line[0].isspace() and line.rstrip().endswith(":"):
                    flush_adapter()
                    current_adapter = line.rstrip().rstrip(":")
                    current_ip = ""
                    current_mask = ""
                    has_real_gw = False
                    continue

                stripped = line.strip()
                low_line = stripped.lower()
                if "ipv4" in low_line:
                    m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", stripped)
                    if m:
                        current_ip = m.group(1)
                elif "subnet mask" in low_line or "子网掩码" in stripped:
                    m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", stripped)
                    if m:
                        current_mask = m.group(1)
                elif "default gateway" in low_line or "默认网关" in stripped:
                    m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", stripped)
                    if m and m.group(1) != "0.0.0.0":
                        has_real_gw = True
                elif current_ip and not has_real_gw:
                    # 处理默认网关换行打印第二行 IPv4 的情况（如先打印 IPv6 网关再下一行打印 IPv4 网关）
                    m = re.match(r"^(\d{1,3}(?:\.\d{1,3}){3})$", stripped)
                    if m and m.group(1) != "0.0.0.0":
                        has_real_gw = True

            flush_adapter()
        except Exception:
            pass

    # 优先返回非虚拟网卡（score >= 0），按推荐度降序去重
    candidates.sort(key=lambda x: x[0], reverse=True)
    seen = set()
    real_list = []
    all_list = []
    for score, ip, name in candidates:
        if ip in seen:
            continue
        seen.add(ip)
        all_list.append((ip, name))
        if score >= 0:
            real_list.append((ip, name))

    if real_list:
        return real_list

    # 兜底：使用 UDP 路由探测
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return [(ip, "默认路由")]
    except Exception:
        pass

    return all_list if all_list else [("127.0.0.1", "本机回环")]


def get_local_ip():
    """获取本机最适合手机连接的真实局域网 IP（自动避开代理 TUN 与虚拟网卡）"""
    return get_lan_candidates()[0][0]


def get_resource_dir():
    """获取资源文件所在目录（兼顾源码运行与 PyInstaller 打包后的环境）"""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        if os.path.exists(os.path.join(exe_dir, "templates")):
            return exe_dir
        if hasattr(sys, "_MEIPASS"):
            return sys._MEIPASS
        return exe_dir
    return os.path.abspath(os.path.dirname(__file__))


def run():
    project_root = get_resource_dir()
    template_path = os.path.join(project_root, "templates", "index.html")
    template = (
        open(template_path, "r", encoding="utf-8").read()
        if os.path.exists(template_path)
        else "<h1>网易云音乐远程控制器</h1>"
    )

    # 后台预热 Windows SMTC 与网易云 CDP 桥接器（零打扰，不阻塞 HTTP 服务秒开）
    SMTCBridge.start_async()
    NetEaseCDPBridge.start_async()

    port = 10010
    lan_candidates = get_lan_candidates()
    local_ip = lan_candidates[0][0]

    print("=" * 60, flush=True)
    print(" 网易云音乐远程控制服务已就绪！(v1.4)", flush=True)
    print(f" 本机局域网 IP: {local_ip} ({lan_candidates[0][1]})", flush=True)
    print(f" 电脑访问地址: http://127.0.0.1:{port}/", flush=True)
    print(f" 手机访问地址: http://{local_ip}:{port}/", flush=True)
    if len(lan_candidates) > 1:
        for extra_ip, extra_name in lan_candidates[1:4]:
            print(f" 备用局域网地址: http://{extra_ip}:{port}/ ({extra_name})", flush=True)
    print(" 当前生效的快捷键与多媒体能力：", flush=True)
    print("   - 播放/暂停: Ctrl + Alt + P", flush=True)
    print("   - 上一首:     Ctrl + Alt + Left", flush=True)
    print("   - 下一首:     Ctrl + Alt + Right", flush=True)
    print("   - 音量加:     Ctrl + Alt + Up", flush=True)
    print("   - 音量减:     Ctrl + Alt + Down", flush=True)
    print("   - 喜欢歌曲:   Ctrl + Alt + L (含取消喜欢二次确认)", flush=True)
    print("   - 进度与红心: 网易云 CDP(9222) 优先 + Windows SMTC 自动回退", flush=True)
    print(f"   - CDP 诊断页: http://127.0.0.1:{port}/cdp_debug", flush=True)
    print("=" * 60, flush=True)
    print("提示：在手机浏览器输入上述手机访问地址，或使用安卓客户端（支持状态栏与锁屏控制）。", flush=True)
    print("按 Ctrl + C 可退出服务。\n", flush=True)

    # 优先使用 Flask；若未安装 Flask 则自动使用 Python 原生内置多线程服务（零安装依赖）
    try:
        from flask import Flask, jsonify, request

        app = Flask(__name__)

        # 过滤高频 /media_status 轮询日志，保持终端整洁
        class _QuietStatusFilter(logging.Filter):
            def filter(self, record):
                msg = record.getMessage()
                return "/media_status" not in msg

        logging.getLogger("werkzeug").addFilter(_QuietStatusFilter())

        @app.after_request
        def add_headers(resp):
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Cache-Control"] = "no-store"
            return resp

        @app.route("/")
        def hello_world():
            action = request.args.get("action")
            fmt = request.args.get("format")
            if action:
                result = MusicBox.dispatch(action)
                if fmt == "json":
                    if isinstance(result, dict):
                        return jsonify(result)
                    return jsonify(ok=True, action=action)
            return template

        @app.route("/mobile_connect")
        def mobile_connect():
            response = jsonify(
                code=200,
                message="Connected",
                platform="win",
                status=1,
                version="1.4.0",
                features=["like_song", "unlike_confirm", "media_status", "media_seek", "ncm_cdp"],
            )
            response.status_code = 200
            return response

        @app.route("/like_state")
        def like_state():
            liked_param = request.args.get("liked")
            if liked_param is not None:
                val = liked_param in ("1", "true", "True")
                LikeStateStore.set_liked(val)
                MediaHub.invalidate_cache()
            return jsonify(ok=True, is_liked=LikeStateStore.get_liked())

        @app.route("/media_status")
        def media_status():
            force = request.args.get("force") == "1"
            return jsonify(MediaHub.get_status(force=force))

        @app.route("/media_seek")
        def media_seek():
            pos = request.args.get("position")
            delta = request.args.get("delta")
            if pos is not None:
                return jsonify(MediaHub.seek_to(pos))
            if delta is not None:
                return jsonify(MediaHub.seek_delta(delta))
            return jsonify(ok=False, supported=False, message="缺少 position 或 delta 参数")

        @app.route("/cdp_debug")
        def cdp_debug():
            return jsonify(NetEaseCDPBridge.get_debug_info())

        app.run(host="0.0.0.0", port=port, threaded=True)

    except ImportError:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        try:
            from http.server import ThreadingHTTPServer
        except ImportError:
            from socketserver import ThreadingMixIn

            class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
                daemon_threads = True

        class NativeHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                first = str(args[0]) if args else ""
                if "/media_status" in first:
                    return
                print(f"[{self.log_date_time_string()}] {first}", flush=True)

            def _send_json(self, data, status_code=200):
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)

                # 移动端握手接口
                if path == "/mobile_connect":
                    self._send_json(
                        {
                            "code": 200,
                            "message": "Connected",
                            "platform": "win",
                            "status": 1,
                            "version": "1.4.0",
                            "features": ["like_song", "unlike_confirm", "media_status", "media_seek", "ncm_cdp"],
                        }
                    )
                    return

                # 仅同步喜欢标记状态（不发送快捷键）
                if path == "/like_state":
                    liked_param = query.get("liked", [None])[0]
                    if liked_param is not None:
                        val = liked_param in ("1", "true", "True")
                        LikeStateStore.set_liked(val)
                        MediaHub.invalidate_cache()
                    self._send_json({"ok": True, "is_liked": LikeStateStore.get_liked()})
                    return

                # CDP 调试诊断接口
                if path == "/cdp_debug":
                    self._send_json(NetEaseCDPBridge.get_debug_info())
                    return

                # 实时媒体状态接口（供进度条、锁屏、状态栏及真实红心状态同步）
                if path == "/media_status":
                    force = query.get("force", ["0"])[0] == "1"
                    self._send_json(MediaHub.get_status(force=force))
                    return

                # 播放进度条控制接口（优先网易云 CDP，自动回退 SMTC）
                if path == "/media_seek":
                    pos = query.get("position", [None])[0]
                    delta = query.get("delta", [None])[0]
                    if pos is not None:
                        self._send_json(MediaHub.seek_to(pos))
                    elif delta is not None:
                        self._send_json(MediaHub.seek_delta(delta))
                    else:
                        self._send_json({"ok": False, "supported": False, "message": "缺少 position 或 delta 参数"})
                    return

                # 静态资源 (bootstrap.min.css)
                if path.startswith("/static/"):
                    fpath = os.path.join(project_root, path.lstrip("/"))
                    if os.path.exists(fpath):
                        self.send_response(200)
                        if path.endswith(".css"):
                            self.send_header("Content-Type", "text/css")
                        self.end_headers()
                        with open(fpath, "rb") as f:
                            self.wfile.write(f.read())
                        return

                # 主控制网页与快捷键动作
                if path == "/":
                    action = query.get("action", [None])[0]
                    fmt = query.get("format", [None])[0]
                    if action:
                        result = MusicBox.dispatch(action)
                        if fmt == "json":
                            if isinstance(result, dict):
                                self._send_json(result)
                            else:
                                self._send_json({"ok": True, "action": action})
                            return

                    body = template.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return

                self.send_response(404)
                self.end_headers()

        server = ThreadingHTTPServer(("0.0.0.0", port), NativeHandler)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n服务已停止。", flush=True)
            server.server_close()


if __name__ == "__main__":
    run()
