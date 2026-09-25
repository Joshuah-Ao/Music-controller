# coding: utf-8
"""
网易云音乐局域网远程控制器 (NetEase Music Controller)
基于原作者开源代码深度优化：
1. 快捷键完全对齐网易云音乐默认/当前全局快捷键（Ctrl + Alt + 方向键/P）。
2. 彻底去除已废弃的老旧 autopy 库依赖，改用 Windows 原生接口进行底层按键模拟。
3. 原生兼容模式：即使电脑未安装 Flask，也能使用 Python 标准库直接秒开运行。
"""

import os
import sys
import time
import socket
import ctypes

# Windows 虚拟键码定义 (完全对齐网易云音乐当前全局快捷键)
VK_CONTROL = 0x11   # Ctrl
VK_MENU = 0x12      # Alt
VK_LEFT = 0x25      # 方向左键 (上一首)
VK_UP = 0x26        # 方向上键 (音量加)
VK_RIGHT = 0x27     # 方向右键 (下一首)
VK_DOWN = 0x28      # 方向下键 (音量减)
VK_P = 0x50         # P 键 (播放/暂停)

# 方向键需设置 EXTENDEDKEY 标志
EXTENDED_KEYS = {VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN}


class MusicBox(object):
    """网易云音乐按键控制类"""
    @classmethod
    def _press_keys(cls, keys):
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

    @classmethod
    def prev_song(cls):
        print("[操作] 上一首 -> 模拟按键: Ctrl + Alt + Left", flush=True)
        cls._press_keys((VK_CONTROL, VK_MENU, VK_LEFT))

    @classmethod
    def pause_play(cls):
        print("[操作] 播放/暂停 -> 模拟按键: Ctrl + Alt + P", flush=True)
        cls._press_keys((VK_CONTROL, VK_MENU, VK_P))

    @classmethod
    def volume_up(cls):
        print("[操作] 音量加 -> 模拟按键: Ctrl + Alt + Up", flush=True)
        cls._press_keys((VK_CONTROL, VK_MENU, VK_UP))

    @classmethod
    def volume_down(cls):
        print("[操作] 音量减 -> 模拟按键: Ctrl + Alt + Down", flush=True)
        cls._press_keys((VK_CONTROL, VK_MENU, VK_DOWN))

    @classmethod
    def shutdown(cls):
        print("[操作] 收到关机指令，将在 5 秒后关机...", flush=True)
        os.system("shutdown -s -t 5")


def get_local_ip():
    """获取本机局域网 IP，方便手机在同一网络下连接"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_resource_dir():
    """获取资源文件所在目录（兼顾源码运行与 PyInstaller 打包后的环境）"""
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        if os.path.exists(os.path.join(exe_dir, "templates")):
            return exe_dir
        if hasattr(sys, '_MEIPASS'):
            return sys._MEIPASS
        return exe_dir
    return os.path.abspath(os.path.dirname(__file__))


def run():
    project_root = get_resource_dir()
    template_path = os.path.join(project_root, "templates", "index.html")
    template = open(template_path, "r", encoding="utf-8").read() if os.path.exists(template_path) else "<h1>网易云音乐远程控制器</h1>"

    port = 10010
    local_ip = get_local_ip()

    print("=" * 60, flush=True)
    print(" 网易云音乐远程控制服务已就绪！", flush=True)
    print(f" 本机局域网 IP: {local_ip}", flush=True)
    print(f" 电脑访问地址: http://127.0.0.1:{port}/", flush=True)
    print(f" 手机访问地址: http://{local_ip}:{port}/", flush=True)
    print(" 当前生效的网易云音乐快捷键：", flush=True)
    print("   - 播放/暂停: Ctrl + Alt + P", flush=True)
    print("   - 上一首:     Ctrl + Alt + Left", flush=True)
    print("   - 下一首:     Ctrl + Alt + Right", flush=True)
    print("   - 音量加:     Ctrl + Alt + Up", flush=True)
    print("   - 音量减:     Ctrl + Alt + Down", flush=True)
    print("=" * 60, flush=True)
    print("提示：在手机浏览器输入上述手机访问地址，即可控制网易云音乐。", flush=True)
    print("按 Ctrl + C 可退出服务。\n", flush=True)

    # 优先使用 Flask；若未安装 Flask 则自动使用 Python 原生内置服务（零安装依赖）
    try:
        from flask import Flask, request, jsonify
        app = Flask(__name__)

        @app.route('/')
        def hello_world():
            action = request.args.get("action")
            if action and hasattr(MusicBox, action):
                getattr(MusicBox, action)()
            return template

        @app.route('/mobile_connect')
        def mobile_connect():
            response = jsonify(code=200, message="Connected", platform="win", status=1, version="0.0.1")
            response.status_code = 200
            return response

        app.run(host="0.0.0.0", port=port)

    except ImportError:
        import json
        from http.server import HTTPServer, BaseHTTPRequestHandler
        from urllib.parse import urlparse, parse_qs

        class NativeHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                print(f"[{self.log_date_time_string()}] {args[0]}", flush=True)

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)

                # 移动端握手接口
                if path == "/mobile_connect":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    data = {"code": 200, "message": "Connected", "platform": "win", "status": 1, "version": "0.0.1"}
                    self.wfile.write(json.dumps(data).encode("utf-8"))
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

                # 主控制网页
                if path == "/":
                    action = query.get("action", [None])[0]
                    if action and hasattr(MusicBox, action):
                        getattr(MusicBox, action)()

                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(template.encode("utf-8"))
                    return

                self.send_response(404)
                self.end_headers()

        server = HTTPServer(("0.0.0.0", port), NativeHandler)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n服务已停止。", flush=True)
            server.server_close()


if __name__ == '__main__':
    run()
