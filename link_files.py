"""
Fast filename extraction from file-sharing links (Dropbox Transfer, Dropbox, Google Drive, WeTransfer, ...).

All links in a batch are opened concurrently in ONE headless browser (no Tista profile needed),
with images/fonts/media blocked. For Dropbox Transfer links the file list is read from the JSON
response the page itself requests (/2/file_transfers/files/list...), which is exact and avoids
fixed sleeps. Other hosts fall back to scanning the rendered page text once it has settled.
"""
import asyncio
import re
import threading

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None

import os

# Small cloud instances (Render Starter: 512 MB) get fewer pages and low-memory browser flags.
IS_CLOUD_ENV = bool(os.environ.get("RENDER") or os.environ.get("HEADLESS", "").lower() in ("1", "true", "yes"))
MAX_CONCURRENT_PAGES = int(os.environ.get("LINK_PAGE_CONCURRENCY", "2" if IS_CLOUD_ENV else "4"))
NAV_TIMEOUT_MS = 20000
SETTLE_TIMEOUT_MS = 6000
BLOCKED_RESOURCES = {"image", "font", "media"}
BROWSER_ARGS = ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
if IS_CLOUD_ENV:
    BROWSER_ARGS += [
        "--js-flags=--max-old-space-size=128",
        "--renderer-process-limit=2",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-sync",
        "--mute-audio",
        "--disable-software-rasterizer",
    ]
FILE_EXT_RE = r"(?:jpg|jpeg|png|psd|tif|tiff|cr2|raw|dng|ai|eps|pdf)"

# Same extraction logic the app used before (text nodes + common attributes), run once the page settles.
_DOM_FILENAMES_JS = """() => {
    const filenames = new Set();
    const extRegex = /\\.(?:%(ext)s)$/i;
    const fullRegex = /[\\w\\-. ]+\\.(?:%(ext)s)/gi;
    const ignoreList = ['dropbox', 'logo', 'favicon', 'icon', 'apple-touch', 'webclip', 'badge', 'banner'];
    const isIgnored = n => { const l = n.toLowerCase(); return ignoreList.some(i => l.includes(i)) || n.length < 4 || n.length > 200; };
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
        const val = (node.nodeValue || '').trim();
        if (extRegex.test(val) && !isIgnored(val)) filenames.add(val);
        else if (val.length < 300) for (const m of (val.match(fullRegex) || [])) { const c = m.trim(); if (!isIgnored(c)) filenames.add(c); }
    }
    for (const el of document.querySelectorAll('[title], [alt], [data-filename], [aria-label]'))
        for (const a of ['title', 'alt', 'data-filename', 'aria-label']) {
            const v = (el.getAttribute(a) || '').trim();
            if (v && extRegex.test(v) && !isIgnored(v)) filenames.add(v);
        }
    return [...filenames];
}""" % {"ext": FILE_EXT_RE}


def clean_link(url: str) -> str:
    return str(url or "").strip().rstrip(".,;!?')\"")


def _is_dropbox_transfer(url: str) -> bool:
    return bool(re.search(r"dropbox\.com/t/", url))


async def _extract_one(context, url: str, sem: asyncio.Semaphore) -> list:
    async with sem:
        page = await context.new_page()
        try:
            if _is_dropbox_transfer(url):
                # Wait for the page's own file-list response instead of scraping the DOM.
                async with page.expect_response(
                    lambda r: "/2/file_transfers/files/list" in r.url and r.request.method == "POST",
                    timeout=NAV_TIMEOUT_MS,
                ) as resp_info:
                    await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
                resp = await resp_info.value
                data = await resp.json()
                files = [f.get("relative_path") for f in data.get("files", []) if not f.get("obsolete_is_dir")]
                return [f for f in files if f]

            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            try:
                await page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
            except Exception:
                pass  # busy pages never go idle; scan what has rendered
            return await page.evaluate(_DOM_FILENAMES_JS)
        except Exception as e:
            print(f"[!] Link file extraction note for {url}: {e}")
            return []
        finally:
            await page.close()


async def _extract_many(urls: list) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 800})

            async def _route(route):
                if route.request.resource_type in BLOCKED_RESOURCES:
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", _route)
            sem = asyncio.Semaphore(MAX_CONCURRENT_PAGES)
            results = await asyncio.gather(*[_extract_one(context, u, sem) for u in urls])
            return dict(zip(urls, results))
        finally:
            await browser.close()


def extract_files_from_links(urls) -> dict:
    """
    Returns {url: [filenames]} for every link, extracting them concurrently in one browser.
    Safe to call from any thread (runs its own event loop in a worker thread).
    """
    urls = [u for u in dict.fromkeys(clean_link(u) for u in urls) if u]
    if not urls:
        return {}
    if async_playwright is None:
        print("[!] Playwright is not installed; cannot extract filenames from links.")
        return {u: [] for u in urls}

    out = {}

    def _run():
        try:
            out.update(asyncio.run(_extract_many(urls)))
        except Exception as e:
            print(f"[!] Link extraction batch error: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join()
    for u in urls:
        out.setdefault(u, [])
    print(f"[✓] Extracted filenames from {len(urls)} link(s): " + ", ".join(str(len(out[u])) for u in urls))
    return out
