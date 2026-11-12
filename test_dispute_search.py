from playwright.sync_api import sync_playwright
import os, time, json

USER_DATA_DIR = os.path.expanduser('~/.tista-profile')
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(USER_DATA_DIR, headless=True)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    url = "https://tista.app/manage/vendors/1/order_disputes?q[dispute_reason_eq]=2&q[chargeable_to_eq]=1&q[status_eq]=1&q[location_id_eq]="
    print("Navigating to:", url)
    page.goto(url)
    page.wait_for_load_state("domcontentloaded")
    time.sleep(2)
    
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
    print(f"Extracted {len(rows)} disputes:")
    print(json.dumps(rows[:5], indent=2))
    ctx.close()

