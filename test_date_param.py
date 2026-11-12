from playwright.sync_api import sync_playwright
from pathlib import Path
import time, urllib.parse

profile = Path.home() / '.tista-profile'
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(str(profile), headless=True)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    
    # Test 1: URL with q[fault_at_between]=02/11/26 - 11/11/26
    val = "02/11/26 - 11/11/26"
    url = f"https://tista.app/manage/vendors/1/order_disputes?q[fault_at_between]={urllib.parse.quote(val)}"
    print("Navigating to:", url)
    page.goto(url)
    page.wait_for_load_state("domcontentloaded")
    time.sleep(2)
    
    # Check the input value
    inp_val = page.locator("#q_fault_at_between").input_value()
    print("Input value in page:", inp_val)
    rows_cnt = page.locator("table tbody tr").count()
    print("Rows found with URL param:", rows_cnt)
    for i in range(min(3, rows_cnt)):
        print(f"Row {i}:", page.locator("table tbody tr").nth(i).inner_text().replace("\n", " | ")[:80])
    
    # Test 2: If we set it via flatpickr / filling
    page.goto("https://tista.app/manage/vendors/1/order_disputes")
    page.wait_for_load_state("domcontentloaded")
    time.sleep(1)
    
    # Fill the input using evaluate
    page.evaluate("""(v) => {
        const el = document.querySelector('#q_fault_at_between');
        if(el) {
            el.value = v;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }""", val)
    
    ctx.close()

