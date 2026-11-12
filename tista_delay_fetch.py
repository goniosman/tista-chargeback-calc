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
import time
import urllib.request
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote
from playwright.sync_api import sync_playwright

BASE_URL = "https://tista.app"
DISPUTES_URL = f"{BASE_URL}/manage/vendors/1/order_disputes"
NAVBAR_SEARCH_XPATH = '//*[@id="navbar"]/form/div[1]/input'
APP_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
LOCAL_PROFILE = APP_DIR / ".tista-profile"

# Allow custom profile directory via environment variable (e.g. Render persistent disk)
CUSTOM_DATA_DIR = os.environ.get("TISTA_DATA_DIR")
if CUSTOM_DATA_DIR:
    USER_DATA_DIR = Path(CUSTOM_DATA_DIR)
elif LOCAL_PROFILE.exists():
    USER_DATA_DIR = LOCAL_PROFILE
else:
    USER_DATA_DIR = Path.home() / ".tista-profile"

# Cloud / Headless auto-detection (True on Render or when HEADLESS is set)
IS_CLOUD_ENV = bool(os.environ.get("RENDER") or os.environ.get("HEADLESS", "").lower() in ("1", "true", "yes"))
DEFAULT_HEADLESS = True if IS_CLOUD_ENV else False

KNOWN_SERVICES = [
    "Clipping path", "Multi-clipping path", "Image masking", "Drop shadow",
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

def ensure_logged_in(page):
    """Ensures user is logged into Tista. If on login page, attempts auto-login with env credentials or waits for user."""
    page.goto(BASE_URL)
    page.wait_for_load_state("domcontentloaded")
    time.sleep(1)

    if "sign_in" in page.url or page.locator('input[type="password"]').count() > 0:
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
                    page.wait_for_load_state("networkidle", timeout=15000)
                    time.sleep(2)
            except Exception as e:
                print(f"[!] Auto-login exception: {e}")

        if "sign_in" in page.url or page.locator('input[type="password"]').count() > 0:
            print("\n" + "=" * 60)
            print("[!] TISTA LOGIN REQUIRED:")
            print("    Please log in to Tista in the browser window.")
            print("    Your session will be saved automatically for future runs.")
            print("    (On headless/cloud servers like Render, set TISTA_EMAIL and TISTA_PASSWORD env vars)")
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
            while "sign_in" in page.url or page.locator('input[type="password"]').count() > 0:
                time.sleep(1)
                if time.time() - start_time > 300:
                    raise TimeoutError("Login timed out after 5 minutes.")

        print("[✓] Login successful! Saved to profile.\n")
        page.wait_for_load_state("networkidle")
        time.sleep(1)

def fetch_order_from_tista(order_id: str, headless: bool = False):
    """
    Automates Tista login, dispute extraction, and order file parsing using Playwright.
    """
    raw_id = str(order_id).strip()
    digits = re.sub(r"\D", "", raw_id)
    search_with_hash = f"#{digits}" if digits else raw_id
    oid = digits or raw_id.replace("#", "")

    if not oid:
        return {"error": "Invalid Order ID"}

    profile_dir = USER_DATA_DIR
    out = {
        "order_id": oid,
        "dispute_id": "",
        "dispute_reason": "Delay",
        "contractor": "",
        "dispute_location": "",
        "fault_by": "",
        "delayed_by_text": "",
        "delayed_by_seconds": None,
        "delay_seconds": None,
        "fault_quantity": None,
        "fault_severity": "Low",
        "status": "",
        "order_duration_text": "",
        "order_duration_seconds": None,
        "pixel": "> 5000",
        "service_type": "",
        "category_level": "",
        "average_category": None,
        "services": [],
        "warnings": []
    }

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            viewport={"width": 1400, "height": 900}
        )
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

            # Check Dispute Edit Page for comment & delay link
            if out.get("dispute_id"):
                try:
                    did = out["dispute_id"]
                    print(f"[*] Checking dispute #{did} edit page for comment & link...")
                    edit_page = ctx.new_page()
                    edit_url = f"{BASE_URL}/manage/vendors/1/order_disputes/{did}/edit"
                    edit_page.goto(edit_url, wait_until="domcontentloaded", timeout=20000)
                    time.sleep(1.5)
                    c_el = edit_page.locator("#order_dispute_comment")
                    if c_el.count() > 0:
                        c_val = c_el.input_value().strip()
                        if c_val:
                            out["comment"] = c_val
                            print(f"[✓] Extracted dispute comment: {c_val}")
                            urls = [u.rstrip(".,;!?')\"") for u in re.findall(r"https?://[^\s<>\x22\x27]+", c_val)]
                            if urls:
                                link = urls[0]
                                out["comment_link"] = link
                                print(f"[*] Found link in dispute comment: {link}")
                                files = extract_filenames_from_page(edit_page, link)
                                if files:
                                    out["filenames"] = files
                                    out["filenames_count"] = len(files)
                                    print(f"[✓] Extracted {len(files)} files from dispute comment link!")
                                    if str(out.get("dispute_reason", "")).lower() == "delay":
                                        out["fault_quantity"] = float(len(files))
                                        print(f"[✓] Updated fault_quantity to {len(files)} from delay link!")
                    edit_page.close()
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
                    for ks in KNOWN_SERVICES:
                        if ks.lower() in txt.lower():
                            current_svc = ks
                            break
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

    output_file = Path(__file__).with_name(f"tista_{oid}.json")
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
                for ks in KNOWN_SERVICES:
                    if ks.lower() in s_line.lower():
                        matched_svc = ks
                        break
                if matched_svc:
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
    return parsed

