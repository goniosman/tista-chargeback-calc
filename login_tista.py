#!/usr/bin/env python3
"""
Tista One-Time Login Helper
Opens the browser, allows you to log in, and permanently saves your login session.
"""
from pathlib import Path
import time
from playwright.sync_api import sync_playwright

LOCAL_PROFILE = Path(__file__).parent / ".tista-profile"
profile_dir = LOCAL_PROFILE if LOCAL_PROFILE.exists() else (Path.home() / ".tista-profile")

print("=" * 60)
print(" Opening Tista in browser for Login...")
print(" Please enter your email and password, check 'Remember me',")
print(" and click 'Sign in'.")
print("=" * 60)

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        str(profile_dir),
        headless=False,
        viewport={"width": 1400, "height": 900}
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://tista.app/users/sign_in")
    
    try:
        rem = page.locator('#user_remember_me, input[name*="remember_me"]').first
        if rem.count() > 0:
            rem.check()
    except Exception:
        pass

    # Wait until logged in
    while "sign_in" in page.url or page.locator('input[type="password"]').count() > 0:
        time.sleep(1)

    print("\n[✓] Login successful! Session is now saved in your profile.")
    print("    You can now close this window or use the calculator.\n")
    time.sleep(3)
    ctx.close()

