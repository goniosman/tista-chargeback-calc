#!/usr/bin/env python3
"""
Tista One-Time Login Helper
Opens Google Chrome, allows you to log in, and permanently saves your login session.
Follows unified_app architecture.
"""
import sys
import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

APP_DIR = Path(__file__).parent
os.chdir(APP_DIR)
sys.path.insert(0, str(APP_DIR))

try:
    from tista_delay_fetch import resolve_profile_dir, launch_playwright_context, DISPUTES_URL
except ImportError:
    def resolve_profile_dir():
        return APP_DIR / "chrome-profile"
    def launch_playwright_context(p, headless=False):
        prof = resolve_profile_dir()
        prof.mkdir(parents=True, exist_ok=True)
        return p.chromium.launch_persistent_context(
            user_data_dir=str(prof),
            channel="chrome",
            headless=headless,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"]
        )
    DISPUTES_URL = "https://tista.app/manage/vendors/1/order_disputes"

def main():
    profile = resolve_profile_dir()
    print("=" * 60)
    print("         Tista Chrome Login Helper (unified_app style)")
    print("=" * 60)
    print(f" [*] Chrome Profile Directory: {profile}")
    print(" [*] Opening Google Chrome window...")
    print(" [*] If login page appears, please sign in with 'Remember me' checked.")
    print("=" * 60 + "\n")

    with sync_playwright() as p:
        ctx = launch_playwright_context(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print("[*] Navigating to Tista disputes page...")
        page.goto(DISPUTES_URL, wait_until="domcontentloaded")
        time.sleep(1.5)

        try:
            rem = page.locator('#user_remember_me, input[name*="remember_me"]').first
            if rem.count() > 0 and not rem.is_checked():
                rem.check()
        except Exception:
            pass

        # Check if already logged in or needs login
        cur_url = page.url.lower()
        needs_login = ("sign_in" in cur_url or "login" in cur_url or page.locator('input[type="password"]').count() > 0)

        if needs_login:
            print("\n[!] Please complete login in the opened Chrome window...")
            start_t = time.time()
            while True:
                cur_url = page.url.lower()
                is_on_login = ("sign_in" in cur_url or "login" in cur_url or page.locator('input[type="password"]').count() > 0)
                if not is_on_login:
                    break
                time.sleep(1)
                if time.time() - start_t > 300:
                    print("\n[!] Login wait timed out after 5 minutes.")
                    break

        # Verification
        final_url = page.url.lower()
        if "sign_in" not in final_url and "login" not in final_url:
            print("\n" + "=" * 60)
            print(" [✓] TISTA LOGIN VERIFIED SUCCESSFULLY!")
            print(f" [✓] Active session cookies saved to: {profile}")
            print(" [✓] You can now close Chrome and use Chargeback Calculator.")
            print("=" * 60 + "\n")
        else:
            print("\n[!] Login was not completed.")

        time.sleep(2)
        ctx.close()

if __name__ == "__main__":
    main()