def fetch_order_files_from_tista(order_id: str, headless: bool = False):
    """
    Fetches the list of files, services, categories, and location for an order to calculate Correction chargebacks.
    """
    raw_id = str(order_id).strip()
    digits = re.sub(r"\D", "", raw_id)
    search_with_hash = f"#{digits}" if digits else raw_id
    oid = digits or raw_id.replace("#", "")

    result = {
        "order_id": oid,
        "contractor": "",
        "pixel": "> 5000",
        "total_files": 0,
        "files": [],
        "warnings": []
    }

    profile_dir = USER_DATA_DIR

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile_dir), headless=headless)
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
            svcs = f.get("services") or []
            clean_svcs = []
            for s in svcs:
                s_name = s.get("name") or s.get("group") or ""
                matched_svc = ""
                for ks in KNOWN_SERVICES:
                    if ks.lower() == s_name.lower() or ks.lower() in s_name.lower():
                        matched_svc = ks
                        break
                if not matched_svc:
                    matched_svc = s_name or "Clipping path"
                
                comp = s.get("complexity") or ""
                cat_m = re.search(r"(\d+(?:\.\d+)?)", comp)
                num_cat = float(cat_m[1]) if cat_m else 1.0
                cat_label = f"Category {cat_m[1]}" if cat_m else "Category 1"
                clean_svcs.append({
                    "service": matched_svc,
                    "raw_name": s_name,
                    "complexity": comp or f"C{num_cat}",
                    "category": cat_label,
                    "numeric_cat": num_cat
                })
            
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

    out_file = Path(__file__).with_name(f"tista_correction_{oid}.json")
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

