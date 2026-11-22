# coding: utf-8
"""
网易云音乐 CEF / Electron 远程调试 (CDP) 桥接模块
特性：
1. 零第三方依赖：使用 Python 标准库实现轻量级本地 RFC 6455 WebSocket 客户端，
   完美兼容原生 Python 与 PyInstaller 单文件打包。
2. 零打扰智能联动：
   - 优先直连 127.0.0.1:9222 远程调试端口；
   - 若 9222 未开启且网易云未运行，自动带 --remote-debugging-port=9222 拉起网易云；
   - 若网易云已在运行但未开调试端口，绝不强杀重启，无感回退至 SMTC，并在后台持续热探测，
     待用户下次用带参数快捷方式启动网易云后自动热挂载。
3. 精准读取与控制：
   - 实时读取歌名、歌手、播放状态、总时长与精确播放进度；
   - 支持无焦点抢占的进度条拖动与 ±10s 快进快退（兼容 HTML5 audio、IPC channel、React Fiber 与 DOM 轨道事件）；
   - 实时读取网易云真实的红心喜欢状态（结合文本属性、React Fiber、SVG 图标与红心色值四重判定）。
"""

import atexit
import base64
import ctypes
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlparse


# Windows 进程快照检测 (无需启动 tasklist 子进程)
class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def is_cloudmusic_running():
    """检测网易云音乐进程 (cloudmusic.exe) 是否已在运行"""
    if sys.platform != "win32":
        return False

    try:
        kernel32 = ctypes.windll.kernel32
        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot and snapshot != INVALID_HANDLE_VALUE:
            try:
                entry = _PROCESSENTRY32W()
                entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
                if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                    while True:
                        if entry.szExeFile.lower() == "cloudmusic.exe":
                            return True
                        if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                            break
                return False
            finally:
                kernel32.CloseHandle(snapshot)
    except Exception:
        pass

    # 兜底检测窗口句柄
    try:
        return bool(ctypes.windll.user32.FindWindowW("OrpheusBrowserHost", None))
    except Exception:
        return False


def _clean_reg_exe_path(raw):
    if not raw:
        return ""
    s = str(raw).strip().strip('"')
    if "," in s and not os.path.exists(s):
        s = s.split(",", 1)[0].strip().strip('"')
    if s.lower().endswith(".exe") and os.path.exists(s):
        return s
    if os.path.isdir(s):
        cand = os.path.join(s, "cloudmusic.exe")
        if os.path.exists(cand):
            return cand
    return ""


def find_cloudmusic_exe():
    """从环境变量、注册表与常见目录自动定位网易云音乐可执行文件路径"""
    env_path = os.environ.get("CLOUDMUSIC_PATH", "").strip().strip('"')
    if env_path and os.path.exists(env_path):
        return env_path

    if sys.platform == "win32":
        try:
            import winreg

            # 1. App Paths
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for sub in (
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\cloudmusic.exe",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\cloudmusic.exe",
                ):
                    try:
                        with winreg.OpenKey(root, sub) as k:
                            val, _ = winreg.QueryValueEx(k, "")
                            p = _clean_reg_exe_path(val)
                            if p:
                                return p
                    except OSError:
                        pass

            # 2. Uninstall 注册表项
            uninstall_paths = [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            ]
            for root, base in uninstall_paths:
                try:
                    with winreg.OpenKey(root, base) as uk:
                        count = winreg.QueryInfoKey(uk)[0]
                        for i in range(count):
                            try:
                                name = winreg.EnumKey(uk, i)
                                with winreg.OpenKey(uk, name) as sk:
                                    disp = ""
                                    try:
                                        disp = str(winreg.QueryValueEx(sk, "DisplayName")[0])
                                    except OSError:
                                        pass
                                    low_name = (name + " " + disp).lower()
                                    if "网易云" in low_name or "cloudmusic" in low_name or "netease" in low_name:
                                        for key_name in ("DisplayIcon", "InstallLocation"):
                                            try:
                                                val = winreg.QueryValueEx(sk, key_name)[0]
                                                p = _clean_reg_exe_path(val)
                                                if p:
                                                    return p
                                            except OSError:
                                                pass
                            except OSError:
                                continue
                except OSError:
                    continue
        except Exception:
            pass

    # 3. 常见安装路径扫描
    candidates = []
    for drive in ("C:", "D:", "E:", "F:"):
        candidates.extend([
            os.path.join(drive + "\\", "Program Files", "Netease", "CloudMusic", "cloudmusic.exe"),
            os.path.join(drive + "\\", "Program Files (x86)", "Netease", "CloudMusic", "cloudmusic.exe"),
            os.path.join(drive + "\\", "Netease", "CloudMusic", "cloudmusic.exe"),
            os.path.join(drive + "\\", "CloudMusic", "cloudmusic.exe"),
        ])
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        candidates.append(os.path.join(local_app, "Programs", "Netease", "CloudMusic", "cloudmusic.exe"))
        candidates.append(os.path.join(local_app, "Netease", "CloudMusic", "cloudmusic.exe"))

    for p in candidates:
        if os.path.exists(p):
            return p
    return ""


class _SimpleWebSocket(object):
    """零依赖 RFC 6455 本地 WebSocket 客户端，用于与 Chromium CDP 通信"""

    def __init__(self, ws_url, timeout=2.0):
        parsed = urlparse(ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._buf = bytearray()

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self._sock.sendall(req.encode("ascii"))

        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self._sock.recv(4096)
            if not chunk:
                self.close()
                raise ConnectionError("WebSocket handshake closed unexpectedly")
            header += chunk
            if len(header) > 65536:
                self.close()
                raise ConnectionError("WebSocket handshake header too large")

        head_part, rest = header.split(b"\r\n\r\n", 1)
        if rest:
            self._buf.extend(rest)

        status_line = head_part.split(b"\r\n", 1)[0].decode("ascii", errors="ignore")
        if " 101 " not in status_line:
            self.close()
            raise ConnectionError(f"WebSocket handshake failed: {status_line}")

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise EOFError("WebSocket connection closed")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _send_frame(self, opcode, payload_bytes):
        length = len(payload_bytes)
        header = bytearray([0x80 | (opcode & 0x0F)])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))

        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload_bytes))
        self._sock.sendall(bytes(header) + masked)

    def send_text(self, text):
        self._send_frame(0x01, text.encode("utf-8"))

    def recv_text(self):
        chunks = []
        while True:
            b1, b2 = self._recv_exact(2)
            fin = (b1 & 0x80) != 0
            opcode = b1 & 0x0F
            masked = (b2 & 0x80) != 0
            length = b2 & 0x7F

            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]

            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length) if length > 0 else b""
            if mask is not None:
                payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))

            if opcode == 0x08:  # CLOSE
                raise EOFError("WebSocket closed by remote")
            if opcode == 0x09:  # PING
                self._send_frame(0x0A, payload)
                continue
            if opcode == 0x0A:  # PONG
                continue

            if opcode in (0x00, 0x01, 0x02):
                chunks.append(payload)
                if fin:
                    return b"".join(chunks).decode("utf-8", errors="replace")

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass


