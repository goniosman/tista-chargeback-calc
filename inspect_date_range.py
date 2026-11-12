from playwright.sync_api import sync_playwright
from pathlib import Path
import time

profile = Path.home() / '.tista-profile'
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(str(profile), headless=True)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto('https://tista.app/manage/vendors/1/order_disputes')
    page.wait_for_load_state('domcontentloaded')
    time.sleep(2)
    
    labels = page.locator('label:has-text("Fault at")')
    print('Labels with Fault at count:', labels.count())
    for i in range(labels.count()):
        parent = labels.nth(i).locator('xpath=..')
        print('Parent HTML:\n', parent.inner_html())
    
    # Check all inputs in the form
    inputs = page.locator('#new_q input')
    print('Total inputs in #new_q:', inputs.count())
    for i in range(inputs.count()):
        inp = inputs.nth(i)
        print(f"Input {i}: name='{inp.get_attribute('name')}' id='{inp.get_attribute('id')}' class='{inp.get_attribute('class')}' value='{inp.get_attribute('value')}' placeholder='{inp.get_attribute('placeholder')}'")
    
    ctx.close()