def fetch_disputes_batch_from_tista(filters: dict = None, headless: bool = False):
    """
    Fetches list of disputes based on filters (Dispute reason, Status, Chargeable to, Location, Order ID),
    and optionally deep-scans order files to extract service and category.
    """
    filters = filters or {}
    reason = str(filters.get("reason", "2") or "")
    status = str(filters.get("status", "1") or "")
    chargeable = str(filters.get("chargeable", "1") or "")
    location = str(filters.get("location", "") or "")
    query = str(filters.get("query") or "").strip()
    deep_scan = bool(filters.get("deep_scan", False))

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
    print(f"\n[*] Navigating to disputes search: {url}")

    profile_dir = USER_DATA_DIR
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile_dir), headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)

        if single_clean_id:
            trs = page.locator("table tbody tr")
            if trs.count() == 0 or (trs.count() == 1 and "no data" in trs.first.inner_text().lower()):
                alt_val = single_clean_id if single_is_hash else f"%23{single_clean_id}"
                alt_params = [p for p in params if not p.startswith("q[id_or_fulfillment_order_order_name_eq]=")]
                alt_params.append(f"q[id_or_fulfillment_order_order_name_eq]={alt_val}")
                alt_url = f"{DISPUTES_URL}?{'&'.join(alt_params)}"
                print(f"[*] Trying alternate query URL: {alt_url}")
                page.goto(alt_url)
                page.wait_for_load_state("domcontentloaded")
                time.sleep(1.5)

            trs = page.locator("table tbody tr")
            if trs.count() == 0 or (trs.count() == 1 and "no data" in trs.first.inner_text().lower()):
                for cand_val in [f"%23{single_clean_id}", single_clean_id]:
                    broad_url = f"{DISPUTES_URL}?q[id_or_fulfillment_order_order_name_eq]={cand_val}"
                    print(f"[*] Trying broad dispute search for '{cand_val}': {broad_url}")
                    page.goto(broad_url)
                    page.wait_for_load_state("domcontentloaded")
                    time.sleep(1.5)
                    trs_broad = page.locator("table tbody tr")
                    if trs_broad.count() > 0 and "no data" not in trs_broad.first.inner_text().lower():
                        break

        rows = page.evaluate("""() => {
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
        }""")

        if multiple_ids:
            target_set = set(multiple_ids)
            rows = [r for r in rows if (re.sub(r"\D", "", r.get("order_id", "")) in target_set) or (re.sub(r"\D", "", r.get("dispute_id", "")) in target_set)]

        print(f"[*] Extracted {len(rows)} dispute records.")
        out_list = []

        for r in rows:
            raw_oid = re.sub(r"\D", "", r["order_id"])
            qty = int(re.sub(r"\D", "", r["fault_quantity"]) or 1)
            reason_name = r["dispute_reason"] or "Correction"

            cached_corr = Path(__file__).with_name(f"tista_correction_{raw_oid}.json")
            cached_delay = Path(__file__).with_name(f"tista_{raw_oid}.json")

            detected_svc = ""
            detected_cat = ""
            detected_dur = "24:00:00"
            detected_pix = "> 5000"

            order_svcs = []
            order_cats = []

            if cached_corr.exists():
                try:
                    cdata = json.loads(cached_corr.read_text(encoding="utf-8"))
                    for f0 in cdata.get("files", []):
                        for s0 in f0.get("services", []):
                            s_name = s0.get("service")
                            c_name = s0.get("category")
                            if s_name and s_name not in order_svcs:
                                order_svcs.append(s_name)
                            if c_name and c_name not in order_cats:
                                order_cats.append(c_name)
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

            if deep_scan and not detected_svc and raw_oid:
                try:
                    page.goto(f"{BASE_URL}/orders?location=all&dateRange=any&query={raw_oid}")
                    page.wait_for_load_state("domcontentloaded")
                    time.sleep(1.5)
                    internal_id = page.evaluate("""() => {
                        const row = document.querySelector('tr[id^="order-"]');
                        return row ? row.id.replace('order-', '') : null;
                    }""")
                    if internal_id:
                        page.goto(f"{BASE_URL}/orders/{internal_id}/files")
                        page.wait_for_load_state("domcontentloaded")
                        time.sleep(1.5)
                        tbl_btn = page.locator('button[data-order-files-layout-param="table"]').first
                        if tbl_btn.count() > 0:
                            tbl_btn.click()
                            time.sleep(1)
                        sinfo = page.evaluate("""() => {
                            const sEls = [...document.querySelectorAll('.services-block .service')];
                            const list = [];
                            sEls.forEach(sEl => {
                                const title = sEl.querySelector('.service-title') ? sEl.querySelector('.service-title').innerText.trim() : '';
                                const subEl = sEl.querySelector('.service-category-title');
                                const subTitle = subEl ? subEl.innerText.trim() : '';
                                const compEl = sEl.querySelector('.complexity');
                                const comp = compEl ? compEl.innerText.trim() : '';
                                list.push({ name: subTitle || title, comp });
                            });
                            return list;
                        }""")
                        if sinfo:
                            for si in sinfo:
                                nm = si.get("name")
                                if nm and nm not in order_svcs:
                                    order_svcs.append(nm)
                                c_m = re.search(r"(\d+(?:\.\d+)?)", si.get("comp", ""))
                                c_str = f"Category {c_m[1]}" if c_m else ""
                                if c_str and c_str not in order_cats:
                                    order_cats.append(c_str)
                            if order_svcs:
                                detected_svc = order_svcs[0]
                            if order_cats:
                                detected_cat = order_cats[0]
                except Exception as ex:
                    print(f"Error deep scanning #{raw_oid}: {ex}")

            if not detected_svc:
                detected_svc = "Clipping path"
            if not detected_cat:
                detected_cat = "Category 1.5"

            delay_sec = hours_to_seconds(r["delayed_by"]) if reason_name.lower() == "delay" else 0

            disp_comment = ""
            disp_link = ""
            disp_files = []
            if r.get("dispute_id") and (reason_name.lower() == "delay" or deep_scan):
                try:
                    edit_page = ctx.new_page()
                    edit_url = f"{BASE_URL}/manage/vendors/1/order_disputes/{r['dispute_id']}/edit"
                    edit_page.goto(edit_url, wait_until="domcontentloaded", timeout=20000)
                    time.sleep(1.2)
                    c_el = edit_page.locator("#order_dispute_comment")
                    if c_el.count() > 0:
                        c_val = c_el.input_value().strip()
                        if c_val:
                            disp_comment = c_val
                            urls = [u.rstrip(".,;!?')\"") for u in re.findall(r"https?://[^\s<>\x22\x27]+", c_val)]
                            if urls:
                                disp_link = urls[0]
                                disp_files = extract_filenames_from_page(edit_page, disp_link)
                                if disp_files and reason_name.lower() == "delay":
                                    qty = len(disp_files)
                    edit_page.close()
                except Exception as bce:
                    print(f"Batch dispute #{r.get('dispute_id')} edit check note: {bce}")

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
                "fault_severity": r["fault_severity"],
                "status": r["status"],
                "service": detected_svc,
                "cat": detected_cat,
                "pix": detected_pix,
                "order_services": order_svcs,
                "order_cats": order_cats,
                "comment": disp_comment,
                "comment_link": disp_link,
                "filenames": disp_files,
                "filenames_count": len(disp_files)
            })

        ctx.close()

    res = {
        "filters": filters,
        "total_disputes": len(out_list),
        "disputes": out_list
    }
    out_file = Path(__file__).with_name("tista_batch_disputes.json")
    out_file.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[✓] Saved {len(out_list)} disputes to: {out_file}")
    return res