# 注入到网易云音乐渲染进程的轻量级无侵入 JS 桥接脚本
# 兼容网易云 2.x 与 3.x (3.0 / 3.1+)，支持实时进度、精准拖动与真实红心状态判定
NCM_BRIDGE_JS = r"""
(() => {
  const BRIDGE_VER = 3;
  if (window.__NMC_BRIDGE__ && window.__NMC_BRIDGE__.ver === BRIDGE_VER) {
    return true;
  }

  const state = {
    trackId: "",
    position: 0,
    duration: 0,
    isPlaying: null,
    lastProgressTs: 0,
    channelHooked: false,
    seekSignature: null
  };

  function parseMMSS(str) {
    if (!str) return null;
    const m = String(str).trim().match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
    if (!m) return null;
    if (m[3] !== undefined) {
      return parseInt(m[1], 10) * 3600 + parseInt(m[2], 10) * 60 + parseInt(m[3], 10);
    }
    return parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
  }

  function parseCombinedTime(str) {
    if (!str) return null;
    const m = String(str).trim().match(/^(\d{1,2}:\d{2}(?::\d{2})?)\s*\/\s*(\d{1,2}:\d{2}(?::\d{2})?)$/);
    if (!m) return null;
    const cur = parseMMSS(m[1]);
    const dur = parseMMSS(m[2]);
    if (cur !== null && dur !== null && dur >= 5) {
      return { position: cur, duration: dur };
    }
    return null;
  }

  // 非侵入式监听 window.channel (网易云 2.x / 3.x 原生音频通道)
  function installChannelHooks() {
    try {
      const ch = window.channel;
      if (!ch || ch.__nmcHooked) return;
      ch.__nmcHooked = true;
      state.channelHooked = true;

      if (typeof ch.registerCall === "function") {
        const origReg = ch.registerCall.bind(ch);
        ch.registerCall = function(name, cb, ...rest) {
          if (typeof name === "string" && typeof cb === "function") {
            const low = name.toLowerCase();
            if (low.includes("onplayprogress")) {
              const wrapped = function(...args) {
                try {
                  if (typeof args[0] === "string" && args[0]) state.trackId = args[0];
                  const nums = args.filter(a => typeof a === "number" && isFinite(a));
                  if (nums.length >= 1) {
                    let p = nums[0];
                    if (p > 100000) p = p / 1000;
                    state.position = p;
                    state.lastProgressTs = Date.now();
                  }
                  if (nums.length >= 2 && nums[1] > 1) {
                    let d = nums[1];
                    if (d > 100000) d = d / 1000;
                    state.duration = d;
                  }
                } catch (e) {}
                return cb.apply(this, args);
              };
              return origReg(name, wrapped, ...rest);
            }
            if (low.includes("onplaystate")) {
              const wrapped = function(...args) {
                try {
                  if (typeof args[0] === "string" && args[0]) state.trackId = args[0];
                  const joined = args.map(a => String(a)).join("|").toLowerCase();
                  if (joined.includes("pause") || args[2] === 2) state.isPlaying = false;
                  else if (joined.includes("play") || joined.includes("resume") || args[2] === 1) state.isPlaying = true;
                } catch (e) {}
                return cb.apply(this, args);
              };
              return origReg(name, wrapped, ...rest);
            }
            if (low.includes("onload")) {
              const wrapped = function(...args) {
                try {
                  if (typeof args[0] === "string" && args[0]) state.trackId = args[0];
                  const info = args[1];
                  if (info && typeof info === "object") {
                    const dur = info.duration || info.dt || info.totalTime;
                    if (typeof dur === "number" && dur > 0) {
                      state.duration = dur > 10000 ? dur / 1000 : dur;
                    }
                  }
                } catch (e) {}
                return cb.apply(this, args);
              };
              return origReg(name, wrapped, ...rest);
            }
          }
          return origReg(name, cb, ...rest);
        };
      }

      if (typeof ch.call === "function") {
        const origCall = ch.call.bind(ch);
        ch.call = function(name, ...args) {
          try {
            if (typeof name === "string" && name.toLowerCase().startsWith("audioplayer.")) {
              const payload = Array.isArray(args[1]) ? args[1] : (Array.isArray(args[0]) ? args[0] : null);
              if (payload && typeof payload[0] === "string" && payload[0]) {
                state.trackId = payload[0];
              }
              if (name.toLowerCase() === "audioplayer.seek" && payload) {
                state.seekSignature = payload.length;
              }
            }
          } catch (e) {}
          return origCall(name, ...args);
        };
      }
    } catch (e) {}
  }

  function getReactProps(el) {
    if (!el) return null;
    for (const k of Object.keys(el)) {
      if (k.startsWith("__reactProps$")) return el[k];
    }
    return null;
  }

  function getReactFiber(el) {
    if (!el) return null;
    for (const k of Object.keys(el)) {
      if (k.startsWith("__reactFiber$") || k.startsWith("__reactInternalInstance$")) {
        return el[k];
      }
    }
    return null;
  }

  function isVolumeRelated(el) {
    let cur = el;
    for (let i = 0; i < 4 && cur && cur !== document.body; i++) {
      const txt = [
        cur.id || "",
        typeof cur.className === "string" ? cur.className : "",
        cur.getAttribute ? (cur.getAttribute("aria-label") || "") : "",
        cur.getAttribute ? (cur.getAttribute("title") || "") : "",
        cur.getAttribute ? (cur.getAttribute("data-testid") || "") : ""
      ].join(" ").toLowerCase();
      if (/volume|volbar|vol-bar|vol_bar|mute|sound|音量|静音|倍速|speed|pitch|eq/.test(txt)) {
        return true;
      }
      cur = cur.parentElement;
    }
    return false;
  }

  function isExcludedNav(el) {
    let cur = el;
    for (let i = 0; i < 4 && cur && cur !== document.body; i++) {
      const tag = (cur.tagName || "").toLowerCase();
      if (tag === "nav" || tag === "aside") return true;
      const txt = [
        cur.id || "",
        typeof cur.className === "string" ? cur.className : ""
      ].join(" ").toLowerCase();
      if (/sidebar|left-nav|nav-list|playlist-item|song-list|track-list|table-row/.test(txt)) {
        return true;
      }
      cur = cur.parentElement;
    }
    return false;
  }

  function isOtherControlButton(el) {
    const txt = [
      el.id || "",
      typeof el.className === "string" ? el.className : "",
      el.getAttribute ? (el.getAttribute("title") || "") : "",
      el.getAttribute ? (el.getAttribute("aria-label") || "") : "",
      el.getAttribute ? (el.getAttribute("data-tooltip") || "") : ""
    ].join(" ").toLowerCase();
    return /播放|暂停|上一首|下一首|音量|静音|播放列表|循环|随机|歌词|音效|音质|评论|分享|下载|play-btn|pause-btn|btnp-play|btnp-pause|prev|next|volume|playlist|lyric/.test(txt);
  }

  function findPlayerBar() {
    const selectors = [
      "#main-player",
      ".m-player",
      "[id*='playerBar' i]",
      "[id*='playbar' i]",
      "[class*='playbar' i]",
      "[class*='player-bar' i]",
      "[class*='player_bar' i]",
      "[class*='playerBar' i]",
      "[class*='bottomPlayer' i]",
      "[class*='BottomPlayer' i]",
      "[data-testid*='player' i]",
      "footer"
    ];
    let hiddenFallback = null;
    for (const sel of selectors) {
      const nodes = document.querySelectorAll(sel);
      for (const n of nodes) {
        if (!n) continue;
        const hasCtrl = Boolean(n.querySelector("[role='slider'], input[type='range'], [title*='播放'], [title*='暂停'], [title*='下一首'], [aria-label*='播放'], [aria-label*='暂停'], .btnp-play, .btnp-pause, time"));
        if (!hasCtrl) continue;
        const rect = n.getBoundingClientRect();
        if (rect.width > 200 && rect.height >= 40 && rect.height <= 220) {
          return n;
        }
        if (rect.width === 0 && !hiddenFallback) {
          hiddenFallback = n;
        }
      }
    }
    if (hiddenFallback) return hiddenFallback;

    // 结构兜底：通过“上一首 / 下一首 / 播放 / 暂停”按钮向上找最近的底部播放栏容器
    const ctrl = document.querySelector(
      "[title='播放'], [title='暂停'], [title='下一首'], [title='上一首'], " +
      "[aria-label='播放'], [aria-label='暂停'], [aria-label='下一首'], " +
      "[title*='暂停'], [title*='播放'], .btnp-play, .btnp-pause"
    );
    if (ctrl) {
      let cur = ctrl.parentElement;
      for (let i = 0; i < 8 && cur && cur !== document.body; i++) {
        const rect = cur.getBoundingClientRect();
        const hasSliderOrTime = cur.querySelector("[role='slider'], input[type='range'], [class*='progress' i], [class*='slider' i], [class*='prg' i], time");
        if (hasSliderOrTime && ((rect.width > 300 && rect.height <= 220) || rect.width === 0)) {
          return cur;
        }
        cur = cur.parentElement;
      }
    }
    return document.body;
  }

  function findTimes(root) {
    const scope = root || document.body;
    // 1. 优先查 2.x 经典 time.now / time.all
    const tNow = scope.querySelector("time.now, .time .now, [class*='currentTime' i], [class*='curTime' i], [class*='time-cur' i]");
    const tAll = scope.querySelector("time.all, .time .all, [class*='totalTime' i], [class*='duration' i], [class*='time-all' i]");
    if (tNow && tAll) {
      const p = parseMMSS(tNow.textContent);
      const d = parseMMSS(tAll.textContent);
      if (p !== null && d !== null && d >= 5 && p <= d + 2) {
        return { position: p, duration: d, source: "dom_named_time" };
      }
    }

    // 2. 扫描播放栏内所有叶子节点的 MM:SS 或 MM:SS / MM:SS
    const singleTimes = [];
    const nodes = scope.querySelectorAll("span, div, time, p, em");
    const winH = window.innerHeight || 800;
    for (const el of nodes) {
      if (el.children && el.children.length > 0) continue;
      const txt = (el.textContent || "").trim();
      if (!txt || txt.length > 22) continue;

      const comb = parseCombinedTime(txt);
      if (comb) {
        const rect = el.getBoundingClientRect();
        if (scope !== document.body || rect.width === 0 || rect.top > winH - 180) {
          return { position: comb.position, duration: comb.duration, source: "dom_combined_time" };
        }
      }

      const sec = parseMMSS(txt);
      if (sec !== null) {
        const rect = el.getBoundingClientRect();
        if (scope !== document.body || rect.width === 0 || rect.top > winH - 180) {
          singleTimes.push({ sec, x: rect.left, el });
        }
      }
    }

    // 从候选时间中找合理的一对 (cur, total)
    for (let i = 0; i < singleTimes.length - 1; i++) {
      const a = singleTimes[i];
      const b = singleTimes[i + 1];
      if (b.sec >= 5 && a.sec <= b.sec + 2) {
        return { position: a.sec, duration: b.sec, source: "dom_pair_time" };
      }
    }
    return null;
  }

  function inspectSliderFiber(el) {
    let fiber = getReactFiber(el);
    let info = {
      value: null,
      min: 0,
      max: null,
      onChange: null,
      onCommit: null,
      onSeek: null
    };
    const directProps = getReactProps(el);
    const checkProps = (p) => {
      if (!p || typeof p !== "object") return;
      if (info.value === null && typeof p.value === "number" && isFinite(p.value)) info.value = p.value;
      if (info.value === null && typeof p.currentTime === "number" && isFinite(p.currentTime)) info.value = p.currentTime;
      if (info.max === null && typeof p.max === "number" && isFinite(p.max) && p.max > 0) info.max = p.max;
      if (info.max === null && typeof p.duration === "number" && isFinite(p.duration) && p.duration > 0) info.max = p.duration;
      if (typeof p.min === "number" && isFinite(p.min)) info.min = p.min;

      if (!info.onChange) {
        for (const fnName of ["onChange", "onValueChange", "onInput", "onProgressChange", "onDragging"]) {
          if (typeof p[fnName] === "function") { info.onChange = p[fnName]; break; }
        }
      }
      if (!info.onCommit) {
        for (const fnName of ["onChangeComplete", "onAfterChange", "onValueCommit", "onSeekEnd", "onDragEnd", "onChangeEnd"]) {
          if (typeof p[fnName] === "function") { info.onCommit = p[fnName]; break; }
        }
      }
      if (!info.onSeek) {
        for (const fnName of ["onSeek", "seek", "seekTo", "setCurrentTime"]) {
          if (typeof p[fnName] === "function") { info.onSeek = p[fnName]; break; }
        }
      }
    };
    checkProps(directProps);
    for (let depth = 0; depth < 8 && fiber; depth++) {
      checkProps(fiber.memoizedProps);
      fiber = fiber.return;
    }
    return info;
  }

  function findProgressSlider(root) {
    const scope = root || document.body;
    const candidates = Array.from(scope.querySelectorAll(
      "input[type='range'], [role='slider'], [aria-valuemax], " +
      ".prg, .prg-ply, [class*='progress' i], [class*='slider' i]"
    ));
    let best = null;
    let bestScore = -1;
    const winH = window.innerHeight || 800;

    for (const el of candidates) {
      if (!el || isVolumeRelated(el) || isExcludedNav(el)) continue;
      const rect = el.getBoundingClientRect();
      // 排除明显是竖向条（音量条通常是竖的）
      if (rect.height > 0 && rect.width > 0 && rect.height > rect.width) continue;

      let score = 0;
      const cls = (typeof el.className === "string" ? el.className : "").toLowerCase();
      const role = (el.getAttribute("role") || "").toLowerCase();
      const tag = (el.tagName || "").toLowerCase();

      if (tag === "input" && el.type === "range") score += 45;
      if (role === "slider") score += 45;
      if (el.hasAttribute("aria-valuemax")) score += 25;
      if (/progress|prg|timeline|seek/.test(cls)) score += 25;
      if (/track|bar|rail/.test(cls)) score += 10;
      if (/thumb|dot|handle|knob|tip/.test(cls)) score -= 25;

      if (rect.width >= 120) score += 25;
      else if (rect.width >= 60) score += 10;
      if (rect.height > 0 && rect.height <= 36) score += 10;
      if (rect.top > winH - 160 && rect.width > 0) score += 15;

      const fInfo = inspectSliderFiber(el);
      if (fInfo.onSeek || fInfo.onCommit || fInfo.onChange) score += 30;
      if (fInfo.max !== null && fInfo.max > 0) score += 15;

      if (score > bestScore) {
        bestScore = score;
        best = el;
      }
    }
    return bestScore >= 25 ? best : null;
  }

  // 解析 CSS 颜色为 {r,g,b,a}
  function parseColor(str) {
    if (!str || str === "none" || str === "transparent" || str === "currentcolor") return null;
    const s = String(str).trim().toLowerCase();
    let m = s.match(/^rgba?\(\s*(\d+(?:\.\d+)?)\s*[,\s]\s*(\d+(?:\.\d+)?)\s*[,\s]\s*(\d+(?:\.\d+)?)(?:\s*[,\/]\s*(\d*(?:\.\d+)?))?\s*\)$/);
    if (m) {
      const a = m[4] !== undefined && m[4] !== "" ? parseFloat(m[4]) : 1;
      return { r: parseFloat(m[1]), g: parseFloat(m[2]), b: parseFloat(m[3]), a };
    }
    m = s.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/);
    if (m) {
      let hex = m[1];
      if (hex.length === 3) hex = hex[0]+hex[0]+hex[1]+hex[1]+hex[2]+hex[2];
      return {
        r: parseInt(hex.slice(0, 2), 16),
        g: parseInt(hex.slice(2, 4), 16),
        b: parseInt(hex.slice(4, 6), 16),
        a: 1
      };
    }
    return null;
  }

  function isRedColor(c) {
    if (!c || c.a < 0.25) return false;
    return c.r >= 175 && (c.r - c.g) >= 55 && (c.r - c.b) >= 45;
  }

  function findLikeButton(root) {
    const scope = root || document.body;
    const winH = window.innerHeight || 800;

    // 1. 优先精确匹配 title / aria-label / data-tooltip 含“喜欢”且处于播放栏中的交互控件
    const explicitSelectors = [
      "[title='取消喜欢']",
      "[title='喜欢']",
      "[title*='取消喜欢']",
      "[title*='喜欢']",
      "[aria-label*='取消喜欢']",
      "[aria-label*='喜欢']",
      "[data-tooltip*='喜欢']",
      "[data-tip*='喜欢']",
      "[data-action='love']",
      "[data-action='like']",
      ".btn-love",
      "[class*='btn-love' i]",
      "[class*='likeBtn' i]",
      "[class*='like-btn' i]",
      "[class*='loveBtn' i]",
      "[class*='love-btn' i]",
      "[class*='heart' i]"
    ];

    let best = null;
    let bestScore = -1;

    for (const sel of explicitSelectors) {
      let nodes;
      try { nodes = scope.querySelectorAll(sel); } catch (e) { continue; }
      for (const el of nodes) {
        if (!el || isExcludedNav(el) || isOtherControlButton(el)) continue;
        // 排除“我喜欢的音乐”歌单文字链接
        const txt = (el.textContent || "").trim();
        if (txt.length > 6 || /歌单|我喜欢的音乐|列表|收藏到/.test(txt)) continue;
        if (el.tagName === "A" && el.getAttribute("href") && el.getAttribute("href").includes("playlist")) continue;

        const rect = el.getBoundingClientRect();
        // 如果 scope 是整个 body，则严格限制在底部播放栏区域
        if (scope === document.body && rect.width > 0 && rect.top < winH - 160) continue;

        let score = 10;
        const attrs = [
          el.getAttribute("title") || "",
          el.getAttribute("aria-label") || "",
          el.getAttribute("data-tooltip") || "",
          el.getAttribute("data-tip") || ""
        ].join(" ");
        if (/取消喜欢|已喜欢/.test(attrs)) score += 60;
        else if (/^喜欢$|喜欢音乐|喜欢歌曲|红心/.test(attrs.trim())) score += 55;
        else if (/喜欢/.test(attrs)) score += 40;

        const cls = (typeof el.className === "string" ? el.className : "").toLowerCase();
        if (/btn-love|likebtn|like-btn|lovebtn|heart/.test(cls)) score += 30;
        if (el.querySelector("svg") || el.tagName.toLowerCase() === "svg") score += 15;
        if (rect.width > 0 && rect.width <= 64 && rect.height <= 64) score += 15;
        if (rect.width > 0 && rect.top > winH - 140) score += 15;

        if (score > bestScore) {
          bestScore = score;
          best = el;
        }
      }
    }

    if (best) return best;

    // 2. 兜底：在播放栏内检查带有 React liked/isLiked 属性的按钮，或心形 SVG 按钮
    if (scope !== document.body) {
      const btns = scope.querySelectorAll("button, [role='button'], span, div");
      for (const el of btns) {
        if (!el || isExcludedNav(el) || isOtherControlButton(el)) continue;
        if ((el.textContent || "").trim().length > 4) continue;
        const r = inspectLikeFiber(el);
        if (r !== null) return el;
      }
    }
    return null;
  }

  function inspectLikeFiber(el) {
    const keys = ["isLiked", "liked", "isLoved", "loved", "isLike", "isLove", "isFavorite", "favorite", "inLikeList", "starred", "isRedHeart"];
    const checkObj = (obj) => {
      if (!obj || typeof obj !== "object") return null;
      for (const k of keys) {
        if (typeof obj[k] === "boolean") return { liked: obj[k], key: k };
      }
      return null;
    };
    const dp = getReactProps(el);
    const r0 = checkObj(dp);
    if (r0) return r0;

    let fiber = getReactFiber(el);
    for (let i = 0; i < 6 && fiber; i++) {
      const r1 = checkObj(fiber.memoizedProps);
      if (r1) return r1;
      // 避免沿着 Fiber 爬太远误读别的组件
      if (fiber.type && typeof fiber.type === "string" && (fiber.type === "footer" || fiber.type === "main")) break;
      fiber = fiber.return;
    }
    return null;
  }

  function detectRealLiked(btn) {
    if (!btn) return { found: false, liked: null, reason: "no_button" };

    // 1. 检查显式文字/无障碍属性（btn 自身及最近父级 1 层）
    const targets = [btn];
    if (btn.parentElement) targets.push(btn.parentElement);
    for (const t of targets) {
      const title = [
        t.getAttribute ? (t.getAttribute("title") || "") : "",
        t.getAttribute ? (t.getAttribute("aria-label") || "") : "",
        t.getAttribute ? (t.getAttribute("data-tooltip") || "") : "",
        t.getAttribute ? (t.getAttribute("data-tip") || "") : ""
      ].join(" ").trim();

      if (/取消喜欢|已喜欢|取消收藏|unlike/i.test(title)) {
        return { found: true, liked: true, reason: "attr_unlike:" + title };
      }
      const pressed = t.getAttribute ? (t.getAttribute("aria-pressed") || t.getAttribute("aria-checked")) : null;
      if (pressed === "true") {
        return { found: true, liked: true, reason: "aria_pressed" };
      }
    }

    // 2. 检查 React Fiber 布尔状态（3.x 最精准）
    const fiberLike = inspectLikeFiber(btn);
    if (fiberLike !== null) {
      return { found: true, liked: fiberLike.liked, reason: "react:" + fiberLike.key };
    }

    // 3. 检查 class / dataset / SVG <use> 图标名
    const clsChain = [
      typeof btn.className === "string" ? btn.className : "",
      btn.parentElement && typeof btn.parentElement.className === "string" ? btn.parentElement.className : ""
    ].join(" ").toLowerCase();

    if (/\b(loved|liked|is-liked|isliked|btn-loved|z-loved|like-active|heart-filled|unlike)\b/.test(clsChain)) {
      return { found: true, liked: true, reason: "class_liked" };
    }
    const useEl = btn.querySelector ? btn.querySelector("use") : null;
    if (useEl) {
      const href = (useEl.getAttribute("xlink:href") || useEl.getAttribute("href") || "").toLowerCase();
      if (href) {
        if (/loved|liked|filled|fill|solid|red|unlike/.test(href) && !/unfilled|outline|line/.test(href)) {
          return { found: true, liked: true, reason: "svg_use:" + href };
        }
      }
    }

    // 4. 检查心形 SVG/Path/图标的实际渲染色值（网易云已喜欢必为红色！）
    const colorNodes = [btn];
    if (btn.querySelectorAll) {
      colorNodes.push(...btn.querySelectorAll("svg, path, use, i, span, polygon, circle"));
    }
    for (const node of colorNodes) {
      try {
        if (node.getAttribute) {
          for (const attr of ["fill", "stroke"]) {
            const c = parseColor(node.getAttribute(attr));
            if (isRedColor(c)) {
              return { found: true, liked: true, reason: "attr_red:" + attr };
            }
          }
        }
        const cs = window.getComputedStyle(node);
        for (const prop of ["fill", "stroke", "color"]) {
          const c = parseColor(cs[prop]);
          if (isRedColor(c)) {
            return { found: true, liked: true, reason: "css_red:" + prop };
          }
        }
      } catch (e) {}
    }

    // 若按钮已明确匹配为喜欢按钮且以上均非红心，则判定为未喜欢
    return { found: true, liked: false, reason: "default_unliked" };
  }

  function detectPlayingFromDom(root) {
    const scope = root || document.body;
    if (scope.querySelector("[title='暂停'], [aria-label='暂停'], [title*='暂停'], .btnp-pause, [class*='btn-pause' i]")) {
      return true;
    }
    if (scope.querySelector("[title='播放'], [aria-label='播放'], .btnp-play, [class*='btn-play' i]")) {
      return false;
    }
    return null;
  }

  function findActiveAudio() {
    const audios = document.querySelectorAll("audio");
    for (const a of audios) {
      if (a && isFinite(a.duration) && a.duration > 0.5) {
        return a;
      }
    }
    return null;
  }

  function readStatus() {
    installChannelHooks();
    const playerBar = findPlayerBar();
    const audio = findActiveAudio();
    const slider = findProgressSlider(playerBar);
    const times = findTimes(playerBar);
    const likeBtn = findLikeButton(playerBar);
    const likeInfo = detectRealLiked(likeBtn);

    let position = 0;
    let duration = 0;
    let hasTimeline = false;
    let isPlaying = null;
    let timelineSource = "none";

    // 1. HTML5 audio 优先
    if (audio) {
      position = audio.currentTime || 0;
      duration = audio.duration || 0;
      hasTimeline = duration > 0.5;
      isPlaying = !audio.paused;
      timelineSource = "audio";
    }

    // 2. Hooked channel 原生进度
    if (!hasTimeline && state.duration > 0.5 && (Date.now() - state.lastProgressTs) < 5000) {
      position = state.position;
      duration = state.duration;
      hasTimeline = true;
      timelineSource = "channel_hook";
    }

    // 3. DOM 时间文本 + Slider 组合（即使刚挂载或最小化也立即可读）
    if (!hasTimeline && times && times.duration > 0.5) {
      position = times.position;
      duration = times.duration;
      hasTimeline = true;
      timelineSource = times.source;
    }

    if (!hasTimeline && slider) {
      const fInfo = inspectSliderFiber(slider);
      let sMax = fInfo.max;
      let sVal = fInfo.value;
      if (sMax === null) {
        const am = parseFloat(slider.getAttribute("aria-valuemax") || slider.max);
        if (isFinite(am) && am > 0) sMax = am;
      }
      if (sVal === null) {
        const av = parseFloat(slider.getAttribute("aria-valuenow") || slider.value);
        if (isFinite(av)) sVal = av;
      }
      if (sMax !== null && sMax >= 5 && sVal !== null) {
        if (sMax > 36000) { // 毫秒转秒
          duration = sMax / 1000;
          position = sVal / 1000;
        } else {
          duration = sMax;
          position = sVal;
        }
        hasTimeline = duration > 0.5;
        timelineSource = "slider_props";
      }
    }

    // 若同时有精确毫秒级 channel 进度与 DOM 时长，取更精确的秒数
    if (hasTimeline && state.lastProgressTs && (Date.now() - state.lastProgressTs) < 3000) {
      if (state.position >= 0 && state.position <= duration + 2) {
        position = state.position;
      }
    }

    if (isPlaying === null) {
      if (state.isPlaying !== null) isPlaying = state.isPlaying;
      else isPlaying = detectPlayingFromDom(playerBar);
    }

    // 从 document.title 解析歌名与歌手作为补充
    let title = "";
    let artist = "";
    const docTitle = (document.title || "").trim();
    if (docTitle && docTitle !== "网易云音乐" && docTitle !== "NetEase Cloud Music") {
      if (docTitle.includes(" - ")) {
        const idx = docTitle.lastIndexOf(" - ");
        title = docTitle.slice(0, idx).trim();
        artist = docTitle.slice(idx + 3).trim();
      } else {
        title = docTitle;
      }
    }

    return {
      ok: true,
      has_player: Boolean(audio || slider || times || likeBtn || state.channelHooked),
      title,
      artist,
      is_playing: isPlaying,
      position: Math.round(Math.max(0, position) * 100) / 100,
      duration: Math.round(Math.max(0, duration) * 100) / 100,
      has_timeline: hasTimeline,
      can_seek: Boolean(audio || slider || state.trackId),
      timeline_source: timelineSource,
      like_found: likeInfo.found,
      is_liked: likeInfo.found ? likeInfo.liked : null,
      like_reason: likeInfo.reason
    };
  }

  function seekTo(targetSec) {
    installChannelHooks();
    const st = readStatus();
    const dur = st.duration;
    let target = Math.max(0, Number(targetSec) || 0);
    if (dur > 0.5 && target > dur) target = dur;

    // 1. HTML5 audio
    const audio = findActiveAudio();
    if (audio) {
      audio.currentTime = target;
      return { ok: true, method: "audio", position: Math.round(target * 100) / 100, duration: Math.round(dur * 100) / 100 };
    }

    // 2. 原生 channel.call("audioplayer.seek") (在已捕获 trackId 时后台最小化同样有效)
    if (window.channel && typeof window.channel.call === "function" && state.trackId) {
      try {
        const args = state.seekSignature === 2
          ? [state.trackId, target]
          : [state.trackId, "nmc_" + Date.now(), target];
        window.channel.call("audioplayer.seek", () => {}, args);
        state.position = target;
        state.lastProgressTs = Date.now();
        return { ok: true, method: "channel_seek", position: Math.round(target * 100) / 100, duration: Math.round(dur * 100) / 100 };
      } catch (e) {}
    }

    const playerBar = findPlayerBar();
    const slider = findProgressSlider(playerBar);
    if (!slider) {
      return { ok: false, method: "none", message: "未在网易云页面定位到进度条组件" };
    }

    const ratio = dur > 0.5 ? Math.max(0, Math.min(1, target / dur)) : 0;

    // 3. 原生 <input type="range"> 或 React Fiber 回调 (后台/最小化亦可生效)
    const fInfo = inspectSliderFiber(slider);
    let nativeMax = fInfo.max;
    let nativeMin = fInfo.min || 0;
    if (nativeMax === null) {
      const am = parseFloat(slider.getAttribute("aria-valuemax") || slider.max);
      if (isFinite(am) && am > 0) nativeMax = am;
    }
    if (nativeMax !== null && nativeMax > nativeMin) {
      let nativeVal;
      if (dur > 0.5 && Math.abs(nativeMax - dur) < 3) {
        nativeVal = target;
      } else if (dur > 0.5 && Math.abs(nativeMax - dur * 1000) < 3000) {
        nativeVal = Math.round(target * 1000);
      } else {
        nativeVal = nativeMin + ratio * (nativeMax - nativeMin);
      }

      if (slider.tagName && slider.tagName.toLowerCase() === "input" && slider.type === "range") {
        try {
          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
          setter.call(slider, String(nativeVal));
          slider.dispatchEvent(new Event("input", { bubbles: true }));
          slider.dispatchEvent(new Event("change", { bubbles: true }));
          if (fInfo.onCommit) fInfo.onCommit(nativeVal);
          return { ok: true, method: "input_range", position: Math.round(target * 100) / 100, duration: Math.round(dur * 100) / 100 };
        } catch (e) {}
      }

      if (fInfo.onSeek) {
        try {
          fInfo.onSeek(nativeVal);
          return { ok: true, method: "react_onSeek", position: Math.round(target * 100) / 100, duration: Math.round(dur * 100) / 100 };
        } catch (e) {}
      }

      if (fInfo.onChange || fInfo.onCommit) {
        try {
          if (fInfo.onChange) fInfo.onChange(nativeVal);
          if (fInfo.onCommit) fInfo.onCommit(nativeVal);
          return { ok: true, method: "react_slider_cb", position: Math.round(target * 100) / 100, duration: Math.round(dur * 100) / 100 };
        } catch (e) {}
      }
    }

    // 4. DOM 合成指针/鼠标事件（不移动真实鼠标、不抢占窗口焦点）
    let track = slider;
    let rect = track.getBoundingClientRect();
    if (rect.width < 40 && track.parentElement) {
      const pRect = track.parentElement.getBoundingClientRect();
      if (pRect.width >= 40) {
        track = track.parentElement;
        rect = pRect;
      }
    }
    if (rect.width >= 40 && dur > 0.5) {
      const x = rect.left + Math.max(1, Math.min(rect.width - 1, rect.width * ratio));
      const y = rect.top + rect.height / 2;
      const baseInit = { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0, buttons: 1 };
      try {
        if (typeof PointerEvent !== "undefined") {
          track.dispatchEvent(new PointerEvent("pointerdown", Object.assign({ pointerId: 1, isPrimary: true }, baseInit)));
        }
        track.dispatchEvent(new MouseEvent("mousedown", baseInit));
        if (typeof PointerEvent !== "undefined") {
          window.dispatchEvent(new PointerEvent("pointermove", Object.assign({ pointerId: 1, isPrimary: true }, baseInit)));
          track.dispatchEvent(new PointerEvent("pointerup", Object.assign({ pointerId: 1, isPrimary: true, buttons: 0 }, baseInit)));
        }
        window.dispatchEvent(new MouseEvent("mousemove", baseInit));
        window.dispatchEvent(new MouseEvent("mouseup", Object.assign({}, baseInit, { buttons: 0 })));
        track.dispatchEvent(new MouseEvent("click", Object.assign({}, baseInit, { buttons: 0 })));
        return { ok: true, method: "dom_pointer_seek", position: Math.round(target * 100) / 100, duration: Math.round(dur * 100) / 100 };
      } catch (e) {}
    }

    return { ok: false, method: "failed", message: "网易云窗口当前处于隐藏状态且未捕获原生进度接口，请展开一次窗口或切歌后重试" };
  }

  function debugInfo() {
    const bar = findPlayerBar();
    const slider = findProgressSlider(bar);
    const likeBtn = findLikeButton(bar);
    const st = readStatus();
    return {
      url: location.href,
      docTitle: document.title,
      hasChannel: Boolean(window.channel),
      hookedState: state,
      playerBarTag: bar ? (bar.tagName + "#" + (bar.id || "") + "." + (typeof bar.className === "string" ? bar.className.slice(0, 60) : "")) : null,
      sliderInfo: slider ? {
        tag: slider.tagName,
        cls: typeof slider.className === "string" ? slider.className.slice(0, 80) : "",
        role: slider.getAttribute("role"),
        max: slider.getAttribute("aria-valuemax") || slider.max || null,
        val: slider.getAttribute("aria-valuenow") || slider.value || null,
        rect: slider.getBoundingClientRect().toJSON ? slider.getBoundingClientRect().toJSON() : null
      } : null,
      likeBtnInfo: likeBtn ? {
        tag: likeBtn.tagName,
        cls: typeof likeBtn.className === "string" ? likeBtn.className.slice(0, 80) : "",
        title: likeBtn.getAttribute("title"),
        ariaLabel: likeBtn.getAttribute("aria-label"),
        detect: detectRealLiked(likeBtn)
      } : null,
      status: st
    };
  }

  installChannelHooks();
  window.__NMC_BRIDGE__ = {
    ver: BRIDGE_VER,
    readStatus,
    seekTo,
    debugInfo
  };
  return true;
})();
"""


