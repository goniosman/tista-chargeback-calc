from playwright.sync_api import sync_playwright
from pathlib import Path
import time, urllib.parse

profile = Path.home() / '.tista-profile'
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(str(profile), headless=True)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    
    test_formats = [
        "02/11/26 - 11/11/26",
        "02/11/2026 - 11/11/2026",
        "2026-11-02 - 2026-11-11",
        "08/11/26 - 08/11/26",  # single day: Nov 8, 2026
    ]
    
    for fmt in test_formats:
        url = f"https://tista.app/manage/vendors/1/order_disputes?q[fault_at_between]={urllib.parse.quote(fmt)}"
        page.goto(url)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(1.5)
        cnt = page.locator("table tbody tr").count()
        inp_v = page.locator("#q_fault_at_between").input_value()
        print(f"Format: '{fmt}' -> Input shows: '{inp_v}', Rows: {cnt}")
        if cnt > 0 and cnt < 25:
            print("First row:", page.locator("table tbody tr").first.inner_text().replace("\n", " | ")[:70])
            
    ctx.close()