def extract_filenames_from_page(page, url):
    """Navigates to a file storage or transfer URL (Dropbox, etc.) and extracts all filenames."""
    if not url: return []
    clean_url = str(url).strip().rstrip(".,;")
    print(f"[*] Extracting filenames from link: {clean_url}")
    try:
        page.goto(clean_url, wait_until="domcontentloaded", timeout=25000)
        time.sleep(2.5)
        files = page.evaluate("""() => {
            const elements = document.querySelectorAll('*');
            const filenames = [];
            for (const el of elements) {
                const txt = el.innerText ? el.innerText.trim() : '';
                if (/\\.(?:jpg|jpeg|png|psd|tif|tiff|cr2|raw|dng|ai|eps|pdf)$/i.test(txt) && txt.length < 150 && !txt.includes('\\n')) {
                    filenames.push(txt);
                }
            }
            return [...new Set(filenames)];
        }""")
        return files
    except Exception as e:
        print(f"[!] Error extracting filenames from {clean_url}: {e}")
        return []

def extract_filenames_from_link_standalone(url: str, headless: bool = True):
    """Launches Playwright headless and extracts filenames from any storage/transfer link."""
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(USER_DATA_DIR), headless=headless)
        page = ctx.new_page()
        files = extract_filenames_from_page(page, url)
        ctx.close()
        return files

