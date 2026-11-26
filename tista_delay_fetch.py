#!/usr/bin/env python3
"""
Tista Automatic Delay Chargeback Fetcher & Calculator Service
Fetches delay-chargeback parameters directly from tista.app for any Order ID.

Usage:
  1. CLI:          py -3.11 tista_delay_fetch.py 458305
  2. Interactive:  py -3.11 tista_delay_fetch.py (prompts for Order ID)
  3. Server:       py -3.11 tista_delay_fetch.py --serve (runs local API on http://127.0.0.1:8765)
"""

import json
import re
import sys
import os
import gc
import time
import threading
import shutil
import tempfile
import urllib.request
from pathlib import Path
from http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote
from concurrent.futures import ThreadPoolExecutor
from link_files import extract_files_from_links, clean_link

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import html
from html import unescape as html_unescape
try:
    import requests
except ImportError:
    requests = None

# Safe stream wrappers so print never fails in windowed/GUI mode
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

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

# Application constants & version
SERVER_VERSION = "2026.11.25.v4"
BASE_URL = "https://tista.app"
DISPUTES_URL = f"{BASE_URL}/manage/vendors/1/order_disputes"
NAVBAR_SEARCH_XPATH = '//*[@id="navbar"]/form/div[1]/input'
APP_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
BUNDLE_DIR = Path(getattr(sys, '_MEIPASS', APP_DIR))

# Writable data directory for caches and close_log.jsonl.
# On Render the app folder is reset on every deploy/restart, so the persistent disk at /data is
# preferred when it exists (override with TISTA_CACHE_DIR). Desktop: the app folder, or
# ~/.tista-data when that is read-only (DMG volumes / Applications on macOS).
def _resolve_data_dir() -> Path:
    custom = os.environ.get("TISTA_CACHE_DIR")
    if custom:
        return Path(custom)
    persistent = Path("/data")
    if persistent.is_dir() and os.access(persistent, os.W_OK):
        return persistent
    try:
        if os.access(APP_DIR, os.W_OK):
            return APP_DIR
    except Exception:
        pass
    return Path.home() / ".tista-data"

DATA_DIR = _resolve_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Cloud / Headless auto-detection (True by default so data fetching runs silently in background)
IS_CLOUD_ENV = bool(os.environ.get("RENDER") or os.environ.get("HEADLESS", "").lower() in ("1", "true", "yes"))
DEFAULT_HEADLESS = True

def resolve_profile_dir() -> Path:
    """
    Finds the active Chrome profile directory with Tista cookies,
    checking local directory, sibling Report Apps directories, and home directory.
    Follows unified_app architecture.
    """
    custom = os.environ.get("TISTA_DATA_DIR")
    if custom:
        p = Path(custom)
        if p.exists():
            return p

    local_chrome = APP_DIR / "chrome-profile"
    candidates = [
        local_chrome,
        APP_DIR / ".tista-profile",
        Path(r"D:\Osman_File\_Report Apps\__Reporting Automation__\Chargeback data\chrome-profile"),
        Path(r"D:\Osman_File\_Report Apps\Chargeback data\chrome-profile"),
        Path(r"D:\Osman_File\_Report Apps\__Reporting Automation__\unified_app\chrome-profile"),
        Path.home() / ".tista-profile",
        Path.home() / "chrome-profile",
    ]

    for p in candidates:
        if not p.exists():
            continue
        for cp in [p / "Default" / "Network" / "Cookies", p / "Default" / "Cookies", p / "Cookies"]:
            if cp.exists() and cp.stat().st_size > 0:
                return p

    try:
        local_chrome.mkdir(parents=True, exist_ok=True)
        return local_chrome
    except Exception:
        return Path.home() / ".tista-profile"

USER_DATA_DIR = resolve_profile_dir()

def launch_playwright_context(p, headless=DEFAULT_HEADLESS):
    """
    Launches persistent context using Google Chrome channel and anti-automation flag,
    matching unified_app. Includes profile lock handling with temp profile fallback.
    """
    profile = resolve_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)

    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--js-flags=--max-old-space-size=64",
        "--renderer-process-limit=1",
        "--single-process",
        "--no-zygote",
        "--disable-software-rasterizer",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-sync",
        "--disable-translate",
        "--disk-cache-size=1",
        "--media-cache-size=1",
        "--safebrowsing-disable-auto-update",
        "--mute-audio"
    ]
    viewport = {"width": 1280, "height": 800}

    ctx = None
    # 1. Try Google Chrome channel first
    try:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel="chrome",
            headless=headless,
            viewport=viewport,
            args=args
        )
    except Exception as e1:
        print(f"[*] Chrome channel launch note: {e1}")

    # 2. Try standard Playwright Chromium with primary profile
    if not ctx:
        try:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=headless,
                viewport=viewport,
                args=args
            )
        except Exception as e2:
            print(f"[*] Standard persistent profile locked or failed: {e2}")

    # 3. Fallback to temp directory with copied cookies if primary profile is locked
    if not ctx:
        temp_dir = Path(tempfile.mkdtemp(prefix="tista_chrome_"))
        print(f"[*] Primary profile locked, falling back to temp profile: {temp_dir}")
        try:
            for c_src in [profile / "Default" / "Network" / "Cookies", profile / "Default" / "Cookies"]:
                if c_src.exists():
                    target = temp_dir / "Default" / "Network"
                    target.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(c_src, target / "Cookies")
                    break
        except Exception:
            pass

        try:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(temp_dir),
                channel="chrome",
                headless=headless,
                viewport=viewport,
                args=args
            )
        except Exception:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(temp_dir),
                headless=headless,
                viewport=viewport,
                args=args
            )

    # Automatically load saved session cookies (e.g. from Render cookie sync)
    for c_path in [profile / "saved_cookies.json", APP_DIR / "saved_cookies.json", Path("/data/saved_cookies.json")]:
        if c_path.exists():
            try:
                with open(c_path, "r", encoding="utf-8") as f:
                    sc = json.load(f)
                    if sc and isinstance(sc, list):
                        ctx.add_cookies(sc)
                        print(f"[✓] Loaded {len(sc)} saved cookies from {c_path.name}")
                        break
            except Exception as e:
                print(f"[!] Error loading saved cookies: {e}")

    return ctx

KNOWN_SERVICES = [
    "Multi-clipping path", "Clipping path", "Image masking", "Drop shadow",
    "Existing shadow", "Natural shadow", "Floating shadow", "Reflection shadow",
    "Ghost mannequin", "Dust, spot and scratch removal", "Wrinkle on clothing",
    "Beauty airbrushing", "Camera reflection removal", "Color variants",
    "Logo", "Artwork", "Line drawing", "Illustration", "Additional copy",
    "File format change", "Straighten, crop and set margin", "Resize",
    "Renaming", "Symmetrical edit", "Background removal"
]

KNOWN_CATEGORIES = [
    "Category 1", "Category 1.5", "Category 2", "Category 2.25", "Category 2.5",
    "Category 2.75", "Category 3", "Category 3.25", "Category 3.5", "Category 3.75",
    "Category 4", "Category 4.25", "Category 4.5", "Category 4.75", "Category 5",
    "Category 5.5", "Category 6"
]

def resolve_service_name(s_name: str, fallback_to_input: bool = False) -> str:
    """
    Robust service name resolver that correctly matches Tista service names,
    preventing substrings like 'Clipping path' from accidentally hijacking 'Multi-clipping path'.
    Strictly filters out non-service strings like Bengali instructions, production notes, and URLs.
    """
    if not s_name:
        return ""
    s = str(s_name).strip()

    # Strictly reject non-service text: Bengali text, emojis, long sentences, or note keywords
    if re.search(r"[\u0980-\u09FF\U00010000-\U0010ffff\u2600-\u27BF]", s):
        return ""
    if len(s) > 45:
        return ""
    s_low = s.lower()
    if any(bad in s_low for bad in ["approval", "completed file", "on hold", "instruction", "production note", "category-এর", "কাজ", "করতে হবে", "পাঠাতে হবে"]):
        return ""

    # 1. Exact match check
    for ks in KNOWN_SERVICES:
        if ks.lower() == s_low:
            return ks

    # 2. Priority check: Multi-clipping MUST be checked before generic clipping!
    if "multi-clipping" in s_low or "multiclipping" in s_low or "multi clipping" in s_low:
        return "Multi-clipping path"
    if "clipping" in s_low or "clip" in s_low:
        return "Clipping path"

    # 3. Known heuristics
    if "wrinkle" in s_low:
        return "Wrinkle on clothing"
    if "dust" in s_low or "scratch" in s_low:
        return "Dust, spot and scratch removal"
    if "ghost" in s_low or "mannequin" in s_low or "neck joint" in s_low:
        return "Ghost mannequin"
    if "mask" in s_low:
        return "Image masking"
    if "natural shadow" in s_low:
        return "Natural shadow"
    if "drop shadow" in s_low:
        return "Drop shadow"
    if "reflection shadow" in s_low:
        return "Reflection shadow"
    if "existing shadow" in s_low:
        return "Existing shadow"
    if "floating shadow" in s_low:
        return "Floating shadow"
    if "camera reflection" in s_low:
        return "Camera reflection removal"
    if "beauty" in s_low or "airbrush" in s_low:
        return "Beauty airbrushing"
    if "color variant" in s_low:
        return "Color variants"
    if "symmetrical" in s_low:
        return "Symmetrical edit"
    if "background removal" in s_low:
        return "Background removal"
    if "straighten" in s_low or "crop" in s_low:
        return "Straighten, crop and set margin"
    if "resize" in s_low:
        return "Resize"
    if "renaming" in s_low:
        return "Renaming"
    if "file format" in s_low:
        return "File format change"

    # 4. Longest-first match among KNOWN_SERVICES
    for ks in sorted(KNOWN_SERVICES, key=len, reverse=True):
        if ks.lower() in s_low:
            return ks

    return s if fallback_to_input else ""

def deduplicate_file_services(svcs):
    """
    Given a list of raw service dicts from Tista for a single file,
    resolves service names, normalizes categories, and deduplicates identical services
    (which happen on Tista due to multiple worker QC logs, revisions, or feedback notes).
    """
    clean_svcs = []
    seen = {}
    for s in svcs:
        s_name = s.get("raw_name") or s.get("name") or s.get("service") or s.get("group") or ""
        matched_svc = resolve_service_name(s_name)
        if not matched_svc:
            continue
        comp = s.get("complexity") or ""
        cat_m = re.search(r"(\d+(?:\.\d+)?)", comp)
        num_cat = float(cat_m[1]) if cat_m else 1.0
        cat_label = f"Category {cat_m[1]}" if cat_m else "Category 1"

        k = matched_svc.lower()
        if k in seen:
            if num_cat > seen[k]["numeric_cat"]:
                seen[k]["complexity"] = comp or f"C{num_cat}"
                seen[k]["category"] = cat_label
                seen[k]["numeric_cat"] = num_cat
            continue

        item = {
            "service": matched_svc,
            "raw_name": s_name,
            "complexity": comp or f"C{num_cat}",
            "category": cat_label,
            "numeric_cat": num_cat
        }
        seen[k] = item
        clean_svcs.append(item)
    return clean_svcs

LOCATION_ID_MAP = {
    "": "",
    "all": "",
    "all locations": "",
    "1": "1",
    "path": "1",
    "2": "2",
    "subs": "2",
    "7": "7",
    "grasigner kitchen": "7",
    "46": "46",
    "perfect pixel (gb2)": "46",
    "perfect pixel": "46",
    "47": "47",
    "pixel path (p2)": "47",
    "pixel path": "47",
    "48": "48",
    "creative path (p1)": "48",
    "creative path": "48",
    "50": "50",
    "khandoker graphics": "50",
    "53": "53",
    "graphics picture": "53",
    "91": "91",
    "vectorio": "91",
    "96": "96",
    "cutout edge": "96",
    "100": "100",
    "edgebo": "100",
    "101": "101",
    "healing image": "101",
    "135": "135",
    "clippingpix": "135",
}

def hours_to_seconds(text):
    """Converts hour strings (e.g., '01:43:05', '12h', '5.5', '5.5 hours', '5h 30m') into seconds."""
    t = (text or "").strip().lower()
    if not t:
        return None
    # Format hh:mm:ss or mm:ss
    m = re.fullmatch(r"-?(\d+):(\d{1,2})(?::(\d{1,2}))?", t)
    if m:
        return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3] or 0)
    d = re.search(r"(\d+(?:\.\d+)?)\s*d", t)
    h = re.search(r"(\d+(?:\.\d+)?)\s*h", t)
    mi = re.search(r"(\d+(?:\.\d+)?)\s*m(?!o)", t)
    s = re.search(r"(\d+(?:\.\d+)?)\s*s", t)
    if d or h or mi or s:
        return round(sum(float(x[1]) * k for x, k in ((d, 86400), (h, 3600), (mi, 60), (s, 1)) if x))
    n = re.search(r"-?\d+(?:\.\d+)?", t)
    return round(float(n[0]) * 3600) if n else None

parse_duration_to_seconds = hours_to_seconds
parseDur = hours_to_seconds


def ensure_logged_in(page):
    """Ensures user is logged into Tista. If on login page, attempts auto-login with env credentials or waits for user."""
    page.goto(DISPUTES_URL, wait_until="domcontentloaded")
    time.sleep(1)

    is_login = ("sign_in" in page.url or "login" in page.url or page.locator('input[type="password"]').count() > 0)
    if is_login:
        email = os.environ.get("TISTA_EMAIL")
        password = os.environ.get("TISTA_PASSWORD")
        if email and password:
            print("\n[✓] Logging in with TISTA_EMAIL / TISTA_PASSWORD environment credentials...")
            try:
                email_input = page.locator('#user_email, input[type="email"], input[name*="email"]').first
                pass_input = page.locator('#user_password, input[type="password"], input[name*="password"]').first
                if email_input.count() > 0 and pass_input.count() > 0:
                    email_input.fill(email)
                    pass_input.fill(password)
                    rem = page.locator('#user_remember_me, input[name*="remember_me"]').first
                    if rem.count() > 0 and not rem.is_checked():
                        rem.check()
                    submit_btn = page.locator('input[type="submit"], button[type="submit"]').first
                    if submit_btn.count() > 0:
                        submit_btn.click()
                    else:
                        pass_input.press("Enter")

                    # Wait up to 20 seconds for login redirection
                    start_nav = time.time()
                    while time.time() - start_nav < 20:
                        time.sleep(1)
                        if "sign_in" not in page.url.lower() and "login" not in page.url.lower() and page.locator('input[type="password"]').count() == 0:
                            break
            except Exception as e:
                print(f"[!] Auto-login exception: {e}")

        is_still_login = ("sign_in" in page.url or "login" in page.url or page.locator('input[type="password"]').count() > 0)
        if is_still_login:
            if DEFAULT_HEADLESS:
                raise RuntimeError("Not logged into Tista. Please click '🔑 Tista Login' in the calculator or run login_tista.bat.")

            print("\n" + "=" * 60)
            print("[!] TISTA LOGIN REQUIRED:")
            print("    Please log in to Tista in the browser window.")
            print("    Your session will be saved automatically for future runs.")
            print("=" * 60 + "\n")

            try:
                rem = page.locator('#user_remember_me, input[name*="remember_me"]').first
                if rem.count() > 0 and not rem.is_checked():
                    rem.check()
            except Exception:
                pass

            try:
                page.bring_to_front()
            except Exception:
                pass

            start_time = time.time()
            while "sign_in" in page.url or "login" in page.url or page.locator('input[type="password"]').count() > 0:
                time.sleep(1)
                if time.time() - start_time > 300:
                    raise TimeoutError("Login timed out after 5 minutes.")

        print("[✓] Login verified! Saved to profile.\n")
        time.sleep(1)