class NetEaseCDPBridge(object):
    """网易云音乐 CDP (Chrome DevTools Protocol) 管理器"""

    PORT = int(os.environ.get("NCM_CDP_PORT", "9222"))

    _lock = threading.Lock()
    _ws = None
    _ws_url = ""
    _req_id = 0
    _bridge_installed = False

    _last_probe_time = 0.0
    _probe_interval = 4.0
    _cache_status = None
    _cache_time = 0.0
    _cache_ttl = 0.35

    _startup_done = False
    _ncm_running_without_cdp = False
    _auto_launched = False

    @classmethod
    def start_async(cls):
        """后台初始化：优先直连 9222；若网易云未开则安全代启；若已开则绝不打断并持续热探测"""
        if sys.platform != "win32":
            return
        t = threading.Thread(target=cls._startup_flow, daemon=True)
        t.start()

    @classmethod
    def _startup_flow(cls):
        if cls._try_connect():
            print(f"[CDP] 已直连网易云远程调试端口 ({cls.PORT})：精准进度拖动与真实红心状态同步已就绪！", flush=True)
            cls._startup_done = True
            return

        running = is_cloudmusic_running()
        if not running:
            exe = find_cloudmusic_exe()
            if exe:
                try:
                    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    subprocess.Popen(
                        [exe, f"--remote-debugging-port={cls.PORT}"],
                        cwd=os.path.dirname(exe),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=creationflags,
                    )
                    cls._auto_launched = True
                    print(f"[CDP] 检测到网易云未运行，已自动带 --remote-debugging-port={cls.PORT} 启动网易云音乐...", flush=True)
                    # 等待网易云 CEF 页面就绪（最多 15 秒）
                    for _ in range(15):
                        time.sleep(1.0)
                        if cls._try_connect():
                            print(f"[CDP] 网易云已就绪并成功挂载调试端口 ({cls.PORT})！", flush=True)
                            break
                except Exception as e:
                    print(f"[CDP] 自动启动网易云失败（已回退至 SMTC 模式）: {e}", flush=True)
            else:
                print(f"[CDP] 未自动找到 cloudmusic.exe，打开带 --remote-debugging-port={cls.PORT} 的网易云后将自动挂载。", flush=True)
        else:
            cls._ncm_running_without_cdp = True
            print(f"[CDP] 检测到网易云音乐已在运行但未开启 {cls.PORT} 调试端口，已无感回退至 SMTC（不打断当前播放）。", flush=True)
            print(f"[CDP] 下次用带 --remote-debugging-port={cls.PORT} 的快捷方式启动网易云后将自动接入！", flush=True)

        cls._startup_done = True

    @classmethod
    def invalidate_cache(cls):
        with cls._lock:
            cls._cache_time = 0.0

    @classmethod
    def shutdown(cls):
        with cls._lock:
            cls._close_locked()

    @classmethod
    def _close_locked(cls):
        if cls._ws is not None:
            try:
                cls._ws.close()
            except Exception:
                pass
        cls._ws = None
        cls._ws_url = ""
        cls._bridge_installed = False

    @classmethod
    def _fetch_targets(cls):
        url = f"http://127.0.0.1:{cls.PORT}/json"
        req = urllib.request.Request(url, headers={"User-Agent": "MusicController/1.3"})
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            return data if isinstance(data, list) else []

    @classmethod
    def _rank_targets(cls, targets):
        ranked = []
        skip_words = ("devtools://", "chrome-extension://", "desktoplyric", "lyric", "tray", "popup", "toast", "menu", "splash", "notification")
        for t in targets:
            if t.get("type") != "page":
                continue
            ws_url = t.get("webSocketDebuggerUrl")
            if not ws_url:
                continue
            page_url = (t.get("url") or "").lower()
            title = (t.get("title") or "").lower()
            if any(w in page_url for w in skip_words):
                continue

            score = 0
            if "orpheus://orpheus/pub/app.html" in page_url:
                score += 100
            elif "orpheus://" in page_url:
                score += 80
            elif "music.163.com" in page_url or "netease" in page_url:
                score += 70
            elif "index.html" in page_url or "app.html" in page_url:
                score += 50
            if "网易云" in title or " - " in title:
                score += 30
            ranked.append((score, ws_url, page_url))

        ranked.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in ranked]

    @classmethod
    def _eval_locked(cls, expression, timeout_ok=True):
        if cls._ws is None:
            return None
        cls._req_id += 1
        req_id = cls._req_id
        payload = {
            "id": req_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": False,
            },
        }
        cls._ws.send_text(json.dumps(payload))
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            raw = cls._ws.recv_text()
            msg = json.loads(raw)
            if msg.get("id") != req_id:
                continue
            if "error" in msg:
                return None
            result_obj = (msg.get("result") or {}).get("result") or {}
            if "value" in result_obj:
                return result_obj["value"]
            return None
        return None

    @classmethod
    def _ensure_connected_locked(cls, force_probe=False):
        if cls._ws is not None:
            return True

        now = time.monotonic()
        if not force_probe and (now - cls._last_probe_time) < cls._probe_interval:
            return False
        cls._last_probe_time = now

        try:
            targets = cls._fetch_targets()
        except Exception:
            return False

        candidates = cls._rank_targets(targets)
        for ws_url in candidates:
            ws = None
            try:
                ws = _SimpleWebSocket(ws_url, timeout=1.8)
                cls._ws = ws
                cls._ws_url = ws_url
                installed = cls._eval_locked(NCM_BRIDGE_JS)
                if not installed:
                    cls._close_locked()
                    continue
                probe = cls._eval_locked("window.__NMC_BRIDGE__ ? window.__NMC_BRIDGE__.readStatus() : null")
                if isinstance(probe, dict) and (probe.get("has_player") or len(candidates) == 1):
                    cls._bridge_installed = True
                    if cls._ncm_running_without_cdp:
                        cls._ncm_running_without_cdp = False
                        print(f"[CDP] 已自动热挂载网易云音乐调试端口 ({cls.PORT})！", flush=True)
                    return True
                # 若是多窗口中的非主页面，尝试下一个候选
                cls._close_locked()
            except Exception:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                cls._close_locked()
                continue
        return False

    @classmethod
    def _try_connect(cls):
        with cls._lock:
            return cls._ensure_connected_locked(force_probe=True)

    @classmethod
    def is_connected(cls):
        with cls._lock:
            return cls._ws is not None

    @classmethod
    def get_hint(cls):
        if cls.is_connected():
            return ""
        if is_cloudmusic_running():
            return f"网易云未开启 {cls.PORT} 调试端口，请用带 --remote-debugging-port={cls.PORT} 的快捷方式重启网易云"
        return ""

    @classmethod
    def get_status(cls, force=False):
        with cls._lock:
            now = time.monotonic()
            if not force and cls._cache_status is not None and (now - cls._cache_time) < cls._cache_ttl:
                cached = dict(cls._cache_status)
                if cached.get("is_playing") and cached.get("has_timeline") and cached.get("duration", 0) > 0:
                    dt = now - cls._cache_time
                    cached["position"] = round(min(cached["duration"], cached["position"] + dt), 2)
                return cached

            if not cls._ensure_connected_locked(force_probe=force):
                return None

            try:
                if not cls._bridge_installed:
                    cls._eval_locked(NCM_BRIDGE_JS)
                    cls._bridge_installed = True
                res = cls._eval_locked("window.__NMC_BRIDGE__ ? window.__NMC_BRIDGE__.readStatus() : null")
                if res is None:
                    # 页面可能刷新重建了，重新注入一次
                    cls._eval_locked(NCM_BRIDGE_JS)
                    res = cls._eval_locked("window.__NMC_BRIDGE__ ? window.__NMC_BRIDGE__.readStatus() : null")
            except Exception:
                cls._close_locked()
                return None

            if isinstance(res, dict) and res.get("ok"):
                cls._cache_status = dict(res)
                cls._cache_time = time.monotonic()
                return res
            return None

    @classmethod
    def seek_to(cls, position_sec):
        try:
            pos = max(0.0, float(position_sec))
        except (TypeError, ValueError):
            return None

        with cls._lock:
            cls._cache_time = 0.0
            if not cls._ensure_connected_locked(force_probe=True):
                return None
            try:
                if not cls._bridge_installed:
                    cls._eval_locked(NCM_BRIDGE_JS)
                    cls._bridge_installed = True
                res = cls._eval_locked(f"window.__NMC_BRIDGE__ ? window.__NMC_BRIDGE__.seekTo({pos:.3f}) : null")
                return res if isinstance(res, dict) else None
            except Exception:
                cls._close_locked()
                return None

    @classmethod
    def seek_delta(cls, delta_sec):
        st = cls.get_status(force=True)
        if not st or not st.get("has_timeline") or float(st.get("duration") or 0.0) <= 0.5:
            return None
        cur = float(st.get("position") or 0.0)
        dur = float(st.get("duration") or 0.0)
        target = max(0.0, min(dur, cur + float(delta_sec)))
        return cls.seek_to(target)

    @classmethod
    def get_debug_info(cls):
        with cls._lock:
            connected = cls._ensure_connected_locked(force_probe=True)
            if not connected:
                return {
                    "connected": False,
                    "port": cls.PORT,
                    "cloudmusic_running": is_cloudmusic_running(),
                    "cloudmusic_exe": find_cloudmusic_exe(),
                    "hint": f"请使用带 --remote-debugging-port={cls.PORT} 参数的快捷方式启动网易云音乐",
                }
            try:
                info = cls._eval_locked("window.__NMC_BRIDGE__ ? window.__NMC_BRIDGE__.debugInfo() : null")
                return {
                    "connected": True,
                    "port": cls.PORT,
                    "ws_url": cls._ws_url,
                    "debug": info,
                }
            except Exception as e:
                cls._close_locked()
                return {"connected": False, "error": str(e)}


atexit.register(NetEaseCDPBridge.shutdown)