def fetch_dispute_comment_and_files(dispute_id: str, headless: bool = True):
    """
    Visits the dispute edit page on Tista, extracts comment and any storage/transfer link,
    and extracts all filenames from that link.
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

    edit_url = f"{BASE_URL}/manage/vendors/1/order_disputes/{clean_did}/edit"
    print(f"[*] Fetching dispute edit page: {edit_url}")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(USER_DATA_DIR), headless=headless)
        page = ctx.new_page()
        try:
            page.goto(edit_url, wait_until="domcontentloaded", timeout=25000)
            time.sleep(1.5)
            comment_el = page.locator("#order_dispute_comment")
            if comment_el.count() > 0:
                comment_val = comment_el.input_value().strip()
                result["comment"] = comment_val
                print(f"[✓] Extracted comment: {comment_val}")
                urls = [u.rstrip(".,;!?')\"") for u in re.findall(r"https?://[^\s<>\x22\x27]+", comment_val)]
                if urls:
                    link = urls[0]
                    result["comment_link"] = link
                    print(f"[*] Found link in comment: {link}")
                    files = extract_filenames_from_page(page, link)
                    result["filenames"] = files
                    result["count"] = len(files)
                    print(f"[✓] Extracted {len(files)} files from dispute comment link!")
        except Exception as e:
            print(f"[!] Error fetching dispute comment: {e}")
            result["error"] = str(e)
        finally:
            ctx.close()

    return result

def close_dispute_on_tista(dispute_id: str = None, order_id: str = None, amount: float = 2.0, comment: str = None, headless: bool = False):
    """
    Charges the specified amount to vendor and closes the dispute on Tista.
    """
    clean_dispute_id = str(dispute_id or "").strip()
    raw_oid = re.sub(r"\D", "", str(order_id or ""))
    charge_amt = round(max(2.0, float(amount or 2.0)), 2)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=headless,
            slow_mo=100
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Step 1: Resolve dispute_id if missing
        if not clean_dispute_id and raw_oid:
            print(f"[*] Resolving dispute_id for order #{raw_oid}...")
            # Try search with #
            page.goto(f"{BASE_URL}/manage/vendors/1/order_disputes?q[id_or_fulfillment_order_order_name_eq]={quote('#' + raw_oid)}")
            page.wait_for_load_state("domcontentloaded")
            time.sleep(1.5)
            row = page.locator("tbody tr").first
            if row.count() > 0:
                first_cell = row.locator("td").first
                clean_dispute_id = first_cell.inner_text().strip()
            
            if not clean_dispute_id:
                # Try search without #
                page.goto(f"{BASE_URL}/manage/vendors/1/order_disputes?q[id_or_fulfillment_order_order_name_eq]={quote(raw_oid)}")
                page.wait_for_load_state("domcontentloaded")
                time.sleep(1.5)
                row = page.locator("tbody tr").first
                if row.count() > 0:
                    first_cell = row.locator("td").first
                    clean_dispute_id = first_cell.inner_text().strip()

        if not clean_dispute_id:
            ctx.close()
            return {"error": f"Could not find dispute ID for Order #{raw_oid or order_id}"}

        print(f"[*] Opening dispute edit page for Dispute #{clean_dispute_id}...")
        edit_url = f"{BASE_URL}/manage/vendors/1/order_disputes/{clean_dispute_id}/edit"
        page.goto(edit_url)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(1.5)

        # Check if form exists
        charge_input = page.locator("#order_dispute_charge_to_vendor")
        if charge_input.count() == 0:
            ctx.close()
            return {"error": f"Dispute #{clean_dispute_id} edit form not found or cannot be edited."}

        # Select Chargeable to: Vendor
        try:
            page.select_option("#order_dispute_chargeable_to", "vendor")
        except Exception as e:
            print(f"Warning setting chargeable_to: {e}")

        # Set Charge to Vendor amount
        charge_input.fill(f"{charge_amt:.2f}")

        # Select Status: Closed
        try:
            page.select_option("#order_dispute_status", "closed")
        except Exception as e:
            print(f"Warning setting status: {e}")

        # If comment provided
        if comment:
            try:
                cur_comment = page.locator("#order_dispute_comment").input_value()
                page.locator("#order_dispute_comment").fill((cur_comment + "\n" + comment).strip())
            except Exception:
                pass

        print(f"[*] Submitting update: Charge ${charge_amt:.2f} to vendor, Status: Closed...")
        # Submit the form
        page.locator('input[value="Update Order dispute"]').click()
        time.sleep(2)

        # Handle confirmation modal if any
        try:
            confirm_btn = page.locator("#turbo-confirmation-modal__confirm")
            if confirm_btn.count() > 0 and confirm_btn.is_visible():
                confirm_btn.click()
                time.sleep(2)
        except Exception:
            pass

        page.wait_for_load_state("domcontentloaded")
        time.sleep(1)

        ctx.close()
        return {
            "success": True,
            "dispute_id": clean_dispute_id,
            "order_id": order_id or f"#{raw_oid}",
            "amount": charge_amt,
            "status": "Closed",
            "message": f"Successfully charged ${charge_amt:.2f} to Vendor and closed dispute #{clean_dispute_id}."
        }

def batch_close_disputes_on_tista(items: list, headless: bool = False):
    """
    Charges to vendor and closes multiple disputes in a single browser session.
    """
    results = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=headless,
            slow_mo=50
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        for it in items:
            did = str(it.get("dispute_id") or "").strip()
            oid = str(it.get("order_id") or "").strip()
            raw_oid = re.sub(r"\D", "", oid)
            amount = round(max(2.0, float(it.get("amount") or 2.0)), 2)

            if not did and raw_oid:
                try:
                    page.goto(f"{BASE_URL}/manage/vendors/1/order_disputes?q[id_or_fulfillment_order_order_name_eq]={quote('#' + raw_oid)}")
                    page.wait_for_load_state("domcontentloaded")
                    time.sleep(1)
                    row = page.locator("tbody tr").first
                    if row.count() > 0:
                        did = row.locator("td").first.inner_text().strip()
                except Exception:
                    pass

            if not did:
                results.append({"order_id": oid, "error": "Dispute ID not found", "success": False})
                continue

            try:
                page.goto(f"{BASE_URL}/manage/vendors/1/order_disputes/{did}/edit")
                page.wait_for_load_state("domcontentloaded")
                time.sleep(1)

                charge_input = page.locator("#order_dispute_charge_to_vendor")
                if charge_input.count() == 0:
                    results.append({"dispute_id": did, "order_id": oid, "error": "Edit form not found", "success": False})
                    continue

                page.select_option("#order_dispute_chargeable_to", "vendor")
                charge_input.fill(f"{amount:.2f}")
                page.select_option("#order_dispute_status", "closed")

                page.locator('input[value="Update Order dispute"]').click()
                time.sleep(1.5)

                try:
                    confirm_btn = page.locator("#turbo-confirmation-modal__confirm")
                    if confirm_btn.count() > 0 and confirm_btn.is_visible():
                        confirm_btn.click()
                        time.sleep(1.5)
                except Exception:
                    pass

                results.append({
                    "dispute_id": did,
                    "order_id": oid,
                    "amount": amount,
                    "status": "Closed",
                    "success": True
                })
                print(f"[✓] Closed dispute #{did} (Order {oid}) with ${amount:.2f}")
            except Exception as ex:
                results.append({"dispute_id": did, "order_id": oid, "error": str(ex), "success": False})

        ctx.close()

    success_count = sum(1 for r in results if r.get("success"))
    return {
        "total": len(items),
        "closed": success_count,
        "results": results
    }

def check_tista_login_status():
    """Checks if Tista profile exists and has saved session cookies/storage."""
    profile_dir = USER_DATA_DIR
    if not profile_dir.exists():
        return {"logged_in": False, "reason": "No profile directory"}
    has_default = (profile_dir / "Default").exists() or (profile_dir / "Cookies").exists()
    return {"logged_in": has_default}

def perform_tista_login():
    """Opens a visible browser for user to log in and saves session permanently."""
    print("\n" + "=" * 60)
    print(" [*] Opening Tista Login Window in browser...")
    print(" Please enter your email and password, check 'Remember me', and click 'Sign in'.")
    print("=" * 60 + "\n")

    profile_dir = USER_DATA_DIR
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            viewport={"width": 1360, "height": 880}
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://tista.app/users/sign_in")

        try:
            rem = page.locator('#user_remember_me, input[name*="remember_me"]').first
            if rem.count() > 0:
                rem.check()
        except Exception:
            pass

        # Wait until user signs in (URL leaves sign_in or no password box)
        start_t = time.time()
        while time.time() - start_t < 180:
            if "sign_in" not in page.url and page.locator('input[type="password"]').count() == 0:
                break
            time.sleep(1)

        time.sleep(2)
        ctx.close()

    print("[✓] Login flow completed. Session saved to profile.\n")
    return {"status": "success", "message": "Login completed successfully"}

def clean_html_block(html):
    t = re.sub(r'<br\s*/?>', '\n', html, flags=re.I)
    t = re.sub(r'</?(?:p|div|tr|td|table|section|li|h[1-6])\b[^>]*>', '\n', t, flags=re.I)
    t = re.sub(r'<[^>]+>', ' ', t)
    t = t.replace('&nbsp;', ' ').replace('&gt;', '>').replace('&lt;', '<').replace('&amp;', '&').replace('&#8203;', '')
    return t

def clean_file_names_list(raw_list):
    res = []
    seen = set()
    for item in raw_list:
        parts = re.split(r'[\r\n\x0b\u2028\u2029\ufffd]+', str(item))
        for p in parts:
            cleaned = re.sub(r'^[^\w#]+', '', p).strip()
            cleaned = cleaned.strip()
            if len(cleaned) >= 2 and cleaned not in seen and not cleaned.lower().startswith("ul.") and not cleaned.lower().startswith("http"):
                seen.add(cleaned)
                res.append(cleaned)
    return res

def parse_google_slides_presentation(slide_url: str):
    """
    Fetches and parses a Google Slides presentation URL to extract:
    order_id, contractor, service, severity, filenames, is_all_images, total_qty, fault_qty
    Supports both Quality Audit Reports and Correction Slides.
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
    
    if sections:
        for s_idx, sec in enumerate(sections):
            slide_text = clean_html_block(sec)
            
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

            # Service
            service = ""
            m_svc_line = re.search(r"Service\s*[:#-]+\s*([A-Za-z0-9 ,/&+-]+)", slide_text, re.I)
            if m_svc_line:
                val = m_svc_line.group(1).strip()
                if val and not any(val.lower().startswith(x) for x in ["format", "instruction", "image", "note"]):
                    service = val
            
            if not service:
                for ks in KNOWN_SERVICES:
                    if re.search(r"\b" + re.escape(ks) + r"\b", slide_text, re.I):
                        service = ks
                        break
            
            if not service:
                service = "Clipping path"

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
                                after = re.sub(r'^.*?image\s*name\s*[:#-]*\s*', '', l, flags=re.I).strip()
                                after = after.lstrip("•-* \t").strip()
                                if after and not after.lower().startswith("ul.") and len(after) > 2:
                                    if after.lower() in ("all image", "all images"):
                                        is_all_images = True
                                    else:
                                        file_list.append(after)
                                started = True
                                continue
                            if started:
                                cl = l.lstrip("•-* \t").strip()
                                if cl.lower().startswith("ul.") or "{" in cl or "list-style" in cl:
                                    continue
                                if cl.lower() in ("original image", "completed image", "fault area", "none"):
                                    break
                                if cl.lower() in ("all image", "all images"):
                                    is_all_images = True
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
                "order_id": order_id,
                "contractor": contractor,
                "service": service,
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
                    svc_m = re.search(r"Service:\s*([^\n\r]+)", c, re.I)
                    service = svc_m.group(1).strip() if svc_m else "Clipping path"
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
                        "order_id": order_id,
                        "contractor": contractor,
                        "service": service,
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

    return {
        "status": "success",
        "presentation_id": pres_id,
        "total_jobs": len(parsed_jobs),
        "jobs": parsed_jobs
    }

