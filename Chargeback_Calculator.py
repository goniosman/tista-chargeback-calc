#!/usr/bin/env python3
"""
Tista Chargeback Calculator - Standalone Desktop Application
Launches the calculator in a dedicated, native desktop application window
without opening a web browser or showing URL bars/tabs.
"""
import sys
import os

# Safe stream wrappers so print never fails in windowed/GUI/frozen mode
if sys.stdout is None:
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass
if sys.stderr is None:
    try:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass
if sys.stdin is None:
    class DummyStdin:
        def readline(self): return ""
        def read(self, *a): return ""
        def isatty(self): return False
    sys.stdin = DummyStdin()

import time
import socket
import threading
import subprocess
import urllib.request
from pathlib import Path

# Resolve base directories
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
    APP_DIR = Path(sys.executable).parent
    bundled_html = BUNDLE_DIR / "Chargeback Calculator.html"
    target_html = APP_DIR / "Chargeback Calculator.html"
    if bundled_html.exists() and not target_html.exists():
        try:
            import shutil
            shutil.copy2(bundled_html, target_html)
        except Exception:
            pass
else:
    APP_DIR = Path(__file__).parent
    BUNDLE_DIR = APP_DIR

os.chdir(APP_DIR)
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(BUNDLE_DIR))

# Import server module
try:
    import tista_delay_fetch
except ImportError:
    tista_delay_fetch = None


def is_port_in_use(port=8765):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def verify_server_http(port=8765, timeout=1.0):
    """Verifies that the server is not just bound, but actually serving HTTP 200."""
    try:
        url = f"http://127.0.0.1:{port}/Chargeback%20Calculator.html"
        req = urllib.request.Request(url, headers={"User-Agent": "HealthCheck"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_internal_server(port=8765):
    """Starts the calculator HTTP server."""
    if tista_delay_fetch:
        tista_delay_fetch.start_server(port=port, host="127.0.0.1")
    else:
        import http.server
        import socketserver
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, format, *args): pass
        with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
            httpd.serve_forever()


def ensure_server(port=8765):
    """Starts the internal background server if not already responsive."""
    if verify_server_http(port):
        return True

    # If port is in use by a dead/unresponsive socket, wait slightly
    if not is_port_in_use(port):
        t = threading.Thread(
            target=start_internal_server,
            args=(port,),
            daemon=True
        )
        t.start()

    # Wait up to 8 seconds for server to be responsive to HTTP GET
    for _ in range(32):
        time.sleep(0.25)
        if verify_server_http(port):
            return True
    return False


def get_standalone_app_launcher():
    """Finds Edge or Chrome executable for native --app standalone window mode on Windows or Mac."""
    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            os.path.expanduser("~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            os.path.expanduser("~/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
        ]
    else:
        candidates = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    for exe in candidates:
        if os.path.exists(exe):
            return exe
    return None


def run_desktop_app():
    port = 8765
    target_url = f"http://127.0.0.1:{port}/Chargeback%20Calculator.html"

    # Start the server and wait until HTTP 200 is confirmed
    server_ok = ensure_server(port)
    if not server_ok:
        # Try alternate port if 8765 is blocked
        for alt_port in [8766, 8767, 8768]:
            if not is_port_in_use(alt_port):
                port = alt_port
                target_url = f"http://127.0.0.1:{port}/Chargeback%20Calculator.html"
                server_ok = ensure_server(port)
                if server_ok:
                    break

    # Open app window
    app_exe = get_standalone_app_launcher()
    opened = False
    if app_exe:
        cmd = [
            app_exe,
            f"--app={target_url}",
            "--window-size=1360,900"
        ]
        try:
            subprocess.Popen(cmd)
            opened = True
        except Exception as e:
            pass

    if not opened:
        import webbrowser
        webbrowser.open(target_url)

    # Keep process alive so the background server never shuts down
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        pass


def main():
    run_desktop_app()


if __name__ == "__main__":
    main()