def fetch_order_from_tista(order_id: str, headless: bool = True):
    """
    Automates Tista login, dispute extraction, and order file parsing.
    Supports Order ID, Dispute ID, and direct URLs (Tista, Dropbox, etc.).
    Uses smart caching and fast HTTP queries to return in ~0.5s when possible.
    """
    raw_id = str(order_id).strip()
    digits = re.sub(r"\D", "", raw_id)

    # Check if a URL was pasted and extract the clean Order/Dispute ID
    if "http://" in raw_id or "https://" in raw_id:
        if "dropbox.com" in raw_id or "feedback" in raw_id.lower():
            m_hash = re.search(r"(?:%23|#)(\d{5,7})", raw_id)
            if m_hash: digits = m_hash.group(1)
            else:
                m_six = re.search(r"\b(\d{6})\b", raw_id)
                if m_six: digits = m_six.group(1)
        elif "tista.app" in raw_id:
            m_q = re.search(r"[?&]query=([^&#]+)", raw_id)
            if m_q: digits = re.sub(r"\D", "", m_q.group(1))
            elif "order_name_eq" in raw_id:
                m_eq = re.search(r"order_name_eq\]=([^&#]+)", raw_id)
                if m_eq: digits = re.sub(r"\D", "", m_eq.group(1))
            elif "/order_disputes/" in raw_id:
                m_disp = re.search(r"/order_disputes/(\d+)", raw_id)
                if m_disp: digits = m_disp.group(1)
            elif "/orders/" in raw_id:
                m_ord = re.search(r"/orders/(\d+)", raw_id)
                if m_ord: digits = m_ord.group(1)

    search_with_hash = f"#{digits}" if digits else raw_id
    oid = digits or raw_id.replace("#", "")

    if not oid:
        return {"error": "Invalid Order ID"}

    # 1. Local Cache Check (Instant return in ~0.005s)
    cached_delay = DATA_DIR / f"tista_{oid}.json"
    if not cached_delay.exists(): cached_delay = APP_DIR / f"tista_{oid}.json"
    if cached_delay.exists():
        try:
            cdata = json.loads(cached_delay.read_text(encoding="utf-8"))
            if cdata and cdata.get("order_id"):
                print(f"[✓] Returning cached dispute & order data for #{oid}")
                return cdata
        except Exception:
            pass

    out = {
        "order_id": oid,
        "dispute_id": "",
        "dispute_reason": "Correction",
        "contractor": "",
        "dispute_location": "",
        "fault_by": "",
        "delayed_by_text": "",
        "delayed_by_seconds": None,
        "delay_seconds": None,
        "fault_quantity": None,
        "fault_severity": "Low",
        "status": "",
        "order_duration_text": "24:00:00",
        "order_duration_seconds": 86400,
        "pixel": "> 5000",
        "service_type": "",
        "category_level": "",
        "average_category": None,
        "services": [],
        "warnings": []
    }

    # 2. Fast HTTP check for dispute record before starting heavy browser
    http_sess = get_tista_http_session()
    http_found = False
    if http_sess:
        for cand_val in [f"%23{oid}", oid]:
            cand_url = f"{DISPUTES_URL}?q[id_or_fulfillment_order_order_name_eq]={cand_val}"
            try:
                resp = http_sess.get(cand_url, timeout=5)
                if resp.status_code == 200 and "order_disputes" in resp.url and "sign_in" not in resp.url:
                    trs = re.findall(r'<tr[^>]*>(.*?)</tr>', resp.text, re.DOTALL)
                    for tr in trs:
                        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.DOTALL)
                        if len(tds) >= 11:
                            cells = [re.sub(r'<[^>]+>', '', td).strip() for td in tds]
                            if cells[0].isdigit():
                                out["dispute_id"] = cells[0]
                                out["dispute_location"] = cells[2]
                                out["contractor"] = cells[2]
                                out["fault_by"] = cells[3]
                                out["dispute_reason"] = cells[4] or "Correction"
                                out["chargeable_to"] = cells[5]
                                out["fault_at"] = cells[7]
                                out["delayed_by_text"] = cells[8]
                                sec = hours_to_seconds(cells[8])
                                out["delayed_by_seconds"] = sec
                                out["delay_seconds"] = -abs(sec) if sec is not None else None
                                q_match = re.search(r"\d+(?:\.\d+)?", cells[9])
                                out["fault_quantity"] = float(q_match[0]) if q_match else 1.0
                                out["fault_severity"] = cells[10] or "Low"
                                out["status"] = cells[11] if len(cells) > 11 else "Open"
                                http_found = True
                                print(f"[✓] Fast HTTP found Dispute #{out['dispute_id']} for #{oid}: Contractor='{out['contractor']}', Reason='{out['dispute_reason']}'")
                                break
                if http_found:
                    break
            except Exception as he:
                print(f"[*] Fast HTTP dispute search note: {he}")

        if http_found and out.get("dispute_id"):
            # Fetch comment & link via HTTP
            try:
                resp_e = http_sess.get(f"{BASE_URL}/manage/vendors/1/order_disputes/{out['dispute_id']}/edit", timeout=5)
                if resp_e.status_code == 200:
                    m_comm = re.search(r'<textarea[^>]+(?:id=[\x22\x27]order_dispute_comment[\x22\x27]|name=[^>]*comment)[^>]*>([\s\S]*?)</textarea>', resp_e.text, re.I)
                    if m_comm:
                        c_val = html.unescape(m_comm.group(1).strip())
                        out["comment"] = c_val
                        urls = [u.rstrip(".,;!?')\"") for u in re.findall(r"https?://[^\s<>\x22\x27,]+", c_val)]
                        if urls:
                            out["comment_link"] = urls[0]
            except Exception:
                pass

        # Check if files already cached or fetch via Fast Direct API (~0.2s)
        cached_corr = DATA_DIR / f"tista_correction_{oid}.json"
        if not cached_corr.exists(): cached_corr = APP_DIR / f"tista_correction_{oid}.json"
        if not cached_corr.exists():
            fetch_order_files_direct_api(oid)
            cached_corr = DATA_DIR / f"tista_correction_{oid}.json"
            if not cached_corr.exists(): cached_corr = APP_DIR / f"tista_correction_{oid}.json"

        if cached_corr.exists():
            try:
                c_corr_data = json.loads(cached_corr.read_text(encoding="utf-8"))
                if c_corr_data.get("order_duration"):
                    out["order_duration_text"] = c_corr_data["order_duration"]
                if c_corr_data.get("contractor") and not out["contractor"]:
                    out["contractor"] = c_corr_data["contractor"]
                out["services"] = c_corr_data.get("files", [])
                for p_save in [DATA_DIR, APP_DIR]:
                    try: (p_save / f"tista_{oid}.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
                    except Exception: pass
                print(f"[✓] Fast combined dispute & cached files for #{oid} returned in <0.5s!")
                return out
            except Exception:
                pass

    if not sync_playwright:
        return {
            "status": "error",
            "error": "Playwright is not installed in the currently active Python environment. Please run: pip install playwright && playwright install chromium"
        }

    with sync_playwright() as p:
        ctx = launch_playwright_context(p, headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Step 1: Ensure user is logged in
        ensure_logged_in(page)

        # Step 2: Search in Disputes Page (supports Order ID with/without hash and Dispute ID)
        print(f"[*] Searching for Order / Dispute '{raw_id}' in Disputes...")
        candidates = []
        if raw_id.startswith("#"):
            candidates = [raw_id, digits]
        elif len(digits) == 5:
            # 5-digit number is typically a Dispute ID (e.g. 47374, 47359)
            candidates = [digits, f"#{digits}"]
        elif len(digits) == 6:
            # 6-digit number is typically an Order ID (e.g. 458305, 458192)
            candidates = [f"#{digits}", digits]
        else:
            candidates = [f"#{digits}" if digits else raw_id, digits or raw_id]

        row = None
        heads = []
        cells = []
        real_oid = digits

        for cand in candidates:
            if not cand: continue
            cand_url = f"{DISPUTES_URL}?q[id_or_fulfillment_order_order_name_eq]={quote(cand)}"
            print(f"[*] Navigating to Dispute search: {cand_url}")
            page.goto(cand_url)
            page.wait_for_load_state("domcontentloaded")
            time.sleep(1.5)
            r = page.locator("table tbody tr").first
            if r.count() > 0:
                txt = r.inner_text().strip()
                if txt and "no data" not in txt.lower() and "displaying 0" not in txt.lower():
                    heads = [h.strip().lower() for h in page.locator("table thead th").all_inner_texts()]
                    cells = [c.strip() for c in r.locator("td").all_inner_texts()]
                    if len(cells) >= 6:
                        row = r
                        break

        if row and cells:
            def get_col_val(column_substring):
                for idx, h in enumerate(heads):
                    if column_substring in h and idx < len(cells):
                        return cells[idx]
                return ""

            out["dispute_id"] = cells[0] if cells else ""
            order_text = get_col_val("order id") or get_col_val("order")
            m_oid = re.search(r"\d+", order_text)
            if m_oid:
                real_oid = m_oid[0]
                out["order_id"] = real_oid

            out["dispute_location"] = get_col_val("dispute location") or get_col_val("location")
            out["contractor"] = out["dispute_location"]
            out["fault_by"] = get_col_val("fault by")

            reason_found = get_col_val("dispute reason") or get_col_val("reason") or "Correction"
            out["dispute_reason"] = reason_found

            delayed_text = get_col_val("delayed by") or get_col_val("delay")
            out["delayed_by_text"] = delayed_text
            sec = hours_to_seconds(delayed_text)
            out["delayed_by_seconds"] = sec
            out["delay_seconds"] = -abs(sec) if sec is not None else None

            qty_text = get_col_val("fault quantity") or get_col_val("quantity") or get_col_val("qty")
            q_match = re.search(r"\d+(?:\.\d+)?", qty_text)
            out["fault_quantity"] = float(q_match[0]) if q_match else 1.0

            out["fault_severity"] = get_col_val("fault severity") or get_col_val("severity") or "Low"
            out["status"] = get_col_val("status")

            print(f"[✓] Found Dispute info: ID={out['dispute_id']}, Order=#{real_oid}, Contractor='{out['contractor']}', Reason='{reason_found}', Delayed='{delayed_text}' ({out['delay_seconds']}s), Fault Qty={out['fault_quantity']}, Severity={out['fault_severity']}")

            # Check Dispute Edit Page for comment & delay link (fast HTTP first)
            if out.get("dispute_id"):
                try:
                    did = out["dispute_id"]
                    print(f"[*] Checking dispute #{did} edit page for comment & link...")
                    edit_url = f"{BASE_URL}/manage/vendors/1/order_disputes/{did}/edit"
                    c_val = ""
                    try:
                        s_http = get_tista_http_session()
                        resp_e = s_http.get(edit_url, timeout=5)
                        if resp_e.status_code == 200:
                            m_comm = re.search(r'<textarea[^>]+(?:id=[\x22\x27]order_dispute_comment[\x22\x27]|name=[^>]*comment)[^>]*>([\s\S]*?)</textarea>', resp_e.text, re.I)
                            if m_comm:
                                c_val = html.unescape(m_comm.group(1).strip())
                    except Exception as he:
                        print(f"[*] Fast HTTP edit check note: {he}")

                    if not c_val and ctx:
                        edit_page = ctx.new_page()
                        try:
                            edit_page.goto(edit_url, wait_until="domcontentloaded", timeout=12000)
                            c_el = edit_page.locator("#order_dispute_comment, textarea[name*='comment']").first
                            if c_el.count() > 0:
                                c_val = c_el.input_value().strip()
                        finally:
                            edit_page.close()

                    if c_val:
                        out["comment"] = c_val
                        print(f"[✓] Extracted dispute comment: {c_val}")
                        urls = [u.rstrip(".,;!?')\"") for u in re.findall(r"https?://[^\s<>\x22\x27,]+", c_val)]
                        if urls:
                            link = urls[0]
                            out["comment_link"] = link
                            print(f"[*] Found link in dispute comment: {link}")
                            files = extract_filenames_from_page(page, link)
                            if files:
                                out["filenames"] = files
                                out["filenames_count"] = len(files)
                                print(f"[✓] Extracted {len(files)} files from dispute comment link!")
                                if str(out.get("dispute_reason", "")).lower() == "delay":
                                    out["fault_quantity"] = float(len(files))
                                    print(f"[✓] Updated fault_quantity to {len(files)} from delay link!")
                except Exception as ce:
                    print(f"[!] Warning reading dispute edit page: {ce}")
                    out["warnings"].append(f"Dispute comment fetch note: {ce}")
        else:
            out["warnings"].append(f"Dispute record for '{raw_id}' was not found in the Dispute table.")

        # Step 3: Search Order in Top Navbar using real_oid
        nav_target = real_oid or oid
        print(f"[*] Searching Order #{nav_target} in top navbar...")
        nav_search = page.locator('xpath=//*[@id="navbar"]/form/div[1]/input').first
        if nav_search.count() == 0:
            nav_search = page.locator('#navbar input, input[placeholder*="Order ID" i], nav input, input[name="q"]').first

        if nav_search.count() > 0:
            try:
                nav_search.fill(nav_target)
                nav_search.press("Enter")
            except Exception as e:
                out["warnings"].append(f"Navbar search error: {e}")
        else:
            page.goto(f"{BASE_URL}/orders?location=all&dateRange=any&query={nav_target}")

        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)

        # Step 4: Extract Turnaround / Duration from Orders Table row
        order_row = page.locator(f'tr:has-text("{nav_target}")').first
        if order_row.count() > 0:
            order_row_text = order_row.inner_text()
            # Turnaround pattern: e.g. "12h" or "24h" or "24:00:00"
            t_match = re.search(r"\b(\d+)\s*h\b", order_row_text)
            if t_match:
                hours_num = int(t_match[1])
                out["order_duration_text"] = f"{hours_num}:00:00"
                out["order_duration_seconds"] = hours_num * 3600
            else:
                out["order_duration_text"] = "24:00:00"
                out["order_duration_seconds"] = 86400
            print(f"[✓] Order Duration (Turnaround): {out['order_duration_text']} ({out['order_duration_seconds']}s)")

            # Click Order files link: title="Order files" or td[1]/div/div[4]/div/a[1]
            files_link = order_row.locator('a[title="Order files"], a[href*="/files"]')
            if files_link.count() == 0:
                files_link = order_row.locator('xpath=.//td[1]/div/div[4]/div/a[1]')

            if files_link.count() > 0:
                print("[*] Opening Order Files page...")
                files_link.first.click()
            else:
                # Direct navigation fallback
                m_id = re.search(r"order-(\d+)", order_row.get_attribute("id") or "")
                if m_id:
                    page.goto(f"{BASE_URL}/orders/{m_id[1]}/files")
        else:
            page.goto(f"{BASE_URL}/orders?location=all&dateRange=any&query={oid}")

        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)

        # Step 5: Read Services & Categories from Filter dropdown on Files page
        print("[*] Opening Filter -> Services dropdown on files page...")
        extracted_categories = []
        detected_service = ""

        # Check file cards on page for exact image service/category tags:
        card_text = page.locator('body').inner_text()
        tags = re.findall(r"(Clipping path|Image masking|Photo retouching|Dust, spot and scratch removal|Multi-clipping path)[^C\n]*C(\d+(?:\.\d+)?)", card_text, re.I)
        if tags:
            print(f"[✓] Extracted {len(tags)} image tags directly from files view!")
            for svc_name, cat_str in tags:
                cat_val = float(cat_str)
                extracted_categories.append({
                    "service": svc_name.strip(),
                    "category": f"Category {cat_str}",
                    "numeric_cat": cat_val,
                    "quantity": 1.0,
                    "raw": f"{svc_name} C{cat_str}"
                })
                if not detected_service:
                    detected_service = svc_name.strip()

        # Also open Filter button to extract categories
        try:
            filter_btn = page.locator('button.custom-dropdown-toggle-btn:has-text("Filter")').first
            if filter_btn.count() > 0:
                filter_btn.click()
                time.sleep(1)
                labels = page.locator('.dropdown-menu.show .filter-line-label').all_inner_texts()
                current_svc = ""
                for lb in labels:
                    txt = lb.strip()
                    res_s = resolve_service_name(txt)
                    if res_s != txt:
                        current_svc = res_s
                    cat_match = re.search(r"category\s*(\d+(?:\.\d+)?)", txt, re.I)
                    if cat_match:
                        cat_val = float(cat_match[1])
                        # If not already present from card tags
                        if not tags:
                            extracted_categories.append({
                                "service": current_svc or "Clipping path",
                                "category": f"Category {cat_match[1]}",
                                "numeric_cat": cat_val,
                                "quantity": 1.0,
                                "raw": txt
                            })
                        if current_svc and not detected_service:
                            detected_service = current_svc
        except Exception as e:
            out["warnings"].append(f"Filter dropdown note: {e}")

        out["services"] = extracted_categories

        # Step 6: Calculate Average Category & Category Level
        if extracted_categories:
            total_qty = sum(x["quantity"] for x in extracted_categories)
            avg_cat = sum(x["numeric_cat"] * x["quantity"] for x in extracted_categories) / total_qty
            out["average_category"] = round(avg_cat, 2)

            def cat_num(name):
                m = re.search(r"\d+(?:\.\d+)?", name)
                return float(m[0]) if m else 0.0

            closest_cat = min(KNOWN_CATEGORIES, key=lambda c: abs(cat_num(c) - avg_cat))
            out["category_level"] = closest_cat

            svcs_with_cats = [x["service"] for x in extracted_categories if x["service"]]
            if svcs_with_cats:
                from collections import Counter
                out["service_type"] = Counter(svcs_with_cats).most_common(1)[0][0]
            elif detected_service:
                out["service_type"] = detected_service
            else:
                out["service_type"] = "Clipping path"
        else:
            out["service_type"] = detected_service or "Clipping path"
            out["category_level"] = "Category 2"
            out["average_category"] = 2.0

        ctx.close()

    output_file = DATA_DIR / f"tista_{oid}.json"
    output_file.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[✓] Successfully saved fetched data to: {output_file}")
    return out

def parse_file_services(services_raw):
    lines = [l.strip() for l in (services_raw or "").replace("\r", "").split("\n") if l.strip()]
    parsed = []
    current_services = []
    for line in lines:
        cat_match = re.search(r"\bC(\d+(?:\.\d+)?)\b|Category\s*(\d+(?:\.\d+)?)", line, re.I)
        if cat_match:
            cat_val = float(cat_match[1] or cat_match[2])
            cat_label = f"Category {cat_match[1] or cat_match[2]}"
            matched_svc = ""
            for s_line in reversed(current_services):
                res_s = resolve_service_name(s_line)
                if res_s != s_line or res_s in KNOWN_SERVICES:
                    matched_svc = res_s
                    break
            if not matched_svc and current_services:
                matched_svc = current_services[-1]
            if not matched_svc:
                matched_svc = "Clipping path"
            parsed.append({
                "service": matched_svc,
                "category": cat_label,
                "numeric_cat": cat_val
            })
            current_services = []
        else:
            if not re.search(r"qc done|approved|pending|\bby\b", line, re.I) and not re.match(r"^[a-z0-9_]+$", line.strip()):
                current_services.append(line)
    
    deduped = []
    seen = {}
    for p in parsed:
        k = p["service"].lower()
        if k in seen:
            if p["numeric_cat"] > seen[k]["numeric_cat"]:
                seen[k]["category"] = p["category"]
                seen[k]["numeric_cat"] = p["numeric_cat"]
            continue
        seen[k] = p
        deduped.append(p)
    return deduped

_TISTA_SERVICE_CAT_MAP = None

def get_tista_service_categories_map(session=None):
    """
    Loads and caches the Tista service categories map from https://tista.app/service/short.json.
    Maps serviceCategoryId (e.g. 29) to exact service title and category label.
    """
    global _TISTA_SERVICE_CAT_MAP
    if _TISTA_SERVICE_CAT_MAP is not None:
        return _TISTA_SERVICE_CAT_MAP

    s = session or get_tista_http_session()
    if not s:
        return {}

    try:
        r = s.get(f"{BASE_URL}/service/short.json", timeout=8)
        if r.status_code == 200:
            mapping = {}
            for sv in r.json():
                sv_title = sv.get("title", "").strip()
                for cat in sv.get("serviceCategories", []):
                    cid = cat.get("id")
                    if cid is None:
                        continue
                    lbl = str(cat.get("categoryLabel", "1")).strip()
                    title_friendly = cat.get("titleFriendly") or sv_title
                    mapping[cid] = {
                        "service": title_friendly,
                        "parent_service": sv_title,
                        "category_title": f"Category {lbl}",
                        "complexity": f"C{lbl}",
                        "numeric_cat": float(re.sub(r"[^\d.]", "", lbl) or 1.0)
                    }
            _TISTA_SERVICE_CAT_MAP = mapping
            return _TISTA_SERVICE_CAT_MAP
    except Exception as e:
        print(f"[!] Error loading Tista service categories map: {e}")
    return {}