# ---------- Web API & Local Server Support ----------
class TistaRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
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
            deep_scan = qs.get("deep_scan", ["0"])[0] in ("1", "true", "True")
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

        if parsed.path == "/api/close_dispute":
            qs = parse_qs(parsed.query)
            order_id = qs.get("order_id", [""])[0]
            dispute_id = qs.get("dispute_id", [""])[0]
            amount = float(qs.get("amount", [2.0])[0] or 2.0)
            comment = qs.get("comment", [""])[0]
            print(f"\n[API] Received close_dispute request: order_id={order_id}, dispute_id={dispute_id}, amount={amount}")
            try:
                res = close_dispute_on_tista(dispute_id=dispute_id, order_id=order_id, amount=amount, comment=comment, headless=DEFAULT_HEADLESS)
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

        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else ""
        data = {}
        if post_body:
            try:
                data = json.loads(post_body)
            except Exception:
                pass

        if parsed.path in ("/api/login", "/api/tista_login"):
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

        if parsed.path in ("/api/fetch_slides", "/api/parse_slides"):
            slide_url = data.get("url", "")
            if not slide_url:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Missing url in body"}).encode("utf-8"))
                return
            res = parse_google_slides_presentation(slide_url)
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
            print(f"\n[API POST] Close dispute: order_id={order_id}, dispute_id={dispute_id}, amount={amount}")
            try:
                res = close_dispute_on_tista(dispute_id=dispute_id, order_id=order_id, amount=amount, comment=comment, headless=DEFAULT_HEADLESS)
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

