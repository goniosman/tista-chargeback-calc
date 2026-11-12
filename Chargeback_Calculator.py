#!/usr/bin/env python3
"""
Tista Chargeback Calculator - Standalone Desktop Application
Launches the calculator in a dedicated, native desktop application window
without opening a web browser or showing URL bars/tabs.
"""
import sys
import os
import time
import socket
import threading
import subprocess
from pathlib import Path

# Resolve base directories
if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

os.chdir(APP_DIR)
sys.path.insert(0, str(APP_DIR))

# Import server module
try:
    import tista_delay_fetch
except ImportError:
    tista_delay_fetch = None


def is_port_in_use(port=8765):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_server(port=8765):
    """Starts the internal background server if not already active."""
    if is_port_in_use(port):
        return True

    if tista_delay_fetch:
        t = threading.Thread(
            target=tista_delay_fetch.start_server,
            kwargs={"port": port, "host": "0.0.0.0"},
            daemon=True
        )
        t.start()

    # Wait up to 5 seconds for server to be responsive
    for _ in range(20):
        time.sleep(0.25)
        if is_port_in_use(port):
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
    # 1. Start internal server silently
    ensure_server(8765)

    target_url = "http://127.0.0.1:8765/Chargeback%20Calculator.html"
    app_exe = get_standalone_app_launcher()

    # 2. Preferred: Native Standalone App Window (No address bar, no tabs, native desktop frame)
    if app_exe:
        cmd = [
            app_exe,
            f"--app={target_url}",
            "--window-size=1360,900",
            "--window-position=center",
            "--disable-extensions",
            "--disable-default-apps"
        ]
        try:
            # Run the standalone window process and wait until user closes the window
            proc = subprocess.Popen(cmd)
            proc.wait()
            return
        except Exception as e:
            print(f"Standalone app mode launch error: {e}")

    # 3. Fallback: pywebview
    try:
        import webview
        window = webview.create_window(
            title="Tista Chargeback Calculator",
            url=target_url,
            width=1360,
            height=900,
            min_size=(960, 600),
            background_color="#0d1117"
        )
        webview.start()
        return
    except Exception as e:
        print(f"pywebview fallback error: {e}")

    # 4. Final fallback: default browser
    import webbrowser
    webbrowser.open(target_url)


def main():
    run_desktop_app()


if __name__ == "__main__":
    main()