def fetch_order_files_direct_api(order_id: str, session=None):
    """
    Fetches order files, services, and categories directly using Tista's fast JSON REST API.
    Bypasses Playwright browser launch overhead (runs in ~0.2 - 0.4s).
    Saves cache to tista_correction_{oid}.json and returns parsed files list.
    Returns [] when Tista definitively has no such order, and None on a network/server error
    (only the latter is worth retrying with the browser).
    """
    raw_id = str(order_id).strip()
    digits = re.sub(r"\D", "", raw_id)
    oid = digits or raw_id.replace("#", "")
    if not oid:
        return []

    # Check local cache first (only return if non-empty files list)
    cached_corr = DATA_DIR / f"tista_correction_{oid}.json"
    if not cached_corr.exists():
        cached_corr = APP_DIR / f"tista_correction_{oid}.json"
    if cached_corr.exists():
        try:
            c_files = json.loads(cached_corr.read_text(encoding="utf-8")).get("files", [])
            if c_files and len(c_files) > 0:
                return c_files
        except Exception:
            pass

    s = session or get_tista_http_session()
    if not s:
        return None

    cat_map = get_tista_service_categories_map(s)

    try:
        # Step 1: Query order details via orders.json
        r_order = s.get(f"{BASE_URL}/orders.json?q[search_by_query]={oid}&page=1&per=10", timeout=10)
        if r_order.status_code != 200:
            return None

        o_data = r_order.json()
        orders_list = o_data.get("orders", [])
        matched_order = None
        for o in orders_list:
            clean_name = re.sub(r"\D", "", str(o.get("orderName", "")))
            if clean_name == oid or str(o.get("id")) == oid:
                matched_order = o
                break
        if not matched_order and orders_list:
            matched_order = orders_list[0]

        if not matched_order:
            print(f"[*] [Fast API] Order #{oid} not found on Tista")
            return []

        internal_id = str(matched_order.get("id", ""))
        turnaround_hours = matched_order.get("turnaroundHours") or 24
        turnaround = f"{int(turnaround_hours)}:00:00"

        # Map order assignments to locations / contractors
        assignments = matched_order.get("orderAssignments", [])
        assign_loc_map = {}
        contractor = ""
        for a in assignments:
            loc = a.get("locationTitle", "").strip()
            if loc:
                assign_loc_map[a.get("id")] = loc
                if not contractor:
                    contractor = loc
        if not contractor:
            contractor = "Edgebo"

        # Step 2: Query order files via files.json
        r_files = s.get(f"{BASE_URL}/orders/{internal_id}/files.json", timeout=12)
        if r_files.status_code != 200:
            return None

        raw_files = r_files.json()
        if not isinstance(raw_files, list):
            return None

        parsed_files = []
        for idx, f in enumerate(raw_files):
            item_name = str(f.get("itemName", "")).strip()
            item_format = str(f.get("itemFormat", "")).strip()
            subpath = str(f.get("subpath") or "").strip()
            if subpath and subpath != "None":
                filename = f"{subpath}/{item_name}.{item_format}" if item_format and not item_name.endswith(f".{item_format}") else f"{subpath}/{item_name}"
            else:
                filename = f"{item_name}.{item_format}" if item_format and not item_name.endswith(f".{item_format}") else item_name

            loc_id = f.get("orderAssignmentId")
            f_loc = assign_loc_map.get(loc_id, contractor)

            dim_px = f.get("dimensionPx") or 0
            dim_label = ">5000px" if dim_px >= 5000 else "<5000px"

            svcs = []
            seen_tasks = set()
            for t in f.get("itemTasks", []):
                sc_id = t.get("serviceCategoryId")
                if sc_id in seen_tasks:
                    continue
                seen_tasks.add(sc_id)

                info = cat_map.get(sc_id, {})
                s_name = info.get("service") or "Clipping path"
                c_name = info.get("category_title") or "Category 1"
                comp = info.get("complexity") or "C1"
                num_c = info.get("numeric_cat") or 1.0

                svcs.append({
                    "service": s_name,
                    "raw_name": s_name,
                    "complexity": comp,
                    "category": c_name,
                    "numeric_cat": num_c
                })

            if not svcs:
                svcs.append({
                    "service": "Clipping path",
                    "raw_name": "Clipping path",
                    "complexity": "C1",
                    "category": "Category 1",
                    "numeric_cat": 1.0
                })

            disp = ", ".join([f"{s['service']} ({s['category']})" for s in svcs])
            parsed_files.append({
                "id": idx + 1,
                "filename": filename,
                "location": f_loc,
                "dimension": dim_label,
                "services": svcs,
                "services_display": disp
            })

        corr_data = {
            "order_id": oid,
            "contractor": contractor,
            "order_duration": turnaround,
            "pixel": "> 5000",
            "total_files": len(parsed_files),
            "files": parsed_files
        }
        for path_cand in [DATA_DIR, APP_DIR]:
            try:
                (path_cand / f"tista_correction_{oid}.json").write_text(
                    json.dumps(corr_data, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            except Exception:
                pass

        print(f"[✓] [Fast API] Extracted & cached {len(parsed_files)} files for Order #{oid}")
        return parsed_files
    except Exception as e:
        print(f"[!] [Fast API] Error for Order #{oid}: {e}")
        return None

def fetch_orders_files_direct_api_batch(order_ids: list, max_workers: int = 8):
    """
    Fetches files for multiple order IDs in parallel using fast Tista JSON API and ThreadPoolExecutor.
    Returns a list of order IDs that failed (if any).
    """
    if not order_ids:
        return []

    clean_oids = [re.sub(r"\D", "", str(o)) for o in order_ids]
    clean_oids = [o for o in clean_oids if o]
    if not clean_oids:
        return []

    # Check which are already cached
    to_fetch = []
    for o in clean_oids:
        c1 = DATA_DIR / f"tista_correction_{o}.json"
        c2 = APP_DIR / f"tista_correction_{o}.json"
        if not c1.exists() and not c2.exists():
            if o not in to_fetch:
                to_fetch.append(o)

    if not to_fetch:
        return []

    s = get_tista_http_session()
    if not s:
        return to_fetch

    # Pre-load category map once
    get_tista_service_categories_map(s)

    workers = min(max_workers, len(to_fetch))
    failed = []

    def _worker(o):
        try:
            res = fetch_order_files_direct_api(o, session=s)
            if res is None:  # network/server error -> browser fallback
                return o
            return None
        except Exception:
            return o

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for bad_oid in executor.map(_worker, to_fetch):
            if bad_oid:
                failed.append(bad_oid)

    if failed and os.environ.get("TISTA_EMAIL") and os.environ.get("TISTA_PASSWORD"):
        print("[*] Direct API encountered unauthenticated or failed orders, attempting direct HTTP re-login...")
        if login_tista_direct_http(os.environ.get("TISTA_EMAIL"), os.environ.get("TISTA_PASSWORD")):
            s_new = get_tista_http_session(force_refresh=True)
            retry_failed = []
            for o in failed:
                res = fetch_order_files_direct_api(o, session=s_new)
                if res is None:
                    retry_failed.append(o)
            failed = retry_failed

    try:
        import gc
        gc.collect()
    except Exception:
        pass

    return failed

_PRICES_CACHE = None

def normalize_service_name_py(s: str) -> str:
    if not s: return ""
    low = str(s).strip().lower()
    if "multi" in low and "clip" in low: return "Multi-clipping path"
    if "dust" in low or "scratch" in low or "spot" in low: return "Dust, spot and scratch removal"
    if "ghost" in low or "mannequin" in low or "neck" in low: return "Ghost mannequin"
    if "mask" in low: return "Image masking"
    if "shadow" in low: return "Natural shadow"
    if "clipping" in low or "cutout" in low: return "Clipping path"
    return str(s).strip()

def normalize_category_name_py(c: str, complexity: str = "") -> str:
    if complexity:
        comp = re.sub(r"^[Cc]", "", str(complexity)).strip()
        if comp: return f"Category {comp}"
    if not c: return "Category 1"
    m = re.search(r"Category\s*([0-9.]+)", str(c), re.I)
    if m: return f"Category {m.group(1)}"
    m2 = re.search(r"^[Cc]([0-9.]+)$", str(c).strip())
    if m2: return f"Category {m2.group(1)}"
    return str(c).strip()

def normalize_pixel_name_py(p: str) -> str:
    if not p: return "> 5000"
    clean = re.sub(r"px", "", str(p), flags=re.I).replace(" ", "").strip().lower()
    if ">5000" in clean or clean == ">5000": return "> 5000"
    if "<1000" in clean or clean == "<1000": return "< 1000"
    standard = ["< 1000", "1001-2000", "2001-3000", "3001-4000", "4001-5000", "> 5000"]
    for sp in standard:
        if sp.replace(" ", "") == clean:
            return sp
    return "> 5000"

def get_base_price_map():
    global _PRICES_CACHE
    if _PRICES_CACHE is not None:
        return _PRICES_CACHE
    _PRICES_CACHE = {}
    for cand in [BUNDLE_DIR / "Chargeback Calculator.html", APP_DIR / "Chargeback Calculator.html", Path(__file__).resolve().parent / "Chargeback Calculator.html"]:
        if cand.exists():
            try:
                html = cand.read_text(encoding="utf-8")
                m = re.search(r'const DEFAULT_CFG = (\{.*?\});', html)
                if m:
                    cfg = json.loads(m.group(1))
                    for r in cfg.get("prices", []):
                        sn = normalize_service_name_py(r[0])
                        cn = normalize_category_name_py(r[1])
                        pn = normalize_pixel_name_py(r[2])
                        _PRICES_CACHE[(sn, cn, pn)] = float(r[3])
                    break
            except Exception:
                pass
    return _PRICES_CACHE

def calculate_order_value_from_files_py(files: list) -> float:
    if not files: return 0.0
    pmap = get_base_price_map()
    tot = 0.0
    for f in files:
        if not isinstance(f, dict): continue
        dim = normalize_pixel_name_py(f.get("dimension") or "> 5000")
        f_svcs = f.get("services") or [{"service": f.get("service", "Clipping path"), "category": f.get("category", "Category 1")}]
        for s in f_svcs:
            if not isinstance(s, dict): continue
            s_name = normalize_service_name_py(s.get("service") or "")
            c_name = normalize_category_name_py(s.get("category") or "", s.get("complexity") or "")
            prc = pmap.get((s_name, c_name, dim), 0.0)
            tot += prc
    return round(tot, 2)

def fetch_order_files_from_tista(order_id: str, headless: bool = True):
    """
    Fetches the list of files, services, categories, and location for an order to calculate Correction chargebacks.
    Supports Order ID, Dispute ID, and direct URLs (Tista, Dropbox, etc.).
    Returns instantly from local cache if available.
    """
    raw_id = str(order_id).strip()
    digits = re.sub(r"\D", "", raw_id)

    # Check if a URL was passed and extract the clean Order/Dispute ID
    if "http://" in raw_id or "https://" in raw_id:
        if "dropbox.com" in raw_id or "feedback" in raw_id.lower():
            m_hash = re.search(r"(?:%23|#)(\d{5,7})", raw_id)
            if m_hash: digits = m_hash.group(1)
            else:
                m_six = re.search(r"\b(\d{6})\b", raw_id)
                if m_six: digits = m_six.group(1)
        elif "tista.app" in raw_id:
            m_q = re.search(r"[?&]query=([^&#]+)", raw_id)
            if m_q: digits = re.sub(r"\D", "", m_q.group(1))
            elif "order_name_eq" in raw_id:
                m_eq = re.search(r"order_name_eq\]=([^&#]+)", raw_id)
                if m_eq: digits = re.sub(r"\D", "", m_eq.group(1))
            elif "/order_disputes/" in raw_id:
                m_disp = re.search(r"/order_disputes/(\d+)", raw_id)
                if m_disp: digits = m_disp.group(1)
            elif "/orders/" in raw_id:
                m_ord = re.search(r"/orders/(\d+)", raw_id)
                if m_ord: digits = m_ord.group(1)

    search_with_hash = f"#{digits}" if digits else raw_id
    oid = digits or raw_id.replace("#", "")

    if not oid:
        return {"error": "Invalid Order ID"}

    # 1. Local Cache Check (Instant return in ~0.005s)
    cached_corr = DATA_DIR / f"tista_correction_{oid}.json"
    if not cached_corr.exists(): cached_corr = APP_DIR / f"tista_correction_{oid}.json"
    if cached_corr.exists():
        try:
            cdata = json.loads(cached_corr.read_text(encoding="utf-8"))
            if cdata and (cdata.get("files") or cdata.get("total_files", 0) >= 0):
                print(f"[✓] Returning cached files data for Order #{oid}")
                if cdata.get("files") and "order_value" not in cdata:
                    cdata["order_value"] = calculate_order_value_from_files_py(cdata["files"])
                return cdata
        except Exception:
            pass

    # 2. Fast Direct JSON API Check (~0.2s, no Playwright browser overhead)
    fast_files = fetch_order_files_direct_api(oid)
    if fast_files is not None:  # a definite answer from Tista (None = network/server error)
        cached_corr = DATA_DIR / f"tista_correction_{oid}.json"
        if not cached_corr.exists(): cached_corr = APP_DIR / f"tista_correction_{oid}.json"
        if cached_corr.exists():
            try:
                cdata = json.loads(cached_corr.read_text(encoding="utf-8"))
                if cdata and (cdata.get("files") or cdata.get("total_files", 0) >= 0):
                    if cdata.get("files") and "order_value" not in cdata:
                        cdata["order_value"] = calculate_order_value_from_files_py(cdata["files"])
                    return cdata
            except Exception:
                pass
        if not fast_files:
            # Tista's order search found no such order: the browser would search the same data.
            return {"order_id": oid, "contractor": "", "pixel": "> 5000", "total_files": 0, "files": [],
                    "warnings": [f"Order #{oid} was not found on Tista"]}

    result = {
        "order_id": oid,
        "contractor": "",
        "pixel": "> 5000",
        "total_files": 0,
        "files": [],
        "warnings": []
    }

    profile_dir = USER_DATA_DIR

    if not sync_playwright:
        return {
            "status": "error",
            "error": "Playwright is not installed in the currently active Python environment. Please run: pip install playwright && playwright install chromium"
        }

    with sync_playwright() as p:
        ctx = launch_playwright_context(p, headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        ensure_logged_in(page)

        # 0. If 5 digits or not found, check if it's a Dispute ID first
        if len(digits) == 5:
            disp_url = f"{DISPUTES_URL}?q[id_or_fulfillment_order_order_name_eq]={digits}"
            page.goto(disp_url)
            page.wait_for_load_state("domcontentloaded")
            time.sleep(1.5)
            r = page.locator("table tbody tr").first
            if r.count() > 0 and "no data" not in r.inner_text().lower():
                heads = [h.strip().lower() for h in page.locator("table thead th").all_inner_texts()]
                cells = [c.strip() for c in r.locator("td").all_inner_texts()]
                for idx, h in enumerate(heads):
                    if "order" in h and idx < len(cells):
                        m_real = re.search(r"\d+", cells[idx])
                        if m_real:
                            print(f"[✓] Resolved Dispute ID {digits} -> Order #{m_real[0]}")
                            oid = m_real[0]
                            result["order_id"] = oid
                        break

        # 1. Search order to get internal id
        print(f"[*] Locating Order #{oid} in orders management...")
        page.goto(f"{BASE_URL}/orders?location=all&dateRange=any&query={oid}")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)

        order_row = page.locator(f'tr:has-text("{oid}")').first
        internal_id = ""
        contractor = ""
        if order_row.count() > 0:
            row_id = order_row.get_attribute("id") or ""
            m = re.search(r"\d+", row_id)
            if m:
                internal_id = m[0]
            row_text = order_row.inner_text()
            for w in [c.strip() for c in ["Creative Path (P1)", "Pixel Path (P2)", "Perfect Pixel (GB2)", "Path", "Grasigner Kitchen", "Khandoker Graphics", "Graphics Picture", "Vectorio", "Cutout Edge", "Edgebo", "Healing Image", "Photo Fix Zone", "ClippingPix"]]:
                if w.lower() in row_text.lower():
                    contractor = w
                    break

        if not internal_id:
            # Fallback: check if dispute table has the real order ID
            disp_url = f"{DISPUTES_URL}?q[id_or_fulfillment_order_order_name_eq]={digits}"
            page.goto(disp_url)
            page.wait_for_load_state("domcontentloaded")
            time.sleep(1.5)
            r = page.locator("table tbody tr").first
            if r.count() > 0 and "no data" not in r.inner_text().lower():
                heads = [h.strip().lower() for h in page.locator("table thead th").all_inner_texts()]
                cells = [c.strip() for c in r.locator("td").all_inner_texts()]
                for idx, h in enumerate(heads):
                    if "order" in h and idx < len(cells):
                        m_real = re.search(r"\d+", cells[idx])
                        if m_real and m_real[0] != oid:
                            oid = m_real[0]
                            result["order_id"] = oid
                            page.goto(f"{BASE_URL}/orders?location=all&dateRange=any&query={oid}")
                            page.wait_for_load_state("domcontentloaded")
                            time.sleep(2)
                            order_row = page.locator(f'tr:has-text("{oid}")').first
                            if order_row.count() > 0:
                                row_id = order_row.get_attribute("id") or ""
                                m = re.search(r"\d+", row_id)
                                if m: internal_id = m[0]

        if not internal_id:
            result["warnings"].append(f"Order #{oid} not found in orders list.")
            ctx.close()
            return result

        result["contractor"] = contractor

        # 2. Go to files page and switch to table view
        print(f"[*] Opening Order Files table for #{oid}...")
        page.goto(f"{BASE_URL}/orders/{internal_id}/files")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)

        tbl_btn = page.locator('button[data-order-files-layout-param="table"]').first
        if tbl_btn.count() > 0:
            tbl_btn.click()
            time.sleep(1)

        # 3. Extract table rows
        files_raw = page.evaluate("""() => {
            const rows = document.querySelectorAll('table.files-container-table tbody tr');
            return [...rows].map((r, idx) => {
                const cells = [...r.querySelectorAll('td')];
                if (cells.length < 5) return null;
                
                const svcs = [];
                const svcBlocks = cells[3].querySelectorAll('.services-block .service');
                svcBlocks.forEach(sb => {
                    const mainTitle = sb.querySelector('.service-title') ? sb.querySelector('.service-title').innerText.trim() : '';
                    const cats = sb.querySelectorAll('.service-category');
                    if (cats.length > 0) {
                        cats.forEach(c => {
                            const subTitleEl = c.querySelector('.service-category-title');
                            const subTitle = subTitleEl ? subTitleEl.innerText.trim() : '';
                            const compEl = c.querySelector('.complexity');
                            const comp = compEl ? compEl.innerText.trim() : '';
                            svcs.push({
                                group: mainTitle,
                                name: subTitle || mainTitle,
                                complexity: comp,
                                category: comp ? 'Category ' + comp.replace(/^[Cc]/, '').trim() : ''
                            });
                        });
                    } else if (mainTitle) {
                        svcs.push({
                            group: mainTitle,
                            name: mainTitle,
                            complexity: '',
                            category: ''
                        });
                    }
                });

                return {
                    id: idx + 1,
                    filename: cells[2] ? cells[2].innerText.trim() : '',
                    services: svcs,
                    services_raw: cells[3] ? cells[3].innerText.trim() : '',
                    location: cells[4] ? cells[4].innerText.trim() : '',
                    dimension: cells[5] ? cells[5].innerText.trim() : '',
                    date: cells[6] ? cells[6].innerText.trim() : '',
                    created_by: cells[7] ? cells[7].innerText.trim() : ''
                };
            }).filter(Boolean);
        }""")

        files_list = []
        for f in files_raw:
            clean_svcs = deduplicate_file_services(f.get("services") or [])
            if not clean_svcs:
                clean_svcs = parse_file_services(f.get("services_raw", ""))

            files_list.append({
                "id": f["id"],
                "filename": f["filename"],
                "location": f["location"] or contractor,
                "dimension": f["dimension"],
                "services": clean_svcs,
                "services_display": ", ".join(f"{s['service']} ({s['category']})" for s in clean_svcs) or f["services_raw"].replace("\n", " ")
            })
            if not result["contractor"] and f["location"]:
                result["contractor"] = f["location"]

        result["total_files"] = len(files_list)
        result["files"] = files_list
        ctx.close()

    out_file = DATA_DIR / f"tista_correction_{oid}.json"
    out_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[✓] Saved {len(files_list)} files to: {out_file}")
    return result

def format_tista_date_range(date_str=None, start_date=None, end_date=None):
    if not date_str and not start_date and not end_date:
        return ""

    def to_ddmmyy(s):
        if not s: return ""
        s = s.strip()
        m1 = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
        if m1:
            y, m, d = m1.groups()
            return f"{int(d):02d}/{int(m):02d}/{y[-2:]}"
        m2 = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", s)
        if m2:
            d, m, y = m2.groups()
            return f"{int(d):02d}/{int(m):02d}/{y[-2:]}"
        return s

    if start_date or end_date:
        s = to_ddmmyy(start_date)
        e = to_ddmmyy(end_date)
        if s and e: return f"{s} - {e}"
        return s or e or ""

    if date_str:
        m_dash = re.findall(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", date_str)
        if len(m_dash) >= 2:
            y1, m1, d1 = m_dash[0]
            y2, m2, d2 = m_dash[1]
            return f"{int(d1):02d}/{int(m1):02d}/{y1[-2:]} - {int(d2):02d}/{int(m2):02d}/{y2[-2:]}"
        m_slash = re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", date_str)
        if len(m_slash) >= 2:
            d1, m1, y1 = m_slash[0]
            d2, m2, y2 = m_slash[1]
            return f"{int(d1):02d}/{int(m1):02d}/{y1[-2:]} - {int(d2):02d}/{int(m2):02d}/{y2[-2:]}"
        return date_str.strip()
    return ""

def extract_order_files_with_page(page, order_id: str):
    """
    Extracts order files, services, and categories directly using an already active Playwright page.
    Saves cache to tista_correction_{oid}.json and returns the parsed files list.
    """
    raw_id = str(order_id).strip()
    digits = re.sub(r"\D", "", raw_id)
    oid = digits or raw_id.replace("#", "")
    if not oid:
        return []

    cached_corr = DATA_DIR / f"tista_correction_{oid}.json"
    if not cached_corr.exists():
        cached_corr = APP_DIR / f"tista_correction_{oid}.json"
    if cached_corr.exists():
        try:
            return json.loads(cached_corr.read_text(encoding="utf-8")).get("files", [])
        except Exception:
            pass

    # Try Fast Direct JSON API first (~0.2s without browser navigation)
    fast_files = fetch_order_files_direct_api(oid)
    if fast_files:
        return fast_files

    print(f"[*] Extracting files for Order #{oid} using active page...")
    try:
        page.goto(f"{BASE_URL}/orders?location=all&dateRange=any&query={oid}", wait_until="domcontentloaded", timeout=12000)
        try:
            page.locator(f'tr:has-text("{oid}")').first.wait_for(state="attached", timeout=3000)
        except Exception:
            pass
        order_row = page.locator(f'tr:has-text("{oid}")').first
        internal_id = ""
        contractor = ""
        turnaround = "24:00:00"
        if order_row.count() > 0:
            row_id = order_row.get_attribute("id") or ""
            m = re.search(r"\d+", row_id)
            if m: internal_id = m[0]
            row_text = order_row.inner_text()
            t_match = re.search(r"\b(\d+)\s*h\b", row_text)
            if t_match:
                turnaround = f"{int(t_match[1])}:00:00"
            for w in [c.strip() for c in ["Creative Path (P1)", "Pixel Path (P2)", "Perfect Pixel (GB2)", "Path", "Grasigner Kitchen", "Khandoker Graphics", "Graphics Picture", "Vectorio", "Cutout Edge", "Edgebo", "Healing Image", "Photo Fix Zone", "ClippingPix"]]:
                if w.lower() in row_text.lower():
                    contractor = w
                    break

        if not internal_id:
            corr_empty = {
                "order_id": oid,
                "contractor": contractor,
                "order_duration": turnaround,
                "pixel": "> 5000",
                "total_files": 0,
                "files": []
            }
            for path_cand in [DATA_DIR, APP_DIR]:
                try:
                    (path_cand / f"tista_correction_{oid}.json").write_text(json.dumps(corr_empty, indent=2, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
            return []

        page.goto(f"{BASE_URL}/orders/{internal_id}/files", wait_until="domcontentloaded", timeout=12000)
        tbl_btn = page.locator('button[data-order-files-layout-param="table"]').first
        try:
            tbl_btn.wait_for(state="visible", timeout=2500)
            tbl_btn.click()
        except Exception:
            pass
        try:
            page.locator('table.files-container-table tbody tr').first.wait_for(state="attached", timeout=3000)
        except Exception:
            pass

        files_raw = page.evaluate("""() => {
            const rows = document.querySelectorAll('table.files-container-table tbody tr');
            return [...rows].map((r, idx) => {
                const cells = [...r.querySelectorAll('td')];
                if (cells.length < 5) return null;
                const svcs = [];
                const svcBlocks = cells[3].querySelectorAll('.services-block .service');
                svcBlocks.forEach(sb => {
                    const mainTitle = sb.querySelector('.service-title') ? sb.querySelector('.service-title').innerText.trim() : '';
                    const cats = sb.querySelectorAll('.service-category');
                    if (cats.length > 0) {
                        cats.forEach(c => {
                            const subTitleEl = c.querySelector('.service-category-title');
                            const subTitle = subTitleEl ? subTitleEl.innerText.trim() : '';
                            const compEl = c.querySelector('.complexity');
                            const comp = compEl ? compEl.innerText.trim() : '';
                            svcs.push({
                                group: mainTitle,
                                name: subTitle || mainTitle,
                                complexity: comp,
                                category: comp ? 'Category ' + comp.replace(/^[Cc]/, '').trim() : ''
                            });
                        });
                    } else if (mainTitle) {
                        svcs.push({
                            group: mainTitle,
                            name: mainTitle,
                            complexity: '',
                            category: ''
                        });
                    }
                });
                return {
                    id: idx + 1,
                    filename: cells[2] ? cells[2].innerText.trim() : '',
                    services: svcs,
                    services_raw: cells[3] ? cells[3].innerText.trim() : '',
                    location: cells[4] ? cells[4].innerText.trim() : '',
                    dimension: cells[5] ? cells[5].innerText.trim() : '',
                    date: cells[6] ? cells[6].innerText.trim() : '',
                    created_by: cells[7] ? cells[7].innerText.trim() : ''
                };
            }).filter(Boolean);
        }""")

        files_list = []
        for f in files_raw:
            clean_svcs = deduplicate_file_services(f.get("services") or [])
            svcs_display = ", ".join([f"{s['service']} ({s['category']})" for s in clean_svcs]) if clean_svcs else "Clipping path (Category 1)"
            files_list.append({
                "id": f.get("id"),
                "filename": f.get("filename"),
                "location": f.get("location") or contractor,
                "dimension": f.get("dimension") or ">5000px",
                "services": clean_svcs,
                "services_display": svcs_display
            })

        corr_res = {
            "order_id": oid,
            "contractor": contractor,
            "order_duration": turnaround,
            "pixel": "> 5000",
            "total_files": len(files_list),
            "files": files_list
        }
        for path_cand in [DATA_DIR, APP_DIR]:
            try:
                (path_cand / f"tista_correction_{oid}.json").write_text(json.dumps(corr_res, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        print(f"[✓] Extracted & cached {len(files_list)} files for Order #{oid}")
        return files_list
    except Exception as e:
        print(f"[!] Note on extract_order_files_with_page for #{oid}: {e}")
        return []

MAX_DISPUTE_PAGES = 200

def _page_url(url: str, page_no: int) -> str:
    return f"{url}{'&' if '?' in url else '?'}page={page_no}"

def fetch_remaining_dispute_pages(http_sess, url: str, first_html: str, parse_rows) -> list:
    """
    Returns dispute rows from pages 2..N of a Tista disputes list.
    Uses the "Displaying disputes 1 - 25 of 97 in total" banner to fetch all pages in parallel,
    falling back to following rel="next" links one page at a time.
    """
    text = re.sub(r"<[^>]+>", "", first_html).replace("&nbsp;", " ")
    m = re.search(r"Displaying\s+\w+\s+(\d+)\s*-\s*(\d+)\s+of\s+(\d+)\s+in total", text)
    extra = []
    if m:
        first, last, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
        per_page = max(1, last - first + 1)
        n_pages = min(MAX_DISPUTE_PAGES, -(-total // per_page))
        if n_pages <= 1:
            return []

        def _get(page_no):
            try:
                r = http_sess.get(_page_url(url, page_no), timeout=15)
                if r.status_code == 200 and "sign_in" not in r.url:
                    return parse_rows(r.text)
            except Exception as e:
                print(f"[!] Dispute page {page_no} note: {e}")
            return []

        with ThreadPoolExecutor(max_workers=min(8, n_pages - 1)) as pool:
            for page_rows in pool.map(_get, range(2, n_pages + 1)):
                extra.extend(page_rows)
        if len(extra) + per_page < total:
            print(f"[!] Expected {total} disputes but fetched {len(extra) + per_page}")
        return extra

    # No banner: follow rel="next" sequentially.
    html_text = first_html
    page_no = 1
    while re.search(r'rel=["\']next["\']', html_text) and page_no < MAX_DISPUTE_PAGES:
        page_no += 1
        try:
            r = http_sess.get(_page_url(url, page_no), timeout=15)
        except Exception as e:
            print(f"[!] Dispute page {page_no} note: {e}")
            break
        if r.status_code != 200 or "sign_in" in r.url:
            break
        page_rows = parse_rows(r.text)
        if not page_rows:
            break
        extra.extend(page_rows)
        html_text = r.text
    return extra

def fetch_disputes_batch_from_tista(filters: dict = None, headless: bool = False):
    """
    Fetches list of disputes based on filters (Dispute reason, Status, Chargeable to, Location, Order ID),
    and optionally deep-scans order files to extract service and category.
    """
    filters = filters or {}
    reason = str(filters.get("reason", "2") or "").strip()

    # If multiple reasons requested (e.g. "2,4" for Correction & Quality), fetch and combine
    if reason and "," in reason:
        sub_reasons = [r.strip() for r in reason.split(",") if r.strip()]
        all_disputes = []
        seen_ids = set()

        def _fetch_sub(sr):
            sub_filters = dict(filters)
            sub_filters["reason"] = sr
            return fetch_disputes_batch_from_tista(sub_filters, headless=headless)

        # Fetch each reason concurrently; results are merged in the original reason order.
        with ThreadPoolExecutor(max_workers=len(sub_reasons)) as pool:
            sub_results = list(pool.map(_fetch_sub, sub_reasons))

        for res in sub_results:
            for d in res.get("disputes", []):
                uid = str(d.get("dispute_id") or d.get("order_id"))
                if uid not in seen_ids:
                    seen_ids.add(uid)
                    all_disputes.append(d)
        # A pasted ID counts as not found only if no reason's search found it.
        not_found = set.intersection(*[set(r.get("not_found_ids") or []) for r in sub_results]) if sub_results else set()
        return {
            "disputes": all_disputes,
            "total_disputes": len(all_disputes),
            "filters": filters,
            "not_found_ids": sorted(not_found)
        }

    status = str(filters.get("status", "1") or "")
    chargeable = str(filters.get("chargeable", "1") or "")
    location = str(filters.get("location", "") or "")
    query = str(filters.get("query") or "").strip()
    deep_scan = bool(filters.get("deep_scan", True))

    params = []
    if reason: params.append(f"q[dispute_reason_eq]={reason}")
    if status: params.append(f"q[status_eq]={status}")
    if chargeable: params.append(f"q[chargeable_to_eq]={chargeable}")
    loc_clean = location.strip().lower()
    loc_id = LOCATION_ID_MAP.get(loc_clean, location.strip())
    if loc_id: params.append(f"q[location_id_eq]={loc_id}")

    date_str = str(filters.get("date_range") or filters.get("fault_at") or filters.get("fault_at_between") or "").strip()
    start_date = str(filters.get("start_date") or "").strip()
    end_date = str(filters.get("end_date") or "").strip()
    formatted_date_range = format_tista_date_range(date_str, start_date, end_date)
    if formatted_date_range:
        params.append(f"q[fault_at_between]={quote(formatted_date_range)}")
        print(f"[*] Filtering by Fault at date range: '{formatted_date_range}'")

    multiple_ids = []
    single_clean_id = ""
    single_is_hash = False
    if query:
        raw_parts = [p.strip() for p in re.split(r"[,;\n\s]+", query) if p.strip()]
        if len(raw_parts) == 1:
            q0 = raw_parts[0]
            d0 = re.sub(r"\D", "", q0)
            single_clean_id = d0
            if q0.startswith("#") or len(d0) == 6:
                single_is_hash = True
                params.append(f"q[id_or_fulfillment_order_order_name_eq]=%23{d0}")
            else:
                params.append(f"q[id_or_fulfillment_order_order_name_eq]={d0}")
        elif len(raw_parts) > 1:
            multiple_ids = [re.sub(r"\D", "", p) for p in raw_parts if re.sub(r"\D", "", p)]

    qs = "&".join(params)
    url = f"{DISPUTES_URL}?{qs}" if qs else DISPUTES_URL
    print(f"\n[*] Searching disputes: {url}")

    t_start = time.time()
    http_sess = get_tista_http_session()
    rows = []
    http_ok = False

    def parse_rows_from_html(html_text):
        trs = re.findall(r'<tr[^>]*>(.*?)</tr>', html_text, re.DOTALL)
        found_rows = []
        for tr in trs:
            tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.DOTALL)
            if len(tds) >= 11:
                cells = [re.sub(r'<[^>]+>', '', td).strip() for td in tds]
                found_rows.append({
                    "dispute_id": cells[0],
                    "order_id": cells[1],
                    "dispute_location": cells[2],
                    "fault_by": cells[3],
                    "dispute_reason": cells[4],
                    "chargeable_to": cells[5],
                    "charge": cells[6],
                    "fault_at": cells[7],
                    "delayed_by": cells[8],
                    "fault_quantity": cells[9],
                    "fault_severity": cells[10],
                    "status": cells[11] if len(cells) > 11 else "Open"
                })
        return found_rows

    not_found_ids = []

    def _lookup_one_id(raw_id):
        """Rows for one pasted order/dispute ID: with the current filters first, then without them
        (same as the single-ID search), so a pasted ID is never silently dropped."""
        base = [p for p in params if not p.startswith("q[id_or_fulfillment_order_order_name_eq]=")]
        first = f"%23{raw_id}" if len(raw_id) == 6 else raw_id
        attempts = [f"{DISPUTES_URL}?{'&'.join(base + [f'q[id_or_fulfillment_order_order_name_eq]={first}'])}"]
        attempts += [f"{DISPUTES_URL}?q[id_or_fulfillment_order_order_name_eq]={c}" for c in dict.fromkeys([first, f"%23{raw_id}", raw_id])]
        for a_url in attempts:
            r_id = http_sess.get(a_url, timeout=15)
            if r_id.status_code != 200 or "sign_in" in r_id.url:
                raise RuntimeError(f"Tista returned {r_id.status_code} for ID {raw_id}")
            found = parse_rows_from_html(r_id.text)
            if found:
                return raw_id, found
        return raw_id, []

    try:
        if multiple_ids:
            # One direct lookup per pasted ID instead of downloading the whole filtered list.
            seen_dids = set()
            with ThreadPoolExecutor(max_workers=min(8, len(multiple_ids))) as pool:
                for raw_id, found in pool.map(_lookup_one_id, list(dict.fromkeys(multiple_ids))):
                    if not found:
                        not_found_ids.append(raw_id)
                    for row in found:
                        if row["dispute_id"] not in seen_dids:
                            seen_dids.add(row["dispute_id"])
                            rows.append(row)
            http_ok = True
            print(f"[✓] Looked up {len(multiple_ids)} pasted IDs in {time.time()-t_start:.2f}s: {len(rows)} disputes"
                  + (f", not found: {not_found_ids}" if not_found_ids else ""))
        else:
            resp = http_sess.get(url, timeout=10)
            if resp.status_code == 200 and "sign_in" not in resp.url and "order_disputes" in resp.url:
                rows = parse_rows_from_html(resp.text)
                http_ok = True
                # Tista paginates at 25 rows; fetch the remaining pages in parallel.
                rows.extend(fetch_remaining_dispute_pages(http_sess, url, resp.text, parse_rows_from_html))
                print(f"[✓] Extracted {len(rows)} dispute records in {time.time()-t_start:.2f}s via fast HTTP session!")
                if single_clean_id and not rows:
                    for cand_val in [single_clean_id if single_is_hash else f"%23{single_clean_id}", f"%23{single_clean_id}", single_clean_id]:
                        alt_url = f"{DISPUTES_URL}?q[id_or_fulfillment_order_order_name_eq]={cand_val}"
                        try:
                            r_alt = http_sess.get(alt_url, timeout=5)
                            if r_alt.status_code == 200:
                                alt_rows = parse_rows_from_html(r_alt.text)
                                if alt_rows:
                                    rows = alt_rows
                                    break
                        except Exception:
                            pass
    except Exception as he:
        print(f"[*] Fast HTTP search note: {he}")

    p_instance = None
    ctx = None
    page = None

    if not http_ok:
        if not sync_playwright:
            return {
                "status": "error",
                "error": "Playwright is not installed in the currently active Python environment. Please run: pip install playwright && playwright install chromium"
            }
        p_instance = sync_playwright().start()
        ctx = launch_playwright_context(p_instance, headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ensure_logged_in(page)
        page.goto(url)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(1.5)

        if single_clean_id:
            trs = page.locator("table tbody tr")
            if trs.count() == 0 or (trs.count() == 1 and "no data" in trs.first.inner_text().lower()):
                alt_val = single_clean_id if single_is_hash else f"%23{single_clean_id}"
                alt_params = [p for p in params if not p.startswith("q[id_or_fulfillment_order_order_name_eq]=")]
                alt_params.append(f"q[id_or_fulfillment_order_order_name_eq]={alt_val}")
                alt_url = f"{DISPUTES_URL}?{'&'.join(alt_params)}"
                page.goto(alt_url)
                page.wait_for_load_state("domcontentloaded")
                time.sleep(1.0)

            trs = page.locator("table tbody tr")
            if trs.count() == 0 or (trs.count() == 1 and "no data" in trs.first.inner_text().lower()):
                for cand_val in [f"%23{single_clean_id}", single_clean_id]:
                    broad_url = f"{DISPUTES_URL}?q[id_or_fulfillment_order_order_name_eq]={cand_val}"
                    page.goto(broad_url)
                    page.wait_for_load_state("domcontentloaded")
                    time.sleep(1.0)
                    trs_broad = page.locator("table tbody tr")
                    if trs_broad.count() > 0 and "no data" not in trs_broad.first.inner_text().lower():
                        break

        rows_js = """() => {
            const table = document.querySelector('table');
            if(!table) return [];
            const trs = [...table.querySelectorAll('tbody tr')];
            return trs.map(tr => {
                const cells = [...tr.querySelectorAll('td')].map(c => c.innerText.trim());
                if(cells.length < 11) return null;
                return {
                    dispute_id: cells[0],
                    order_id: cells[1],
                    dispute_location: cells[2],
                    fault_by: cells[3],
                    dispute_reason: cells[4],
                    chargeable_to: cells[5],
                    charge: cells[6],
                    fault_at: cells[7],
                    delayed_by: cells[8],
                    fault_quantity: cells[9],
                    fault_severity: cells[10],
                    status: cells[11]
                };
            }).filter(Boolean);
        }"""
        rows = page.evaluate(rows_js)
        # Follow pagination (25 rows per page) for list searches.
        if not single_clean_id:
            page_no = 1
            while page_no < MAX_DISPUTE_PAGES and page.locator('a[rel="next"]').count() > 0:
                page_no += 1
                page.goto(_page_url(url, page_no))
                page.wait_for_load_state("domcontentloaded")
                page_rows = page.evaluate(rows_js)
                if not page_rows:
                    break
                rows.extend(page_rows)

    if multiple_ids:
        target_set = set(multiple_ids)
        rows = [r for r in rows if (re.sub(r"\D", "", r.get("order_id", "")) in target_set) or (re.sub(r"\D", "", r.get("dispute_id", "")) in target_set)]

    print(f"[*] Processing {len(rows)} dispute records...")

    def _cache_paths(oid):
        c_corr = DATA_DIR / f"tista_correction_{oid}.json"
        if not c_corr.exists(): c_corr = APP_DIR / f"tista_correction_{oid}.json"
        c_delay = DATA_DIR / f"tista_{oid}.json"
        if not c_delay.exists(): c_delay = APP_DIR / f"tista_{oid}.json"
        return c_corr, c_delay

    def _cache_has(path, key):
        if not path.exists():
            return False
        try:
            return bool(json.loads(path.read_text(encoding="utf-8")).get(key))
        except Exception:
            return False

    # Decide up front (from the cache as it is now) which orders need Tista files and which need
    # their comment link scanned, so the parallel stages below don't race on cache files.
    uncached_oids = []
    needs_link_oids = set()
    for r in rows:
        raw_oid = re.sub(r"\D", "", r.get("order_id", ""))
        if not raw_oid:
            continue
        c_corr, c_delay = _cache_paths(raw_oid)
        if (deep_scan or single_clean_id) and "revision" not in (r.get("dispute_reason") or "").lower():
            if not c_corr.exists() and not c_delay.exists() and raw_oid not in uncached_oids:
                uncached_oids.append(raw_oid)
        # Skip the external link when files are already known from Tista or an earlier link scan.
        if not _cache_has(c_corr, "files") and not _cache_has(c_delay, "filenames"):
            needs_link_oids.add(raw_oid)

    # Stage A (background): order files via Tista's JSON API, running while comments and links load.
    stage_pool = ThreadPoolExecutor(max_workers=1)
    orders_future = None
    if uncached_oids:
        print(f"[*] [Fast API] Parallel extracting {len(uncached_oids)} uncached orders: {uncached_oids}")
        orders_future = stage_pool.submit(fetch_orders_files_direct_api_batch, uncached_oids)

    # Stage B: dispute comments (they carry the file link) via HTTP, 8 at a time - only where needed.
    # The batch table doesn't show comments; the calculator loads one when a row is opened.
    comment_map = {}
    link_map = {}

    def fetch_comments(dids):
        dids = [d for d in dict.fromkeys(dids) if d and d not in comment_map]
        to_read = []
        for did in dids:
            cached = load_cached_comment(did)
            if cached is None:
                to_read.append(did)
            else:
                if cached[0]: comment_map[did] = cached[0]
                if cached[1]: link_map[did] = cached[1]
        if not to_read:
            return
        with ThreadPoolExecutor(max_workers=min(8, len(to_read))) as pool:
            for did, c_text, c_link in pool.map(lambda d: (d, *read_dispute_comment(http_sess, d)), to_read):
                save_cached_comment(did, c_text, c_link)
                if c_text: comment_map[did] = c_text
                if c_link: link_map[did] = c_link

    def _oid(r):
        return re.sub(r"\D", "", r.get("order_id", ""))

    def _is_delay(r):
        return "delay" in (r.get("dispute_reason") or "").lower()

    if single_clean_id:
        fetch_comments([r.get("dispute_id") for r in rows])
    elif deep_scan:
        # Delay rows: the link lists the delayed files, needed unless already known.
        fetch_comments([r.get("dispute_id") for r in rows if _is_delay(r) and _oid(r) in needs_link_oids])

    # Join stage A; any orders the JSON API couldn't resolve fall back to the Tista browser scan.
    if orders_future:
        uncached_oids = orders_future.result()
        if uncached_oids:
            print(f"[*] Falling back to Playwright for {len(uncached_oids)} orders: {uncached_oids}")
    stage_pool.shutdown(wait=False)

    # Quality/Correction rows: the link is only a fallback when Tista has no files for the order.
    if deep_scan and not single_clean_id:
        fetch_comments([r.get("dispute_id") for r in rows
                        if not _is_delay(r) and _oid(r) in needs_link_oids
                        and not _cache_has(_cache_paths(_oid(r))[0], "files")])

    # Stage C: filenames from external links, all at once in one headless browser.
    links_to_extract = []
    for r_check in rows:
        link_c = link_map.get(str(r_check.get("dispute_id") or "").strip())
        if link_c and _oid(r_check) in needs_link_oids:
            links_to_extract.append(link_c)
    link_files_map = extract_files_from_links(links_to_extract) if links_to_extract else {}

    if uncached_oids and not page and sync_playwright:
        try:
            p_instance = sync_playwright().start()
            ctx = launch_playwright_context(p_instance, headless=headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            ensure_logged_in(page)
        except Exception as pe:
            print(f"[!] Playwright start note: {pe}")

    if page and uncached_oids:
        print(f"[*] Deep scanning {len(uncached_oids)} uncached orders: {uncached_oids}")
        for u_oid in uncached_oids:
            try:
                extract_order_files_with_page(page, u_oid)
            except Exception as e_uo:
                print(f"[!] Deep scan note for #{u_oid}: {e_uo}")

    out_list = []

    for r in rows:
        raw_oid = re.sub(r"\D", "", r["order_id"])
        d_id = str(r.get("dispute_id") or "").strip()
        if raw_oid and d_id and d_id.isdigit():
            _ORDER_TO_DISPUTE_CACHE[raw_oid] = d_id
            if r.get("status"):
                _DISPUTE_STATUS_CACHE[d_id] = r.get("status")

        qty = int(re.sub(r"\D", "", r["fault_quantity"]) or 1)
        reason_name = r["dispute_reason"] or "Correction"

        # Skip Revision disputes completely per user rules (no chargeback for revision)
        if "revision" in reason_name.lower():
            print(f"[*] Skipping Revision dispute #{r.get('dispute_id')} (No chargeback for revision)")
            continue

        cached_corr = DATA_DIR / f"tista_correction_{raw_oid}.json"
        if not cached_corr.exists():
            cached_corr = APP_DIR / f"tista_correction_{raw_oid}.json"

        cached_delay = DATA_DIR / f"tista_{raw_oid}.json"
        if not cached_delay.exists():
            cached_delay = APP_DIR / f"tista_{raw_oid}.json"

        disp_comment = ""
        disp_link = ""
        disp_files = []

        # First, check if cached delay already has comment, link, and filenames
        if cached_delay.exists():
            try:
                ddata = json.loads(cached_delay.read_text(encoding="utf-8"))
                disp_comment = ddata.get("comment", "")
                disp_link = ddata.get("comment_link", "")
                disp_files = ddata.get("filenames", [])
            except Exception:
                pass

        # Use pre-fetched comment and link from fast HTTP
        if not disp_comment and d_id in comment_map:
            disp_comment = comment_map[d_id]
        if not disp_link and d_id in link_map:
            disp_link = link_map[d_id]

        # If link exists and files not yet extracted, use the batch link results and cache permanently
        if disp_link and not disp_files:
            disp_files = link_files_map.get(clean_link(disp_link), [])
            if disp_files and raw_oid:
                try:
                    (DATA_DIR / f"tista_{raw_oid}.json").write_text(json.dumps({
                        "order_id": raw_oid,
                        "dispute_id": d_id,
                        "comment": disp_comment,
                        "comment_link": disp_link,
                        "filenames": disp_files,
                        "filenames_count": len(disp_files)
                    }, indent=2, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass

        detected_svc = ""
        detected_cat = ""
        detected_dur = "24:00:00"
        detected_pix = "> 5000"

        order_svcs = []
        order_cats = []
        order_file_details = []

        if cached_corr.exists():
            try:
                cdata = json.loads(cached_corr.read_text(encoding="utf-8"))
                if cdata.get("order_duration"):
                    detected_dur = cdata.get("order_duration")
                raw_files = cdata.get("files", [])

                # For Delay disputes, filter strictly to the delayed files and slice to dispute qty
                target_files = raw_files
                if reason_name.lower() == "delay":
                    if disp_files:
                        disp_set = set(Path(f).name.lower().strip() for f in disp_files)
                        matched = [f for f in raw_files if Path(f.get("filename", "")).name.lower().strip() in disp_set]
                        if matched:
                            target_files = matched
                    if qty and len(target_files) > qty:
                        target_files = target_files[:qty]
                elif qty and len(target_files) > qty:
                    target_files = target_files[:qty]

                for f0 in target_files:
                    fn = f0.get("filename", "")
                    svcs0 = f0.get("services", [])
                    clean_file_services = []
                    seen_cfs = {}
                    for s_item in svcs0:
                        raw_s = s_item.get("raw_name") or s_item.get("name") or s_item.get("service") or ""
                        s_name = resolve_service_name(raw_s)
                        c_name = s_item.get("category")
                        if not c_name and s_item.get("complexity"):
                            c_name = f"Category {re.sub(r'^[Cc]', '', s_item.get('complexity')).strip()}"
                        num_cat = s_item.get("numeric_cat") or 1.0
                        k = s_name.lower()
                        if k in seen_cfs:
                            if num_cat > seen_cfs[k]["numeric_cat"]:
                                seen_cfs[k]["category"] = c_name or "Category 1.5"
                                seen_cfs[k]["numeric_cat"] = num_cat
                                seen_cfs[k]["complexity"] = s_item.get("complexity") or ""
                            continue
                        item = {
                            "service": s_name or "Clipping path",
                            "raw_name": raw_s,
                            "category": c_name or "Category 1.5",
                            "complexity": s_item.get("complexity") or "",
                            "numeric_cat": num_cat
                        }
                        seen_cfs[k] = item
                        clean_file_services.append(item)
                        if s_name and s_name not in order_svcs:
                            order_svcs.append(s_name)
                        if c_name and c_name not in order_cats:
                            order_cats.append(c_name)

                    primary_cat = clean_file_services[0]["category"] if clean_file_services else "Category 1.5"
                    primary_svc = clean_file_services[0]["service"] if clean_file_services else "Clipping path"

                    order_file_details.append({
                        "filename": fn,
                        "category": primary_cat,
                        "service": primary_svc,
                        "services": clean_file_services
                    })

                if order_svcs and not detected_svc:
                    detected_svc = order_svcs[0]
                if order_cats and not detected_cat:
                    detected_cat = order_cats[0]
            except Exception:
                pass
        elif cached_delay.exists():
            try:
                ddata = json.loads(cached_delay.read_text(encoding="utf-8"))
                detected_svc = ddata.get("service_type")
                detected_cat = ddata.get("category_level")
                detected_dur = ddata.get("order_duration_text") or "24:00:00"
                for s0 in ddata.get("services", []):
                    s_name = s0.get("service")
                    c_name = s0.get("category")
                    if s_name and s_name not in order_svcs:
                        order_svcs.append(s_name)
                    if c_name and c_name not in order_cats:
                        order_cats.append(c_name)
            except Exception:
                pass

        if not detected_svc:
            detected_svc = "Clipping path"
        if not detected_cat:
            detected_cat = "Category 1"

        delay_sec = hours_to_seconds(r["delayed_by"]) if reason_name.lower() == "delay" else 0
        dur_sec = parse_duration_to_seconds(detected_dur) or 86400
        delay_pct = (abs(delay_sec) / dur_sec) if dur_sec > 0 else 0

        raw_sev = r.get("fault_severity") or ""
        if reason_name.lower() == "delay" or not raw_sev or raw_sev.lower() in ("not applicable", "n/a", "--", "none"):
            if delay_pct > 0.50:
                fault_sev = "Critical"
            elif delay_pct > 0.30:
                fault_sev = "High"
            elif delay_pct > 0.15:
                fault_sev = "Medium"
            else:
                fault_sev = "Low"
        else:
            fault_sev = raw_sev

        out_list.append({
            "dispute_id": r["dispute_id"],
            "order_id": r["order_id"],
            "contractor": r["dispute_location"],
            "fault_by": r["fault_by"],
            "dispute_reason": reason_name,
            "chargeable_to": r["chargeable_to"],
            "fault_at": r["fault_at"],
            "delayed_by": r["delayed_by"],
            "delay_seconds": delay_sec,
            "order_duration": detected_dur,
            "fault_quantity": qty,
            "fault_severity": fault_sev,
            "status": r["status"],
            "service": detected_svc or "Clipping path",
            "cat": detected_cat or "Category 1",
            "pix": detected_pix,
            "order_services": order_svcs,
            "order_cats": order_cats,
            "comment": disp_comment,
            "comment_link": disp_link,
            "filenames": [f["filename"] for f in order_file_details][:qty] if order_file_details else disp_files[:qty],
            "file_details": order_file_details[:qty],
            "all_order_files": order_file_details,
            "total_order_files": len(order_file_details),
            "filenames_count": len(order_file_details[:qty]) if order_file_details else min(qty, len(disp_files))
        })

    if ctx:
        try: ctx.close()
        except Exception: pass
    if p_instance:
        try: p_instance.stop()
        except Exception: pass

    res = {
        "filters": filters,
        "total_disputes": len(out_list),
        "disputes": out_list,
        "not_found_ids": not_found_ids
    }
    out_file = DATA_DIR / "tista_batch_disputes.json"
    out_file.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[✓] Saved {len(out_list)} disputes to: {out_file}")
    gc.collect()
    return res

def extract_filenames_from_page(page, url):
    """
    Navigates to a file storage or transfer URL (Dropbox, Google Drive, WeTransfer, etc.)
    and extracts all filenames reliably using TreeWalker and attribute search.
    """
    if not url: return []
    clean_url = str(url).strip().rstrip(".,;")
    print(f"[*] Extracting filenames from link: {clean_url}")
    try:
        try:
            page.goto(clean_url, wait_until="domcontentloaded", timeout=12000)
        except Exception:
            try:
                page.goto(clean_url, timeout=12000)
            except Exception:
                pass
        time.sleep(2.0)

        files = page.evaluate("""() => {
            const filenames = new Set();
            const extRegex = /\\.(?:jpg|jpeg|png|psd|tif|tiff|cr2|raw|dng|ai|eps|pdf)$/i;
            const fullRegex = /[\\w\\-. ]+\\.(?:jpg|jpeg|png|psd|tif|tiff|cr2|raw|dng|ai|eps|pdf)/gi;
            const ignoreList = ['dropbox', 'logo', 'favicon', 'icon', 'apple-touch', 'webclip', 'badge', 'banner'];

            function isIgnored(name) {
                const lower = name.toLowerCase();
                return ignoreList.some(ig => lower.includes(ig)) || name.length < 4 || name.length > 200;
            }

            // 1. Text walker on all visible text nodes
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode())) {
                const val = (node.nodeValue || '').trim();
                if (extRegex.test(val) && !isIgnored(val)) {
                    filenames.add(val);
                } else if (val.length < 300) {
                    const matches = val.match(fullRegex);
                    if (matches) {
                        for (const m of matches) {
                            const cleanM = m.trim();
                            if (!isIgnored(cleanM)) filenames.add(cleanM);
                        }
                    }
                }
            }

            // 2. Check title, alt, data-filename, aria-label attributes
            const attrElements = document.querySelectorAll('[title], [alt], [data-filename], [aria-label]');
            for (const el of attrElements) {
                for (const attr of ['title', 'alt', 'data-filename', 'aria-label']) {
                    const v = (el.getAttribute(attr) || '').trim();
                    if (v && extRegex.test(v) && !isIgnored(v)) {
                        filenames.add(v);
                    }
                }
            }

            return [...filenames];
        }""")
        print(f"[✓] Extracted {len(files)} filenames from {clean_url}")
        return files
    except Exception as e:
        print(f"[!] Error extracting filenames from {clean_url}: {e}")
        return []

def extract_filenames_from_link_standalone(url: str, headless: bool = True):
    """Extracts filenames from any storage/transfer link (see link_files.py)."""
    return extract_files_from_links([url]).get(clean_link(url), [])

def fetch_dispute_comment_and_files(dispute_id: str, headless: bool = True, with_files: bool = True):
    """
    Reads the dispute's comment and first storage/transfer link from its Tista edit page over HTTP,
    and (with_files) the filenames behind that link.
    """
    clean_did = str(dispute_id or "").strip().replace("#", "")
    if not clean_did:
        return {"error": "Missing dispute ID"}

    result = {
        "dispute_id": clean_did,
        "comment": "",
        "comment_link": "",
        "filenames": [],
        "count": 0
    }
    s = get_tista_http_session()
    if not s:
        return {**result, "error": "HTTP session not available"}

    comment, link = read_dispute_comment(s, clean_did)
    result["comment"], result["comment_link"] = comment, link
    if link and with_files:
        files = extract_files_from_links([link]).get(clean_link(link), [])
        result["filenames"], result["count"] = files, len(files)
    return result

# --- Ultra-Fast Direct HTTP Tista Session & Cache ---
_TISTA_HTTP_SESSION = None
_CACHED_CSRF_TOKEN = ""
_ORDER_TO_DISPUTE_CACHE = {}
_DISPUTE_STATUS_CACHE = {}
# Guards creation/replacement of the shared session and CSRF token, so parallel
# requests (e.g. closing several disputes at once) never race on them.
_SESSION_LOCK = threading.RLock()
_CSRF_LOCK = threading.Lock()

def get_tista_http_session(force_refresh: bool = False):
    """
    Returns a persistent, keep-alive requests.Session loaded with Tista session cookies.
    Reuses TCP/TLS connections to execute requests in ~200-300ms.
    """
    if requests is None:
        return None
    if _TISTA_HTTP_SESSION is not None and not force_refresh:
        return _TISTA_HTTP_SESSION
    with _SESSION_LOCK:
        if _TISTA_HTTP_SESSION is not None and not force_refresh:
            return _TISTA_HTTP_SESSION
        return _build_tista_http_session()

# At most this many requests to Tista are in flight at once across the whole server
# (e.g. "Correction & Quality (Both)" runs two searches, each with its own worker pools).
MAX_TISTA_CONCURRENT_REQUESTS = 12
_TISTA_REQUEST_SLOTS = threading.BoundedSemaphore(MAX_TISTA_CONCURRENT_REQUESTS)

def _build_tista_http_session():
    global _TISTA_HTTP_SESSION

    s = requests.Session()
    _unthrottled_request = s.request

    def _throttled_request(*args, **kwargs):
        with _TISTA_REQUEST_SLOTS:
            return _unthrottled_request(*args, **kwargs)

    s.request = _throttled_request
    try:
        from requests.adapters import HTTPAdapter
        adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
    except Exception:
        pass
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    })

    # Search and load cookies
    for cp in [APP_DIR / "saved_cookies.json", resolve_profile_dir() / "saved_cookies.json", Path("saved_cookies.json"), Path("/data/saved_cookies.json")]:
        if cp.exists():
            try:
                with open(cp, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                for c in cookies:
                    s.cookies.set(c["name"], c["value"], domain=c.get("domain", "tista.app").lstrip("."), path=c.get("path", "/"))
                print(f"[✓] Initialized fast HTTP session with {len(cookies)} cookies from {cp.name}")
                break
            except Exception as ce:
                print(f"[!] Error loading {cp}: {ce}")

    _TISTA_HTTP_SESSION = s
    return s

def sync_cookies_from_playwright_context(ctx):
    """Dumps fresh cookies from Playwright browser into saved_cookies.json and refreshes HTTP session."""
    try:
        cookies = ctx.cookies()
        if cookies:
            for sp in [APP_DIR / "saved_cookies.json", resolve_profile_dir() / "saved_cookies.json"]:
                try:
                    with open(sp, "w", encoding="utf-8") as f:
                        json.dump(cookies, f, indent=2)
                except Exception:
                    pass
            s = get_tista_http_session(force_refresh=True)
            print(f"[✓] Synced {len(cookies)} cookies from Playwright to fast HTTP session")
            try:
                get_cached_csrf_token(s, force_refresh=True)
            except Exception:
                pass
    except Exception as e:
        print(f"[!] Cookie sync note: {e}")

def get_cached_csrf_token(session=None, force_refresh: bool = False, failed_token: str = None):
    """
    Returns the cached CSRF token. If missing or forced, fetches once from disputes list.
    Pass failed_token when refreshing after a 422: if another thread already replaced that
    token, the new one is returned without another fetch.
    Also opportunistically pre-populates _ORDER_TO_DISPUTE_CACHE and _DISPUTE_STATUS_CACHE.
    """
    if _CACHED_CSRF_TOKEN and not force_refresh:
        return _CACHED_CSRF_TOKEN
    with _CSRF_LOCK:
        if _CACHED_CSRF_TOKEN and (not force_refresh or (failed_token and _CACHED_CSRF_TOKEN != failed_token)):
            return _CACHED_CSRF_TOKEN
        return _fetch_csrf_token(session)

def _fetch_csrf_token(session=None):
    global _CACHED_CSRF_TOKEN
    s = session or get_tista_http_session()
    try:
        resp = s.get(f"{BASE_URL}/manage/vendors/1/order_disputes?per_page=100", timeout=8)
        if resp.status_code == 200:
            html = resp.text
            m = re.search(r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"', html) or re.search(r'<meta[^>]+content="([^"]+)"[^>]+name="csrf-token"', html)
            if m:
                _CACHED_CSRF_TOKEN = m.group(1)

            # Pre-cache all visible disputes
            for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL):
                tds = [re.sub(r'<[^>]+>', '', td).strip() for td in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.DOTALL)]
                if len(tds) >= 12 and tds[0].isdigit():
                    d_id = tds[0]
                    o_id = re.sub(r"\D", "", tds[1])
                    if o_id:
                        _ORDER_TO_DISPUTE_CACHE[o_id] = d_id
                    _DISPUTE_STATUS_CACHE[d_id] = tds[11]
    except Exception as e:
        print(f"[!] Error pre-fetching CSRF & dispute cache: {e}")

    return _CACHED_CSRF_TOKEN

def prepopulate_dispute_cache(session=None):
    """Pre-warms the HTTP session and loads active disputes + CSRF token into memory."""
    try:
        s = session or get_tista_http_session()
        get_cached_csrf_token(s, force_refresh=True)
        print(f"[✓] Pre-warmed Tista cache: {len(_ORDER_TO_DISPUTE_CACHE)} order disputes cached.")
    except Exception as e:
        print(f"[!] Pre-warming dispute cache note: {e}")

def close_dispute_via_direct_http(dispute_id: str = None, order_id: str = None, amount: float = 2.0, comment: str = None, fault_severity: str = None):
    """
    Submits dispute closure via pure HTTP requests with zero browser launch overhead.
    Takes ~0.4 - 0.8 seconds total.
    Returns (True, result_dict) on success, or (False, error_reason) on failure.
    """
    clean_did = str(dispute_id or "").strip()
    if clean_did and (not clean_did.isdigit() or clean_did.startswith("SLIDE-")):
        clean_did = ""
    raw_oid = re.sub(r"\D", "", str(order_id or ""))
    charge_amt = round(max(2.0, float(amount or 2.0)), 2)

    s = get_tista_http_session()
    if not s:
        return False, "HTTP session not available"

    # 1. Resolve dispute_id from cache if missing
    if not clean_did and raw_oid and raw_oid in _ORDER_TO_DISPUTE_CACHE:
        clean_did = _ORDER_TO_DISPUTE_CACHE[raw_oid]
        print(f"[*] [Fast HTTP] Using cached Dispute ID #{clean_did} for Order #{raw_oid}")

    # 2. If the in-memory cache says closed, confirm with Tista before skipping the update
    #    (the cache can be stale if the dispute was reopened).
    if clean_did and "close" in str(_DISPUTE_STATUS_CACHE.get(clean_did, "")).lower():
        live_status = read_dispute_status(s, clean_did)
        if live_status != "closed":
            _DISPUTE_STATUS_CACHE.pop(clean_did, None)
        else:
            print(f"[✓] [Fast HTTP] Dispute #{clean_did} is already closed on Tista.")
            return True, {
                "success": True,
                "dispute_id": clean_did,
                "order_id": order_id or f"#{raw_oid}",
                "amount": charge_amt,
                "status": "Closed",
                "message": f"Dispute #{clean_did} is already closed on Tista."
            }

    # 3. If dispute_id still unknown, do ONE fast search
    if not clean_did and raw_oid:
        try:
            search_url = f"{BASE_URL}/manage/vendors/1/order_disputes?q[fulfillment_order_order_name_cont]={raw_oid}"
            resp = s.get(search_url, timeout=8)
            if resp.status_code == 200:
                html = resp.text
                global _CACHED_CSRF_TOKEN
                if not _CACHED_CSRF_TOKEN:
                    m_csrf = re.search(r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"', html) or re.search(r'<meta[^>]+content="([^"]+)"[^>]+name="csrf-token"', html)
                    if m_csrf:
                        _CACHED_CSRF_TOKEN = m_csrf.group(1)

                for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL):
                    tds = [re.sub(r'<[^>]+>', '', td).strip() for td in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.DOTALL)]
                    if tds and len(tds) >= 2 and tds[0].strip().isdigit():
                        cand_did = tds[0].strip()
                        cand_oid = re.sub(r"\D", "", tds[1])
                        if not raw_oid or cand_oid == raw_oid:
                            clean_did = cand_did
                            _ORDER_TO_DISPUTE_CACHE[raw_oid] = clean_did
                            status_cand = tds[11].strip() if len(tds) >= 12 else ""
                            if status_cand:
                                _DISPUTE_STATUS_CACHE[clean_did] = status_cand
                            if "close" in status_cand.lower() or "done" in status_cand.lower():
                                return True, {
                                    "success": True,
                                    "dispute_id": clean_did,
                                    "order_id": order_id or f"#{raw_oid}",
                                    "amount": charge_amt,
                                    "status": "Closed",
                                    "message": f"Dispute #{clean_did} for Order #{raw_oid} is already closed on Tista."
                                }
                            break
        except Exception as se:
            print(f"[!] Fast HTTP search note: {se}")

    if not clean_did:
        return False, f"Dispute ID not found for Order #{raw_oid or order_id}"

    # 4. Get session CSRF token (instant if cached)
    csrf_token = get_cached_csrf_token(s)
    if not csrf_token:
        try:
            edit_url = f"{BASE_URL}/manage/vendors/1/order_disputes/{clean_did}/edit"
            resp = s.get(edit_url, timeout=8)
            if resp.status_code == 200:
                html = resp.text
                m_status = re.search(r'<select[^>]+name="order_dispute\[status\]"[^>]*>([\s\S]*?)</select>', html, re.I)
                if m_status and "closed" in m_status.group(1).lower() and 'selected="selected"' in m_status.group(1):
                    _DISPUTE_STATUS_CACHE[clean_did] = "Closed"
                    return True, {
                        "success": True,
                        "dispute_id": clean_did,
                        "order_id": order_id or f"#{raw_oid}",
                        "amount": charge_amt,
                        "status": "Closed",
                        "message": f"Dispute #{clean_did} is already closed on Tista."
                    }
                m_csrf = re.search(r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"', html) or re.search(r'name="authenticity_token"\s+value="([^"]+)"', html)
                if m_csrf:
                    csrf_token = m_csrf.group(1)
                    _CACHED_CSRF_TOKEN = csrf_token
        except Exception as ee:
            print(f"[!] Edit page check note: {ee}")

    if not csrf_token:
        return False, "Could not obtain CSRF token for Tista session"

    # 5. Build payload & headers
    post_url = f"{BASE_URL}/manage/vendors/1/order_disputes/{clean_did}"
    form_data = {
        "_method": "patch",
        "authenticity_token": csrf_token,
        "order_dispute[chargeable_to]": "vendor",
        "order_dispute[charge_to_vendor]": f"{charge_amt:.2f}",
        "order_dispute[status]": "closed",
        "commit": "Update Order dispute"
    }
    if fault_severity:
        raw_sev = str(fault_severity).strip().lower().replace(" ", "_")
        sev_map = {
            "minor": "low", "low": "low",
            "medium": "medium",
            "major": "high", "high": "high",
            "critical": "critical",
            "not_applicable": "not_applicable"
        }
        form_data["order_dispute[fault_severity]"] = sev_map.get(raw_sev, "low")
    if comment:
        form_data["order_dispute[comment]"] = comment

    headers = {
        "X-CSRF-Token": csrf_token,
        "Referer": f"{BASE_URL}/manage/vendors/1/order_disputes/{clean_did}/edit",
        "Origin": BASE_URL,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    # 6. Execute direct POST. Rails answers a successful update with a redirect (302/303);
    #    a 200 means the form was re-rendered with validation errors, i.e. NOT saved.
    def _post(form):
        return s.post(post_url, data=form, headers=headers, timeout=10, allow_redirects=False)

    def _saved(resp):
        loc = resp.headers.get("Location", "")
        return resp.status_code in (301, 302, 303) and "sign_in" not in loc and "/edit" not in loc

    t0 = time.time()
    try:
        post_resp = _post(form_data)
        print(f"[*] [Fast HTTP] POST to dispute #{clean_did} status: {post_resp.status_code} ({time.time() - t0:.3f}s)")

        # 422 = CSRF token expired: refresh it and retry once.
        if post_resp.status_code == 422:
            print("[*] [Fast HTTP] 422 received. Refreshing CSRF token and retrying...")
            fresh_csrf = get_cached_csrf_token(s, force_refresh=True, failed_token=form_data["authenticity_token"])
            if fresh_csrf:
                form_data["authenticity_token"] = fresh_csrf
                headers["X-CSRF-Token"] = fresh_csrf
                post_resp = _post(form_data)

        # 5xx = server error: retry once without the optional fields (severity, comment).
        if post_resp.status_code >= 500:
            print("[*] [Fast HTTP] Server error. Retrying once with the essential fields only...")
            min_form = {k: v for k, v in form_data.items()
                        if k not in ("order_dispute[fault_severity]", "order_dispute[comment]")}
            post_resp = _post(min_form)

        elapsed = time.time() - t0
        if not _saved(post_resp):
            if post_resp.status_code in (301, 302, 303):
                reason = "Tista session expired - please log in again"
            elif post_resp.status_code == 200:
                errs = re.findall(r'class="[^"]*(?:invalid-feedback|error|alert-danger)[^"]*"[^>]*>([^<]{3,200})<', post_resp.text)
                reason = "Tista rejected the update" + (f": {html_unescape(errs[0].strip())}" if errs else " (form returned with errors)")
            else:
                reason = f"Tista returned status {post_resp.status_code}"
            print(f"[!] [Fast HTTP] Dispute #{clean_did} NOT closed: {reason}")
            return False, reason

        # Confirm the dispute really is closed now.
        live_status = read_dispute_status(s, clean_did)
        if live_status and live_status != "closed":
            print(f"[!] [Fast HTTP] Dispute #{clean_did} saved but status is '{live_status}', not closed")
            return False, f"Tista saved the dispute but its status is '{live_status}', not closed"

        _DISPUTE_STATUS_CACHE[clean_did] = "Closed"
        return True, {
            "success": True,
            "dispute_id": clean_did,
            "order_id": order_id or f"#{raw_oid}",
            "amount": charge_amt,
            "status": "Closed",
            "verified": live_status == "closed",
            "elapsed_seconds": round(elapsed, 3),
            "message": f"Successfully charged ${charge_amt:.2f} to Vendor and closed dispute #{clean_did} ({elapsed:.2f}s)."
        }
    except Exception as pe:
        print(f"[!] Fast HTTP error for dispute #{clean_did}: {pe}")
        return False, str(pe)

COMMENT_CACHE_TTL_SECONDS = 6 * 3600

def _comment_cache_path(dispute_id: str) -> Path:
    return DATA_DIR / f"tista_comment_{dispute_id}.json"

def load_cached_comment(dispute_id: str):
    """(comment, link) read within the last COMMENT_CACHE_TTL_SECONDS, else None."""
    p = _comment_cache_path(dispute_id)
    try:
        if p.exists() and time.time() - p.stat().st_mtime < COMMENT_CACHE_TTL_SECONDS:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d.get("comment", ""), d.get("comment_link", "")
    except Exception:
        pass
    return None

def save_cached_comment(dispute_id: str, comment: str, link: str):
    try:
        _comment_cache_path(dispute_id).write_text(json.dumps({"dispute_id": dispute_id, "comment": comment, "comment_link": link}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def read_dispute_comment(session, dispute_id: str):
    """Returns (comment, first_link) from the dispute's Tista edit page over plain HTTP; ("", "") if unavailable."""
    try:
        r = session.get(f"{BASE_URL}/manage/vendors/1/order_disputes/{dispute_id}/edit", timeout=10)
        if r.status_code == 200:
            m = re.search(r'<textarea[^>]+(?:id=[\x22\x27]order_dispute_comment[\x22\x27]|name=[^>]*comment)[^>]*>([\s\S]*?)</textarea>', r.text, re.I)
            if m:
                c_text = html_unescape(m.group(1).strip())
                urls = [u.rstrip(".,;!?')\"") for u in re.findall(r"https?://[^\s<>\x22\x27,]+", c_text)]
                return c_text, (urls[0] if urls else "")
    except Exception:
        pass
    return "", ""

def read_dispute_status(session, dispute_id: str):
    """Returns the dispute's current status value from its Tista edit page (e.g. 'closed'), or None if unreadable."""
    try:
        r = session.get(f"{BASE_URL}/manage/vendors/1/order_disputes/{dispute_id}/edit", timeout=8, allow_redirects=False)
        if r.status_code != 200:
            return None
        sel = re.search(r'<select[^>]+name="order_dispute\[status\]"[^>]*>([\s\S]*?)</select>', r.text, re.I)
        if not sel:
            return None
        opt = re.search(r'<option[^>]*selected[^>]*value="([^"]+)"|<option[^>]*value="([^"]+)"[^>]*selected', sel.group(1))
        return (opt.group(1) or opt.group(2)) if opt else None
    except Exception:
        return None

def close_dispute_on_tista(dispute_id: str = None, order_id: str = None, amount: float = 2.0, comment: str = None, fault_severity: str = None, headless: bool = False):
    """
    Charges the specified amount to vendor and closes the dispute on Tista.
    Uses ultra-fast direct HTTP session (completes in ~0.4 - 0.8s with zero browser launch overhead).
    """
    started = time.time()
    try:
        ok, res = close_dispute_via_direct_http(
            dispute_id=dispute_id,
            order_id=order_id,
            amount=amount,
            comment=comment,
            fault_severity=fault_severity
        )
    except Exception as e:
        ok, res = False, str(e)

    if ok:
        print(f"[✓] Dispute closed via fast HTTP: {res.get('message')}")
        _log_close_attempt(dispute_id, order_id, amount, fault_severity, True, res, started)
        return res

    err_msg = res if isinstance(res, str) else str(res.get("error") if isinstance(res, dict) else "Failed to close dispute")
    print(f"[!] HTTP dispute close note: {err_msg}")
    out = {
        "status": "error",
        "error": err_msg,
        "dispute_id": dispute_id,
        "order_id": order_id
    }
    _log_close_attempt(dispute_id, order_id, amount, fault_severity, False, out, started)
    return out

CLOSE_LOG_FILE = DATA_DIR / "close_log.jsonl"
CLOSE_CONCURRENCY = 4
_CLOSE_LOG_LOCK = threading.Lock()

def _log_close_attempt(dispute_id, order_id, amount_requested, fault_severity, success, res, started):
    """Appends one line per close attempt to close_log.jsonl - the record of what was charged."""
    entry = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dispute_id": str((res or {}).get("dispute_id") or dispute_id or ""),
        "order_id": str(order_id or ""),
        "amount_requested": amount_requested,
        "amount_charged": (res or {}).get("amount") if success else None,
        "fault_severity": fault_severity,
        "success": bool(success),
        "verified": (res or {}).get("verified") if success else None,
        "error": None if success else (res or {}).get("error"),
        "seconds": round(time.time() - started, 2),
    }
    try:
        with _CLOSE_LOG_LOCK, open(CLOSE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[!] Could not write close log: {e}")

def batch_close_disputes_on_tista(items: list, headless: bool = False):
    """
    Charges to vendor and closes multiple disputes via the direct HTTP session,
    CLOSE_CONCURRENCY at a time. Every attempt is written to close_log.jsonl.
    """
    if not items:
        return {"total": 0, "closed": 0, "results": []}

    s = get_tista_http_session()
    if not s:
        return {"total": len(items), "closed": 0, "results": [], "error": "HTTP session not available"}
    get_cached_csrf_token(s)

    def _close(it):
        r = close_dispute_on_tista(
            dispute_id=str(it.get("dispute_id") or "").strip(),
            order_id=str(it.get("order_id") or "").strip(),
            amount=float(it.get("amount") or 2.0),
            comment=it.get("comment") or "",
            fault_severity=it.get("fault_severity") or None,
        )
        if r.get("error"):
            r = {**r, "success": False}
        return r

    print(f"[*] Closing {len(items)} disputes, {CLOSE_CONCURRENCY} at a time...")
    with ThreadPoolExecutor(max_workers=CLOSE_CONCURRENCY) as pool:
        results = list(pool.map(_close, items))

    success_count = sum(1 for r in results if r.get("success"))
    return {
        "total": len(items),
        "closed": success_count,
        "results": results
    }

def check_tista_login_status():
    """Checks if Tista profile exists and has saved session cookies/storage."""
    profile_dir = resolve_profile_dir()
    if not profile_dir.exists():
        return {"logged_in": False, "reason": "No profile directory"}
    has_cookies = False
    for cp in [profile_dir / "saved_cookies.json", APP_DIR / "saved_cookies.json", profile_dir / "Default" / "Network" / "Cookies", profile_dir / "Default" / "Cookies", profile_dir / "Cookies"]:
        if cp.exists() and cp.stat().st_size > 0:
            has_cookies = True
            break
    return {"logged_in": has_cookies, "profile_dir": str(profile_dir)}

def login_tista_direct_http(email, password):
    """Logs in to Tista via direct HTTP POST. Ultra-lightweight (~2MB RAM, 0.3s)."""
    if not email or not password or not requests:
        return False
    try:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        r_get = s.get(f"{BASE_URL}/users/sign_in", timeout=12)
        if r_get.status_code != 200:
            return False

        m_csrf = re.search(r'name=["\']authenticity_token["\']\s+value=["\']([^"\']+)["\']', r_get.text)
        if not m_csrf:
            m_csrf = re.search(r'name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', r_get.text)
        token = m_csrf.group(1) if m_csrf else ""

        payload = {
            "authenticity_token": token,
            "user[email]": email,
            "user[password]": password,
            "user[remember_me]": "1",
            "commit": "Log in"
        }
        r_post = s.post(f"{BASE_URL}/users/sign_in", data=payload, timeout=15, allow_redirects=True)
        if "sign_in" not in r_post.url.lower() and "login" not in r_post.url.lower():
            cookies = []
            for cookie in s.cookies:
                cookies.append({
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain or "tista.app",
                    "path": cookie.path or "/",
                    "secure": cookie.secure,
                    "httpOnly": cookie.has_nonstandard_attr("HttpOnly")
                })
            for out_p in [DATA_DIR / "saved_cookies.json", APP_DIR / "saved_cookies.json", resolve_profile_dir() / "saved_cookies.json"]:
                try:
                    out_p.parent.mkdir(parents=True, exist_ok=True)
                    out_p.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
                except Exception:
                    pass
            get_tista_http_session(force_refresh=True)
            print("[✓] Successfully logged in to Tista via fast direct HTTP API!")
            return True
    except Exception as e:
        print(f"[*] Direct HTTP login note: {e}")
    return False

def perform_tista_login(email=None, password=None):
    """Logs in to Tista. Local runs open visible Chrome matching unified_app."""
    profile_dir = resolve_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)

    email = (email or os.environ.get("TISTA_EMAIL", "")).strip()
    password = (password or os.environ.get("TISTA_PASSWORD", "")).strip()

    # If email and password provided, try ultra-fast direct HTTP login first (avoids browser launch & memory spike)
    if email and password:
        if login_tista_direct_http(email, password):
            return {"status": "ok", "message": "Successfully logged in to Tista via direct API (ultra-low memory)"}

    # In cloud environments (Render), login runs headless with env credentials.
    # On local desktop, login opens a visible Google Chrome window so the user can sign in.
    headless = True if (IS_CLOUD_ENV and email and password) else False
    if IS_CLOUD_ENV and not (email and password):
        return {
            "status": "error",
            "error": "Tista credentials missing. Please set TISTA_EMAIL and TISTA_PASSWORD in Render Environment Variables."
        }

    print("\n" + "=" * 60)
    print(f" [*] Logging in to Tista (Headless: {headless})...")
    print("=" * 60 + "\n")

    if not sync_playwright:
        return {
            "status": "error",
            "error": "Playwright is not installed in the currently active Python environment. Please run: pip install playwright && playwright install chromium"
        }

    with sync_playwright() as p:
        ctx = launch_playwright_context(p, headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(DISPUTES_URL, wait_until="domcontentloaded")
        time.sleep(1)

        if email and password:
            print(" [✓] Submitting login credentials...")
            try:
                email_input = page.locator('#user_email, input[type="email"], input[name*="email"]').first
                pass_input = page.locator('#user_password, input[type="password"], input[name*="password"]').first
                if email_input.count() > 0 and pass_input.count() > 0:
                    email_input.fill(email)
                    pass_input.fill(password)
                    rem = page.locator('#user_remember_me, input[name*="remember_me"]').first
                    if rem.count() > 0 and not rem.is_checked():
                        rem.check()
                    submit_btn = page.locator('input[type="submit"], button[type="submit"], input[value*="Log in"], input[value*="Sign in"]').first
                    if submit_btn.count() > 0:
                        submit_btn.click()
                    else:
                        pass_input.press("Enter")

                    # Wait up to 20 seconds for navigation/login redirect
                    start_nav = time.time()
                    while time.time() - start_nav < 20:
                        time.sleep(1)
                        cur_url = page.url.lower()
                        if "sign_in" not in cur_url and "login" not in cur_url and page.locator('input[type="password"]').count() == 0:
                            break
            except Exception as e:
                print(f" [!] Login submission error: {e}")

        if not headless:
            try:
                page.bring_to_front()
            except Exception:
                pass
            # Wait until user signs in (URL leaves sign_in or no password box)
            start_t = time.time()
            while time.time() - start_t < 300:
                cur_url = page.url.lower()
                is_on_login = "sign_in" in cur_url or "login" in cur_url or page.locator('input[type="password"]').count() > 0
                if not is_on_login:
                    break
                time.sleep(1)

        is_logged_in = "sign_in" not in page.url and "login" not in page.url and page.locator('input[type="password"]').count() == 0

        err_msg = ""
        if not is_logged_in:
            try:
                alerts = page.locator('.alert-danger, .alert-warning, #flash_alert, .error_explanation, .invalid-feedback')
                if alerts.count() > 0:
                    txt = alerts.first.inner_text().strip()
                    if "You need to sign in or sign up" not in txt:
                        err_msg = txt
                if not err_msg:
                    all_alerts = page.locator('.alert, .notice, #flash_notice, .flash-message')
                    if all_alerts.count() > 0:
                        txt = all_alerts.first.inner_text().strip()
                        if "You need to sign in or sign up" not in txt:
                            err_msg = txt
            except Exception:
                pass

        time.sleep(1)
        ctx.close()

    if is_logged_in:
        print("[✓] Login flow completed. Session saved to profile.\n")
        return {"status": "success", "message": "Login completed successfully"}
    else:
        detail = f": {err_msg}" if err_msg else ". Please verify your Tista Email and Password."
        return {"status": "error", "error": f"Login failed{detail}"}

def clean_html_block(html):
    t = re.sub(r'<br\s*/?>', '\n', html, flags=re.I)
    t = re.sub(r'</?(?:p|div|tr|td|table|section|li|h[1-6])\b[^>]*>', '\n', t, flags=re.I)
    t = re.sub(r'<[^>]+>', ' ', t)
    t = t.replace('&nbsp;', ' ').replace('&gt;', '>').replace('&lt;', '<').replace('&amp;', '&').replace('&#8203;', '')
    t = t.replace('\u2028', '\n').replace('\u2029', '\n').replace('\u00a0', ' ').replace('\xa0', ' ')
    return t

def clean_file_names_list(raw_list):
    res = []
    seen = set()
    bad_prefixes = (
        "checked by", "report type", "job id", "order id", "instruction",
        "service:", "services:", "note:", "investigation", "format:", "correction",
        "error severity", "fault severity", "original image", "completed image",
        "fault area", "feedback", "date:", "subs:"
    )
    for item in raw_list:
        parts = re.split(r'[\r\n\x0b\u2028\u2029\ufffd]+', str(item))
        for p in parts:
            cleaned = re.sub(r'^[^\w#]+', '', p).strip()
            cleaned = cleaned.strip()
            if len(cleaned) >= 2 and cleaned not in seen and not cleaned.lower().startswith("ul.") and not cleaned.lower().startswith("http"):
                low = cleaned.lower()
                if low in ("--", "---", "----", "none", "n/a", "all image", "all images", "all files"):
                    continue
                if any(low.startswith(bp) for bp in bad_prefixes):
                    continue
                seen.add(cleaned)
                res.append(cleaned)
    return res

def extract_slide_services(slide_text: str) -> list:
    """
    Extracts services ONLY from the 'Service:' block/field in a Google Slide.
    Strictly ignores instructions, notes, investigations, feedbacks, and error descriptions.
    Supports single service or multiple services separated by commas, slashes, pluses, ampersands, or newlines (including Shift+Enter / \u2028).
    """
    found = []
    # Normalize unicode line breaks
    normalized_text = slide_text.replace('\u2028', '\n').replace('\u2029', '\n').replace('\u00a0', ' ').replace('\xa0', ' ')

    # Match ONLY the Service: block up to the next section header or boundary
    pattern = r"Services?\s*[:#-]*\s*([\s\S]*?)(?=(?:\b(?:Format|Instruction|Note|Image\s*name|Checked\s*by|Report\s*type|Job\s*ID|Order\s*ID|Quantity|Date|Subs?|Correction|Error\s*Severity|Severity|Investigation|Feedback|Issue\s*report|Original\s*image|Completed\s*image)\b|$))"
    svc_blocks = re.findall(pattern, normalized_text, re.I)

    def match_single_service(text):
        t = text.strip()
        if not t:
            return None
        return resolve_service_name(t)

    for block in svc_blocks:
        b_clean = block.strip()
        if not b_clean:
            continue
        # Pre-check multi-word services with punctuation like 'Dust, spot and scratch removal'
        remaining = b_clean
        for special_svc in ["Dust, spot and scratch removal", "Straighten, crop and set margin"]:
            sp_pat = re.compile(r"\b" + re.escape(special_svc) + r"\b", re.I)
            if sp_pat.search(remaining):
                if special_svc not in found:
                    found.append(special_svc)
                remaining = sp_pat.sub(" ", remaining)

        dust_abbr = re.search(r"\bdust[,\s]+spot[^\n\r,+/&]*", remaining, re.I)
        if dust_abbr:
            if "Dust, spot and scratch removal" not in found:
                found.append("Dust, spot and scratch removal")
            remaining = remaining[:dust_abbr.start()] + " " + remaining[dust_abbr.end():]

        lines = re.split(r"[\r\n\x0b\u2028\u2029\ufffd]+", remaining)
        for l in lines:
            tokens = re.split(r"[,/\\+&]|\band\b", l, flags=re.I)
            for tok in tokens:
                tok = tok.strip()
                if not tok or len(tok) < 2:
                    continue
                # Skip any stray label words
                if tok.lower() in ("service", "services", "format", "instruction", "note", "none"):
                    continue
                matched = match_single_service(tok)
                if matched and matched in KNOWN_SERVICES and matched not in found:
                    found.append(matched)

    if not found:
        # Fallback if Service: keyword was not found or empty
        found = ["Clipping path"]
    return found

def parse_google_slides_presentation(slide_url: str):
    """
    Fetches and parses a Google Slides presentation URL to extract:
    order_id, contractor, service, services, severity, filenames, is_all_images, total_qty, fault_qty
    Supports both Quality Audit Reports and Correction Slides with multi-service chargeback.
    """
    raw_url = str(slide_url or "").strip()
    m = re.search(r"/presentation/d/([a-zA-Z0-9_-]+)", raw_url)
    if not m:
        if re.match(r"^[a-zA-Z0-9_-]{20,}$", raw_url):
            pres_id = raw_url
        else:
            return {"error": "Invalid Google Slides URL or ID"}
    else:
        pres_id = m.group(1)

    # 1. First attempt: HTML export (provides precise per-slide <section> isolation)
    export_html_url = f"https://docs.google.com/presentation/d/{pres_id}/export/html"
    print(f"\n[*] Fetching Google Slides HTML export from: {export_html_url}")
    
    html = ""
    try:
        req = urllib.request.Request(
            export_html_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[!] HTML export failed: {e}. Will attempt TXT export fallback.")

    parsed_jobs = []
    sections = re.findall(r"<section\b[^>]*>([\s\S]*?)</section>", html) if html else []

    # If urllib export/html failed or produced no sections, attempt authenticated Playwright fetch
    if not sections and sync_playwright:
        try:
            print(f"[*] Attempting authenticated Playwright fetch for Google Slides: {export_html_url}")
            with sync_playwright() as p:
                ctx = launch_playwright_context(p, headless=True)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(export_html_url, wait_until="domcontentloaded", timeout=15000)
                page_content = page.content()
                secs = re.findall(r"<section\b[^>]*>([\s\S]*?)</section>", page_content)
                if secs:
                    sections = secs
                else:
                    page.goto(raw_url, wait_until="domcontentloaded", timeout=15000)
                    time.sleep(2.0)
                    slide_texts = page.evaluate("""() => {
                        const slides = [];
                        const pages = document.querySelectorAll('.punch-viewer-svgpage, .punch-viewer-page, .punch-filmstrip-thumbnail');
                        if (pages.length) {
                            pages.forEach(p => slides.push(p.innerText || p.textContent || ''));
                        } else {
                            slides.push(document.body.innerText || '');
                        }
                        return slides;
                    }""")
                    if slide_texts:
                        sections = [f"<section>{txt}</section>" for txt in slide_texts if txt.strip()]
                ctx.close()
        except Exception as pe:
            print(f"[!] Playwright slides fallback note: {pe}")

    if sections:
        for s_idx, sec in enumerate(sections):
            slide_text = clean_html_block(sec)

            # Slide ID (if present in tag attributes)
            s_id_m = re.search(r'id=["\'](?:slide-id-)?([a-zA-Z0-9_-]+)["\']', sec) or re.search(r'data-slide-id=["\']([a-zA-Z0-9_-]+)["\']', sec)
            slide_id_val = s_id_m.group(1) if s_id_m else f"p{s_idx + 1}"
            
            # Order ID
            m_oid = re.search(r"(?:Job|Order)\s*ID\s*[:#-]*\s*#?(\d+)", slide_text, re.I)
            if not m_oid:
                continue
            order_id = m_oid.group(1)
            
            # Contractor / Sub Name
            m_sub = re.search(r"(?:Sub\.?\s*Name|Subs?)\s*[:#-]*\s*([^\n\r]+)", slide_text, re.I)
            contractor = m_sub.group(1).strip() if m_sub else ""
            contractor = re.sub(r"\s*(?:None|Checked|Report).*", "", contractor, flags=re.I).strip()
            
            # Report Type / Dispute Reason
            m_rep = re.search(r"Report\s*Type\s*[:#-]*\s*([^\n\r<]+)", slide_text, re.I)
            raw_rep = m_rep.group(1).strip() if m_rep else ""

            # Rule: Skip SM Correction slides completely per user instructions (not chargeable to vendor)
            if re.search(r"\b(?:SM\s*Correction|Instruction\s*mistake\s*by\s*SM)\b", slide_text, re.I) or "sm correction" in raw_rep.lower():
                print(f"[-] Skipping Slide {s_idx + 1} for Order #{order_id}: Report type is SM Correction.")
                continue

            clean_reason = "Correction"
            if re.search(r"quality", raw_rep, re.I):
                clean_reason = "Quality"
            elif re.search(r"delay", raw_rep, re.I):
                clean_reason = "Delay"
            elif re.search(r"revision", raw_rep, re.I):
                clean_reason = "Revision"
            elif re.search(r"technical", raw_rep, re.I):
                clean_reason = "Technical fault"
            elif re.search(r"correction", raw_rep, re.I):
                clean_reason = "Correction"
            elif raw_rep:
                clean_reason = raw_rep

            # Severity
            m_sev = re.search(r"(?:Error|Fault)\s*Severity\s*[:#-]*\s*([a-zA-Z]+)", slide_text, re.I)
            sev_raw = m_sev.group(1).strip() if m_sev else "Low"
            sev_map = {"low": "Minor", "medium": "Medium", "major": "Major", "high": "Major", "multiple": "Multiple"}
            severity = sev_map.get(sev_raw.lower(), "Minor")
            
            # Quantity / Fault
            tot_qty = None
            fault_qty = None
            m_qf = re.search(r"(?:Checked\s*/\s*Fault|Quantity\s*/\s*Fault)\s*[:#-]*\s*(\d+)\s*/\s*(\d+)", slide_text, re.I)
            if m_qf:
                tot_qty = int(m_qf.group(1))
                fault_qty = int(m_qf.group(2))
            else:
                m_iq = re.search(r"Image\s*Quantity\s*[:#-]*\s*(\d+)", slide_text, re.I)
                if m_iq:
                    tot_qty = int(m_iq.group(1))

            # Services (can be single or multiple, e.g. "Ghost mannequin, Clipping path")
            slide_services = extract_slide_services(slide_text)
            service = slide_services[0] if slide_services else "Clipping path"

            # Filenames & All images
            is_all_images = bool(re.search(r"\b(?:all\s*images?|all\s*files?)\b", slide_text, re.I))
            file_list = []
            
            if not is_all_images:
                shapes = re.findall(r'<div[^>]*class=["\'](?:shape|group)["\'][^>]*style=["\']([^"\']*)["\'][^>]*>([\s\S]*?)</div>', sec)
                for style, inner in shapes:
                    st_clean = clean_html_block(inner)
                    lines = [l.strip() for l in re.split(r'[\r\n\x0b]+', st_clean) if l.strip()]
                    
                    if any("image name" in l.lower() for l in lines):
                        started = False
                        for l in lines:
                            if "image name" in l.lower():
                                raw_after = re.sub(r'^.*?image\s*name\s*[:#-]*\s*', '', l, flags=re.I).strip()
                                after = raw_after.lstrip("•-* \t").strip()
                                if raw_after in ("--", "---", "----", "none", "n/a") or after.lower() in ("all image", "all images", "all files"):
                                    is_all_images = True
                                    file_list = []
                                    break
                                if after and not after.lower().startswith("ul.") and len(after) > 2:
                                    file_list.append(after)
                                    started = True
                                    continue
                                elif not after:
                                    started = True
                                    continue
                            if started:
                                cl = l.lstrip("•-* \t").strip()
                                if cl.lower().startswith("ul.") or "{" in cl or "list-style" in cl:
                                    continue
                                if any(cl.lower().startswith(p) for p in [
                                    "original image", "completed image", "fault area", "none", "checked by",
                                    "report type", "job id", "order id", "instruction", "service", "note",
                                    "investigation", "format", "correction", "error severity", "date:", "subs:"
                                ]):
                                    break
                                if cl.lower() in ("all image", "all images", "--", "---"):
                                    is_all_images = True
                                    file_list = []
                                    break
                                if len(cl) >= 2:
                                    file_list.append(cl)
                        if file_list or is_all_images:
                            break

                    if ("left:640px" in style or "left: 640px" in style) and ("top:34" in style or "height:14" in style):
                        for l in lines:
                            cl = l.lstrip("•-* \t").strip()
                            if cl.lower() in ("image name:", "image name", "original image", "completed image", "fault area", "none"):
                                continue
                            if cl.lower() in ("all image", "all images"):
                                is_all_images = True
                                break
                            if len(cl) >= 2:
                                file_list.append(cl)
                        if file_list or is_all_images:
                            break

                if not file_list and not is_all_images:
                    top_m = re.search(r"^([\s\S]*?)(?=Type of error|Error Severity|Fault severity)", slide_text, re.I)
                    if top_m:
                        lines = [l.strip() for l in re.split(r'[\r\n\x0b]+', top_m.group(1)) if l.strip()]
                        for l in lines:
                            cl = l.lstrip("•-* \t").strip()
                            if cl.lower() in ("image name:", "image name", "original image", "completed image", "fault area", "none"):
                                continue
                            if cl.lower() in ("all image", "all images"):
                                is_all_images = True
                                break
                            if len(cl) >= 2 and not cl.startswith("http") and not cl.lower().startswith("report"):
                                file_list.append(cl)

            cleaned_filenames = clean_file_names_list(file_list)
            parsed_jobs.append({
                "slide_id": slide_id_val,
                "order_id": order_id,
                "contractor": contractor,
                "service": service,
                "services": slide_services,
                "severity": severity,
                "total_qty": tot_qty,
                "fault_qty": fault_qty,
                "reason": clean_reason,
                "report_type": raw_rep or clean_reason,
                "is_all_images": is_all_images,
                "filenames": cleaned_filenames
            })

    # If HTML export returned no jobs, fall back to TXT export
    if not parsed_jobs:
        export_txt_url = f"https://docs.google.com/presentation/d/{pres_id}/export/txt"
        print(f"[*] Falling back to TXT export: {export_txt_url}")
        try:
            req = urllib.request.Request(
                export_txt_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
                chunks = re.split(r"(?=Checked by:)", text)
                for c in chunks:
                    job_m = re.search(r"(?:Job|Order)\s*ID:\s*#?(\d+)", c, re.I)
                    if not job_m: continue
                    order_id = job_m.group(1)
                    subs_m = re.search(r"(?:Sub\.?\s*Name|Subs):\s*([^\n\r]+)", c, re.I)
                    contractor = subs_m.group(1).strip() if subs_m else ""
                    slide_services = extract_slide_services(c)
                    service = slide_services[0] if slide_services else "Clipping path"
                    sev_m = re.search(r"(?:Error|Fault)\s*Severity\s*[:-]+\s*([a-zA-Z]+)", c, re.I)
                    severity_raw = sev_m.group(1).strip() if sev_m else "Low"
                    sev_map = {"low": "Minor", "medium": "Medium", "major": "Major", "high": "Major"}
                    severity = sev_map.get(severity_raw.lower(), "Minor")
                    qf_m = re.search(r"(?:Checked\s*/\s*Fault|Quantity/Fault):\s*(\d+)\s*/\s*(\d+)", c, re.I)
                    tot_qty = int(qf_m.group(1)) if qf_m else None
                    fault_qty = int(qf_m.group(2)) if qf_m else None
                    img_m = re.search(r"Image name:\s*([\s\S]*?)(?=Instruction:|Service:|Note:|Investigation:|Format:|Checked by:|$)", c, re.I)
                    raw_images = img_m.group(1).strip() if img_m else ""
                    is_all_images = bool(re.search(r"\ball\s*images?\b", raw_images, re.I))
                    fl = []
                    if not is_all_images:
                        for line in raw_images.splitlines():
                            cl = line.strip().lstrip("•-* \t").strip()
                            if cl and cl not in ("--", "---") and len(cl) >= 2:
                                fl.append(cl)
                    rep_m = re.search(r"Report\s*type\s*[:#-]*\s*([^\n\r]+)", c, re.I)
                    raw_rep = rep_m.group(1).strip() if rep_m else ""
                    if re.search(r"\b(?:SM\s*Correction|Instruction\s*mistake\s*by\s*SM)\b", c, re.I) or "sm correction" in raw_rep.lower():
                        print(f"[-] Skipping TXT Slide for Order #{order_id}: Report type is SM Correction.")
                        continue
                    clean_reason = "Correction"
                    if re.search(r"quality", raw_rep, re.I):
                        clean_reason = "Quality"
                    elif re.search(r"delay", raw_rep, re.I):
                        clean_reason = "Delay"
                    elif re.search(r"revision", raw_rep, re.I):
                        clean_reason = "Revision"
                    elif re.search(r"technical", raw_rep, re.I):
                        clean_reason = "Technical fault"
                    elif re.search(r"correction", raw_rep, re.I):
                        clean_reason = "Correction"
                    elif raw_rep:
                        clean_reason = raw_rep

                    parsed_jobs.append({
                        "slide_id": f"p{len(parsed_jobs) + 1}",
                        "order_id": order_id,
                        "contractor": contractor,
                        "service": service,
                        "services": slide_services,
                        "severity": severity,
                        "total_qty": tot_qty,
                        "fault_qty": fault_qty,
                        "reason": clean_reason,
                        "report_type": raw_rep or clean_reason,
                        "is_all_images": is_all_images,
                        "filenames": clean_file_names_list(fl)
                    })
        except Exception as e:
            print(f"[!] TXT fallback error: {e}")

    # Check if any orders lack cached files, and batch-extract them in ONE fast Playwright pass
    uncached_slide_oids = []
    for j in parsed_jobs:
        raw_rep = str(j.get("report_type") or "")
        clean_reas = str(j.get("reason") or "")
        if re.search(r"sm\s*correction|instruction\s*mistake\s*by\s*sm", raw_rep, re.I) or re.search(r"sm\s*correction", clean_reas, re.I):
            continue
        oid = re.sub(r"\D", "", str(j.get("order_id", "")))
        if not oid: continue
        c_corr = DATA_DIR / f"tista_correction_{oid}.json"
        if not c_corr.exists(): c_corr = APP_DIR / f"tista_correction_{oid}.json"
        if not c_corr.exists():
            if oid not in uncached_slide_oids:
                uncached_slide_oids.append(oid)

    if uncached_slide_oids:
        print(f"[*] [Fast API] Parallel batch extracting {len(uncached_slide_oids)} uncached orders for Google Slides: {uncached_slide_oids}")
        failed_slide_oids = fetch_orders_files_direct_api_batch(uncached_slide_oids)
        if failed_slide_oids and sync_playwright:
            print(f"[*] Playwright fallback for {len(failed_slide_oids)} orders: {failed_slide_oids}")
            try:
                with sync_playwright() as p_slides:
                    ctx_slides = launch_playwright_context(p_slides, headless=True)
                    page_slides = ctx_slides.pages[0] if ctx_slides.pages else ctx_slides.new_page()
                    for u_oid in failed_slide_oids:
                        try:
                            extract_order_files_with_page(page_slides, u_oid)
                        except Exception as eo:
                            print(f"[!] Error extracting files for #{u_oid}: {eo}")
                    try:
                        ctx_slides.close()
                    except Exception:
                        pass
            except Exception as pe:
                print(f"[!] Batch Playwright fetch note: {pe}")

    # Attach local cached files (instant disk lookup in 0.001s, no network/Playwright overhead)
    for j in parsed_jobs:
        oid = re.sub(r"\D", "", str(j.get("order_id", "")))
        if not oid: continue
        c_corr = DATA_DIR / f"tista_correction_{oid}.json"
        if not c_corr.exists(): c_corr = APP_DIR / f"tista_correction_{oid}.json"
        if c_corr.exists():
            try:
                cdata = json.loads(c_corr.read_text(encoding="utf-8"))
                if cdata.get("files"):
                    j["cached_files"] = cdata["files"]
                    j["all_order_files"] = cdata["files"]
                    j["total_files"] = cdata.get("total_files") or len(cdata["files"])
                    ov = calculate_order_value_from_files_py(cdata["files"])
                    if ov > 0:
                        j["order_value"] = ov
            except Exception:
                pass

    gc.collect()
    return {
        "status": "success",
        "presentation_id": pres_id,
        "total_jobs": len(parsed_jobs),
        "jobs": parsed_jobs
    }

# ---------- Web API & Local Server Support ----------
# Only these files are served as static content; everything else (cookies, cached order JSON,
# source code) returns 404.
STATIC_FILES = {
    "/Chargeback%20Calculator.html", "/Chargeback Calculator.html",
    "/favicon.png", "/favicon.ico", "/logo.png", "/logo.ico",
}

# Extra origins allowed to call the API cross-site (e.g. the Vercel/GitHub Pages front end that
# talks to the Render backend), comma-separated: ALLOWED_ORIGINS=https://my-app.vercel.app
EXTRA_ALLOWED_ORIGINS = {o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()}
_LOCAL_ORIGIN_RE = re.compile(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$")

class TistaRequestHandler(SimpleHTTPRequestHandler):
    def _origin_allowed(self, origin: str) -> bool:
        origin = origin.rstrip("/")
        host = self.headers.get("Host", "")
        if origin in (f"http://{host}", f"https://{host}"):
            return True  # same origin
        if origin in EXTRA_ALLOWED_ORIGINS:
            return True
        # Allow trusted project frontends (Vercel, Render, GitHub Pages) and local files
        parsed = urlparse(origin)
        h = (parsed.hostname or "").lower()
        if h.endswith(".vercel.app") or h.endswith(".onrender.com") or h.endswith(".github.io"):
            return True
        if _LOCAL_ORIGIN_RE.match(origin) or origin == "null" or not origin:
            return True  # local page, or the HTML opened straight from disk (file://)
        return False

    def _request_allowed(self) -> bool:
        """Blocks other websites from driving the API (e.g. closing disputes) through the user's browser."""
        origin = self.headers.get("Origin")
        if origin:
            return self._origin_allowed(origin)
        # No Origin header: plain tools (curl) and same-site navigations are fine;
        # cross-site no-cors requests (e.g. <img src=".../api/close_dispute?...">) are not.
        return self.headers.get("Sec-Fetch-Site", "") != "cross-site"

    def _reject_if_forbidden(self, path: str) -> bool:
        if path.startswith("/api/") and not self._request_allowed():
            self.send_response(403)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Origin not allowed"}).encode("utf-8"))
            return True
        return False

    def end_headers(self):
        origin = self.headers.get("Origin") if hasattr(self, "headers") and self.headers else None
        if origin and self._origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Vary", "Origin")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200 if self._request_allowed() else 403)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if self._reject_if_forbidden(parsed.path):
            return
        if parsed.path == "/api/version":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"version": SERVER_VERSION}).encode("utf-8"))
            return

        if parsed.path in ("", "/", "/index.html", "/calc", "/calculator"):
            self.send_response(302)
            self.send_header("Location", "/Chargeback%20Calculator.html")
            self.end_headers()
            return

        if parsed.path in ("/api/login", "/api/tista_login"):
            print("\n[API] Received Tista login request...")
            try:
                res = perform_tista_login()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        if parsed.path in ("/api/login_status", "/api/tista_login_status"):
            res = check_tista_login_status()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path in ("/api/fetch_disputes", "/api/batch_disputes"):
            qs = parse_qs(parsed.query)
            reason = qs.get("reason", ["2"])[0]
            status = qs.get("status", ["1"])[0]
            chargeable = qs.get("chargeable", ["1"])[0]
            location = qs.get("location", [""])[0]
            query = qs.get("query", [""])[0]
            date_range = qs.get("date_range", [""])[0] or qs.get("fault_at", [""])[0] or qs.get("fault_at_between", [""])[0]
            start_date = qs.get("start_date", [""])[0]
            end_date = qs.get("end_date", [""])[0]
            deep_scan = qs.get("deep_scan", ["1"])[0] in ("1", "true", "True")
            filters = {
                "reason": reason,
                "status": status,
                "chargeable": chargeable,
                "location": location,
                "query": query,
                "date_range": date_range,
                "start_date": start_date,
                "end_date": end_date,
                "deep_scan": deep_scan
            }
            print(f"\n[API] Received batch disputes request with filters: {filters}")
            try:
                result = fetch_disputes_batch_from_tista(filters, headless=DEFAULT_HEADLESS)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return
        if parsed.path in ("/api/fetch_files", "/api/fetch_correction"):
            qs = parse_qs(parsed.query)
            order_id = qs.get("order_id", [""])[0]
            if not order_id:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Missing order_id parameter"}).encode("utf-8"))
                return

            print(f"\n[API] Received files fetch request for Order #{order_id}")
            try:
                result = fetch_order_files_from_tista(order_id, headless=DEFAULT_HEADLESS)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        if parsed.path in ("/api/fetch_slides", "/api/parse_slides"):
            qs = parse_qs(parsed.query)
            slide_url = qs.get("url", [""])[0]
            if not slide_url:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Missing url parameter"}).encode("utf-8"))
                return
            res = parse_google_slides_presentation(slide_url)
            order_id = qs.get("order_id", [""])[0].strip()
            m_hash = re.search(r"#slide=id\.([a-zA-Z0-9_-]+)", slide_url)
            hash_slide_id = m_hash.group(1) if m_hash else None
            clean_oid = re.sub(r"\D", "", order_id) if order_id else ""
            if "jobs" in res and res["jobs"]:
                target = None
                if hash_slide_id:
                    target = next((j for j in res["jobs"] if j.get("slide_id") == hash_slide_id), None)
                if not target and clean_oid:
                    target = next((j for j in res["jobs"] if re.sub(r"\D", "", str(j.get("order_id", ""))) == clean_oid or clean_oid in str(j.get("order_id", ""))), None)
                if not target:
                    target = res["jobs"][0]
                res["target_job"] = target
            self.send_response(200 if "error" not in res else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/fetch":
            qs = parse_qs(parsed.query)
            order_id = qs.get("order_id", [""])[0]
            if not order_id:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Missing order_id parameter"}).encode("utf-8"))
                return

            print(f"\n[API] Received fetch request for Order #{order_id}")
            try:
                result = fetch_order_from_tista(order_id, headless=DEFAULT_HEADLESS)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        if parsed.path == "/api/fetch_link_files":
            qs = parse_qs(parsed.query)
            link_url = qs.get("url", [""])[0]
            if not link_url:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Missing url parameter"}).encode("utf-8"))
                return
            print(f"\n[API] Received fetch_link_files request for: {link_url}")
            try:
                files = extract_filenames_from_link_standalone(link_url, headless=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "url": link_url, "filenames": files, "count": len(files)}, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        if parsed.path == "/api/fetch_dispute_comment":
            qs = parse_qs(parsed.query)
            dispute_id = qs.get("dispute_id", [""])[0]
            if not dispute_id:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Missing dispute_id parameter"}).encode("utf-8"))
                return
            print(f"\n[API] Received fetch_dispute_comment request for Dispute #{dispute_id}")
            try:
                res = fetch_dispute_comment_and_files(dispute_id, headless=True, with_files=qs.get("files", ["1"])[0] != "0")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        if parsed.path == "/api/close_dispute":
            qs = parse_qs(parsed.query)
            order_id = qs.get("order_id", [""])[0]
            dispute_id = qs.get("dispute_id", [""])[0]
            amount = float(qs.get("amount", [2.0])[0] or 2.0)
            comment = qs.get("comment", [""])[0]
            fault_severity = qs.get("fault_severity", [""])[0] or None
            print(f"\n[API] Received close_dispute request: order_id={order_id}, dispute_id={dispute_id}, amount={amount}, fault_severity={fault_severity}")
            try:
                res = close_dispute_on_tista(dispute_id=dispute_id, order_id=order_id, amount=amount, comment=comment, fault_severity=fault_severity, headless=DEFAULT_HEADLESS)
                self.send_response(502 if res.get("error") else 200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        if parsed.path not in STATIC_FILES:
            self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Not found"}).encode("utf-8"))
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if self._reject_if_forbidden(parsed.path):
            return
        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else ""
        data = {}
        if post_body:
            try:
                data = json.loads(post_body)
            except Exception:
                pass

        if parsed.path in ("/api/import_cookies", "/api/sync_cookies"):
            cookies = data.get("cookies", [])
            if cookies:
                try:
                    for d in [USER_DATA_DIR, APP_DIR, Path("/data")]:
                        if d.exists():
                            with open(d / "saved_cookies.json", "w", encoding="utf-8") as f:
                                json.dump(cookies, f, indent=2)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success", "message": f"Successfully imported {len(cookies)} cookies"}).encode("utf-8"))
                except Exception as e:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            else:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "No cookies provided"}).encode("utf-8"))
            return

        if parsed.path in ("/api/login", "/api/tista_login"):
            try:
                res = perform_tista_login(email=data.get("email"), password=data.get("password"))
                self.send_response(200 if res.get("status") == "success" else 400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        if parsed.path in ("/api/login_status", "/api/tista_login_status"):
            res = check_tista_login_status()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path in ("/api/fetch_slides", "/api/parse_slides"):
            slide_url = data.get("url", "")
            if not slide_url:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Missing url in body"}).encode("utf-8"))
                return
            res = parse_google_slides_presentation(slide_url)
            order_id = str(data.get("order_id", "")).strip()
            m_hash = re.search(r"#slide=id\.([a-zA-Z0-9_-]+)", slide_url)
            hash_slide_id = m_hash.group(1) if m_hash else None
            clean_oid = re.sub(r"\D", "", order_id) if order_id else ""
            if "jobs" in res and res["jobs"]:
                target = None
                if hash_slide_id:
                    target = next((j for j in res["jobs"] if j.get("slide_id") == hash_slide_id), None)
                if not target and clean_oid:
                    target = next((j for j in res["jobs"] if re.sub(r"\D", "", str(j.get("order_id", ""))) == clean_oid or clean_oid in str(j.get("order_id", ""))), None)
                if not target:
                    target = res["jobs"][0]
                res["target_job"] = target
            self.send_response(200 if "error" not in res else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            return

        if parsed.path == "/api/close_dispute":
            order_id = data.get("order_id", "")
            dispute_id = data.get("dispute_id", "")
            amount = float(data.get("amount", 2.0) or 2.0)
            comment = data.get("comment", "")
            fault_severity = data.get("fault_severity") or None
            print(f"\n[API POST] Close dispute: order_id={order_id}, dispute_id={dispute_id}, amount={amount}, fault_severity={fault_severity}")
            try:
                res = close_dispute_on_tista(dispute_id=dispute_id, order_id=order_id, amount=amount, comment=comment, fault_severity=fault_severity, headless=DEFAULT_HEADLESS)
                self.send_response(502 if res.get("error") else 200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        if parsed.path == "/api/batch_close_disputes":
            items = data.get("disputes", [])
            print(f"\n[API POST] Batch close disputes: {len(items)} items")
            try:
                res = batch_close_disputes_on_tista(items, headless=DEFAULT_HEADLESS)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        if parsed.path == "/api/fetch_link_files":
            link_url = data.get("url", "")
            if not link_url:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Missing url in body"}).encode("utf-8"))
                return
            print(f"\n[API POST] Received fetch_link_files request for: {link_url}")
            try:
                files = extract_filenames_from_link_standalone(link_url, headless=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "url": link_url, "filenames": files, "count": len(files)}, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        if parsed.path == "/api/fetch_dispute_comment":
            dispute_id = data.get("dispute_id", "")
            if not dispute_id:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Missing dispute_id in body"}).encode("utf-8"))
                return
            print(f"\n[API POST] Received fetch_dispute_comment request for Dispute #{dispute_id}")
            try:
                res = fetch_dispute_comment_and_files(dispute_id, headless=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        self.send_response(404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps({"error": "Endpoint not found"}).encode("utf-8"))

def get_local_ip():
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def start_server(port=None, host=None):
    if port is None:
        port = int(os.environ.get("PORT", 8765))
    if host is None:
        host = "0.0.0.0" if (IS_CLOUD_ENV or "PORT" in os.environ) else "127.0.0.1"
    server_address = (host, port)
    serve_dir = BUNDLE_DIR if (BUNDLE_DIR / "Chargeback Calculator.html").exists() else APP_DIR
    os.chdir(serve_dir)
    httpd = ThreadingHTTPServer(server_address, TistaRequestHandler)
    local_ip = get_local_ip()
    print(f"\n========================================================")
    print(f"  Tista Chargeback Calculator Server is RUNNING!")
    print(f"--------------------------------------------------------")
    print(f"  Port:                {port}")
    print(f"  This PC (Local):     http://localhost:{port}/")
    print(f"  Other Team Members:  http://{local_ip}:{port}/")
    print(f"  Headless Mode:       {DEFAULT_HEADLESS}")
    print(f"  Data Directory:      {USER_DATA_DIR}")
    print(f"  Press Ctrl+C to stop the server.")
    print(f"========================================================\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        httpd.server_close()

def main():
    port = int(os.environ.get("PORT", 8765))
    if len(sys.argv) > 1 and sys.argv[1] in ("--serve", "--server", "-s"):
        start_server(port=port)
        return

    # If run in GUI/frozen mode or no interactive terminal attached, default to start_server
    is_interactive = False
    try:
        if sys.stdin and hasattr(sys.stdin, 'isatty') and sys.stdin.isatty():
            is_interactive = True
    except Exception:
        is_interactive = False

    if getattr(sys, 'frozen', False) or not is_interactive:
        start_server(port=port)
        return

    is_files_mode = any(arg in ("--files", "-f", "--correction", "-c") for arg in sys.argv)
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    order_id = args[0] if args else ""

    if not order_id:
        try:
            order_id = input("Enter Tista Order ID: ").strip()
        except (KeyboardInterrupt, EOFError, RuntimeError, Exception):
            # If input fails for any reason (e.g. sys.stdin closed), start the server
            start_server(port=port)
            return

    if not order_id:
        print("Error: Order ID is required.")
        sys.exit(1)

    if is_files_mode:
        print(f"\nFetching files for Order #{order_id}...")
        res = fetch_order_files_from_tista(order_id, headless=True)
        print(f"\n[✓] Fetched {res.get('total_files', 0)} files for Order #{res.get('order_id')}")
        print(f"Contractor: {res.get('contractor')}")
        return

    print(f"\nFetching data for Order #{order_id}...")
    res = fetch_order_from_tista(order_id, headless=True)
    print("\n--- Fetched Summary ---")
    print(f"Order ID:                 {res.get('order_id')}")
    print(f"Contractor Name:          {res.get('contractor') or res.get('dispute_location')}")
    print(f"Service Type:             {res.get('service_type')}")
    print(f"Average Category:         {res.get('average_category')} -> Level: {res.get('category_level')}")
    print(f"Pixel Range:              {res.get('pixel')}")
    print(f"Quantity of Service:      {res.get('fault_quantity')}")
    print(f"Order Duration:           {res.get('order_duration_text')} ({res.get('order_duration_seconds')}s)")
    print(f"Delayed by:               {res.get('delayed_by_text')} -> {res.get('delay_seconds')}s")
    if res.get("warnings"):
        print("\nWarnings / Notes:")
        for w in res["warnings"]:
            print(f" - {w}")

if __name__ == "__main__":
    main()