def start_server(port=None, host="0.0.0.0"):
    if port is None:
        port = int(os.environ.get("PORT", 8765))
    server_address = (host, port)
    os.chdir(APP_DIR)
    httpd = HTTPServer(server_address, TistaRequestHandler)
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
    if getattr(sys, 'frozen', False) or not sys.stdin or not hasattr(sys.stdin, 'isatty') or not sys.stdin.isatty():
        start_server(port=port)
        return

    is_files_mode = any(arg in ("--files", "-f", "--correction", "-c") for arg in sys.argv)
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    order_id = args[0] if args else ""

    if not order_id:
        try:
            order_id = input("Enter Tista Order ID: ").strip()
        except (KeyboardInterrupt, EOFError, RuntimeError, Exception):
            sys.exit(0)

    if not order_id:
        print("Error: Order ID is required.")
        sys.exit(1)

    if is_files_mode:
        print(f"\nFetching files for Order #{order_id}...")
        res = fetch_order_files_from_tista(order_id, headless=False)
        print(f"\n[✓] Fetched {res.get('total_files', 0)} files for Order #{res.get('order_id')}")
        print(f"Contractor: {res.get('contractor')}")
        return

    print(f"\nFetching data for Order #{order_id}...")
    res = fetch_order_from_tista(order_id, headless=False)
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
