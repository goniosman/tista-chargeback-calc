"""
Backend tests against a simulated Tista - no network, nothing is charged or closed.
Run from the repo root:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="tista_test_")
os.environ["TISTA_CACHE_DIR"] = _TMP  # keep caches and close_log.jsonl out of the repo
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tista_delay_fetch as t  # noqa: E402

DISPUTES = "https://tista.app/manage/vendors/1/order_disputes"


def edit_page(status):
    selected = 'selected="selected" '
    opts = "".join(
        f'<option {selected if v == status else ""}value="{v}">{v}</option>'
        for v in ("awaiting_response_from_vendor", "under_review", "closed")
    )
    return f'<select name="order_dispute[status]">{opts}</select>'


class Resp:
    def __init__(self, code, location="", text="", url="https://tista.app/x"):
        self.status_code, self.headers, self.text, self.url = code, {"Location": location}, text, url


class FakeTista:
    """Thread-safe stand-in for the Tista HTTP session."""

    def __init__(self, posts=None, edit_status="closed", latency=0.0, valid_token="tok", reject_ids=()):
        self.posts = list(posts or [])
        self.edit_status, self.latency, self.valid_token = edit_status, latency, valid_token
        self.reject_ids = set(reject_ids)
        self.lock = threading.Lock()
        self.post_calls, self.inflight, self.max_inflight, self.token_fetches = [], 0, 0, 0

    def _wait(self):
        with self.lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        time.sleep(self.latency)
        with self.lock:
            self.inflight -= 1

    def get(self, url, **kw):
        self._wait()
        if url.endswith("/edit"):
            return Resp(200, text=edit_page(self.edit_status))
        with self.lock:
            self.token_fetches += 1
        return Resp(200, text=f'<meta name="csrf-token" content="{self.valid_token}">')

    def post(self, url, data=None, **kw):
        assert kw.get("allow_redirects") is False, "close must not follow redirects"
        self._wait()
        with self.lock:
            self.post_calls.append(dict(data))
            if self.posts:
                return self.posts.pop(0)
        did = url.rstrip("/").split("/")[-1]
        if data["authenticity_token"] != self.valid_token:
            return Resp(422)
        if did in self.reject_ids:
            return Resp(200, text='<div class="invalid-feedback">Charge is invalid</div>')
        return Resp(302, f"{DISPUTES}/{did}")


class CloseDisputeTests(unittest.TestCase):
    def setUp(self):
        self._orig_session = t.get_tista_http_session
        self._orig_log = t.CLOSE_LOG_FILE
        t.CLOSE_LOG_FILE = Path(_TMP) / f"close_log_{self._testMethodName}.jsonl"
        t._CACHED_CSRF_TOKEN = "tok"
        t._DISPUTE_STATUS_CACHE.clear()

    def tearDown(self):
        t.get_tista_http_session = self._orig_session
        t.CLOSE_LOG_FILE = self._orig_log

    def close(self, fake, **kw):
        t.get_tista_http_session = lambda *a, **k: fake
        return t.close_dispute_via_direct_http(dispute_id="123", amount=kw.get("amount", 3), fault_severity="High")

    def test_redirect_and_verified_closed_is_success(self):
        fake = FakeTista([Resp(302, f"{DISPUTES}/123")])
        ok, res = self.close(fake)
        self.assertTrue(ok)
        self.assertTrue(res["verified"])
        self.assertEqual(len(fake.post_calls), 1)

    def test_form_rerender_is_failure_with_reason(self):
        fake = FakeTista([Resp(200, text='<div class="invalid-feedback">Charge is invalid</div>')])
        ok, res = self.close(fake)
        self.assertFalse(ok)
        self.assertIn("Charge is invalid", res)

    def test_redirect_to_sign_in_is_failure(self):
        ok, res = self.close(FakeTista([Resp(302, "https://tista.app/users/sign_in")]))
        self.assertFalse(ok)
        self.assertIn("log in", res)

    def test_expired_token_refreshes_and_retries_once(self):
        fake = FakeTista([Resp(422), Resp(302, f"{DISPUTES}/123")])
        ok, _ = self.close(fake)
        self.assertTrue(ok)
        self.assertEqual(len(fake.post_calls), 2)

    def test_server_error_retries_once_without_optional_fields(self):
        fake = FakeTista([Resp(500), Resp(302, f"{DISPUTES}/123")])
        ok, _ = self.close(fake)
        self.assertTrue(ok)
        self.assertNotIn("order_dispute[fault_severity]", fake.post_calls[1])

    def test_other_errors_are_not_retried(self):
        fake = FakeTista([Resp(404)])
        ok, _ = self.close(fake)
        self.assertFalse(ok)
        self.assertEqual(len(fake.post_calls), 1)

    def test_saved_but_not_closed_is_failure(self):
        ok, res = self.close(FakeTista([Resp(302, f"{DISPUTES}/123")], edit_status="under_review"))
        self.assertFalse(ok)
        self.assertIn("not closed", res)

    def test_batch_close_runs_in_parallel_refreshes_token_once_and_logs(self):
        fake = FakeTista(latency=0.3, valid_token="new", reject_ids={"105"})
        t.get_tista_http_session = lambda *a, **k: fake
        t._CACHED_CSRF_TOKEN = "old"  # every first POST gets 422 at the same time
        items = [{"dispute_id": str(100 + i), "order_id": f"#{900000 + i}", "amount": 0.5 if i == 0 else 3.25} for i in range(8)]
        start = time.time()
        res = t.batch_close_disputes_on_tista(items)
        elapsed = time.time() - start

        self.assertEqual(res["closed"], 7)
        self.assertEqual(fake.max_inflight, t.CLOSE_CONCURRENCY)
        self.assertEqual(fake.token_fetches, 1)
        self.assertLess(elapsed, 8 * 3 * 0.3 / 2)
        lines = [json.loads(l) for l in t.CLOSE_LOG_FILE.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 8)
        rejected = next(l for l in lines if l["dispute_id"] == "105")
        self.assertFalse(rejected["success"])
        self.assertIn("invalid", rejected["error"])
        self.assertEqual(next(l for l in lines if l["dispute_id"] == "100")["amount_charged"], 2.0)


class PaginationTests(unittest.TestCase):
    def test_fetches_all_remaining_pages_from_banner(self):
        requested = []

        class Session:
            def get(self, url, **kw):
                requested.append(url)
                n = int(url.rsplit("page=", 1)[1])
                return Resp(200, text=f"page{n}")

        first = "<div>Displaying disputes <b>1&nbsp;-&nbsp;25</b> of <b>97</b> in total</div>"
        rows = t.fetch_remaining_dispute_pages(Session(), f"{DISPUTES}?q[status_eq]=1", first, lambda h: [h])
        self.assertEqual(sorted(rows), ["page2", "page3", "page4"])
        self.assertTrue(all("q[status_eq]=1&page=" in u for u in requested))

    def test_single_page_needs_no_extra_requests(self):
        class Session:
            def get(self, *a, **k):
                raise AssertionError("no request expected")

        self.assertEqual(t.fetch_remaining_dispute_pages(Session(), DISPUTES, "Displaying all 7 disputes", lambda h: []), [])


class OrderFilesTests(unittest.TestCase):
    def test_order_not_on_tista_does_not_start_a_browser(self):
        orig_api, orig_pw = t.fetch_order_files_direct_api, t.sync_playwright
        t.fetch_order_files_direct_api = lambda oid, session=None: []

        def no_browser(*a, **k):
            raise AssertionError("browser must not start for a definite 'not found'")

        t.sync_playwright = no_browser
        try:
            res = t.fetch_order_files_from_tista("123456789")
        finally:
            t.fetch_order_files_direct_api, t.sync_playwright = orig_api, orig_pw
        self.assertEqual(res["files"], [])
        self.assertIn("not found", res["warnings"][0])


if __name__ == "__main__":
    unittest.main()
