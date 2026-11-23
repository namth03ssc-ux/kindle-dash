#!/usr/bin/env python3
"""
Máy chủ nhỏ cho dashboard Kindle (v1). Chạy trên máy tính trong mạng Wi-Fi nhà.

- Phục vụ trang dashboard tại  http://<IP máy tính>:8765/
- /usage.json  : số liệu mức dùng Claude (tự làm mới nếu cũ hơn 10 phút)
- /refresh     : làm mới ngay (nút "Cập nhật" trên Kindle gọi vào đây)

Cách lấy số liệu: đọc mã đăng nhập Claude Code có sẵn trên máy
(%USERPROFILE%\\.claude\\.credentials.json) rồi hỏi máy chủ Anthropic mức dùng,
giống lệnh /usage trong Claude Code. Không tốn token.
Nếu mã đăng nhập đã hết hạn, script chạy `claude -p` một câu ngắn bằng Haiku để
Claude Code tự gia hạn mã, rồi thử lại (lần này tốn một ít token).

Lưu ý: địa chỉ /api/oauth/usage là địa chỉ nội bộ Claude Code dùng, Anthropic
không công bố. Nếu sau này họ đổi, script sẽ báo lỗi trên Kindle.

Chỉ dùng thư viện chuẩn Python 3. Không cần cài thêm gì.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("KINDLE_PORT", "8765"))
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "index.html")
USAGE_FILE = os.path.join(HERE, "usage.json")
CRED_FILE = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
AUTO_EVERY = 10 * 60   # /usage.json tự làm mới nếu cũ hơn 10 phút
MIN_GAP = 30           # nút Cập nhật: tối thiểu 30 giây giữa hai lần hỏi Anthropic
GIST_ID = os.environ.get("KINDLE_GIST_ID", "").strip()  # tuỳ chọn: đồng bộ lên Gist

_lock = threading.Lock()
_state = {"last_try": 0.0}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def read_token():
    with open(CRED_FILE, encoding="utf-8") as f:
        cred = json.load(f)
    tok = (cred.get("claudeAiOauth") or {}).get("accessToken")
    if not tok:
        raise RuntimeError("Không thấy mã đăng nhập Claude Code trong " + CRED_FILE)
    return tok


def ask_anthropic(token):
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": "kindle-dashboard",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def renew_login():
    """Cho Claude Code tự gia hạn mã đăng nhập bằng một câu hỏi rất ngắn."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("Mã đăng nhập hết hạn và không tìm thấy lệnh claude để gia hạn")
    log("Mã đăng nhập hết hạn, gọi claude -p để gia hạn")
    kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "timeout": 120}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000  # không bật cửa sổ
    subprocess.run([exe, "-p", "Trả lời: ok", "--model", "haiku"], **kw)


def window(raw):
    if not isinstance(raw, dict):
        return None
    pct = raw.get("utilization")
    if pct is None:
        pct = raw.get("used_percentage")
    if pct is None:
        return None
    pct = float(pct)
    return {"used_percentage": pct, "resets_at": raw.get("resets_at")}


def fetch_usage():
    try:
        data = ask_anthropic(read_token())
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        renew_login()
        data = ask_anthropic(read_token())
    payload = {
        "updated": int(time.time()),
        "five_hour": window(data.get("five_hour")),
        "seven_day": window(data.get("seven_day")),
        "models": [],
    }
    if not payload["five_hour"] and not payload["seven_day"]:
        raise RuntimeError("Anthropic trả về dữ liệu không như mong đợi: " + json.dumps(data)[:200])
    tmp = USAGE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, USAGE_FILE)
    if GIST_ID and shutil.which("gh"):
        kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000
        subprocess.Popen(["gh", "gist", "edit", GIST_ID, "-f", "usage.json", USAGE_FILE], **kw)
    log("Đã cập nhật: 5h %.0f%%, tuần %.0f%%" % (
        (payload["five_hour"] or {}).get("used_percentage", -1),
        (payload["seven_day"] or {}).get("used_percentage", -1)))
    return payload


def cached():
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def get_usage(force):
    """Trả (payload, lỗi hoặc None). Luôn có payload (có thể là số cũ)."""
    with _lock:
        old = cached()
        age = time.time() - float(old.get("updated") or 0)
        if not force and age < AUTO_EVERY:
            return old, None
        if time.time() - _state["last_try"] < MIN_GAP:
            return old, None
        _state["last_try"] = time.time()
        try:
            return fetch_usage(), None
        except Exception as e:  # noqa: BLE001 - báo lỗi lên Kindle, không dừng máy chủ
            log("Lỗi: %s" % e)
            return old, str(e)


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                with open(PAGE, "rb") as f:
                    self.send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self.send(404, "Thiếu index.html cạnh script", "text/plain; charset=utf-8")
        elif path in ("/usage.json", "/refresh"):
            data, err = get_usage(force=(path == "/refresh"))
            out = dict(data)
            if err:
                out["error"] = err
            self.send(200, json.dumps(out, ensure_ascii=False), "application/json; charset=utf-8")
        else:
            self.send(404, "Không có", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        pass  # tắt log mỗi lần Kindle gọi


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    if "--test" in sys.argv:
        print(json.dumps(fetch_usage(), ensure_ascii=False, indent=2))
        return
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log("Dashboard Kindle: mở http://%s:%d/ trên Kindle (cùng Wi-Fi)" % (lan_ip(), PORT))
    srv.serve_forever()


if __name__ == "__main__":
    main()
