import hashlib
import errno
import importlib.util
import json
import os
import tempfile
import unittest
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).parents[1] / "skills/readiz-download/scripts/download.py"
spec = importlib.util.spec_from_file_location("readiz_download", MODULE)
dl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dl)


class FakeResponse(BytesIO):
    def __init__(self, body, headers, status=200):
        super().__init__(body); self.headers = headers; self.status = status

    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class FakeOpener:
    def __init__(self, path, counts): self.path, self.counts = path, counts
    def open(self, request, timeout=45):
        self.counts[self.path] = self.counts.get(self.path, 0) + 1
        data = {
            "/ok": (b"%PDF-1.7\nbody", 200, {"Content-Type": "application/pdf", "Content-Length": "13"}),
            "/403": (b"<html><title>Access denied</title>Denied from 192.0.2.5 user a@b.com token=abcdefgh</html>", 403, {"Content-Type": "text/html", "Content-Length": "99"}),
            "/429": (b"later", 429, {"Retry-After": "3600", "Content-Length": "5"}),
            "/html": (b"<html><title>Sign in</title>login</html>", 200, {"Content-Type": "text/html", "Content-Length": "40"}),
            "/truncated": (b"%PDF-short", 200, {"Content-Type": "application/pdf", "Content-Length": "100"}),
        }.get(self.path)
        if data is None: raise AssertionError("unexpected network request")
        body, status, fields = data
        headers = Message()
        for key, value in fields.items(): headers[key] = value
        if status >= 400: raise dl.urllib.error.HTTPError(request.full_url, status, "error", headers, BytesIO(body))
        return FakeResponse(body, headers, status)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.counts = {}

    def tearDown(self):
        self.temp.cleanup()

    def get(self, path): return f"https://example.org{path}"

    def fetch(self, path, *args, **kwargs):
        with patch.object(dl.urllib.request, "build_opener", return_value=FakeOpener(path, self.counts)):
            return dl.download(self.get(path), *args, min_free_bytes=0, **kwargs)

    def test_pdf_hash_and_verified_reuse_without_request(self):
        out, receipt = self.root / "a.pdf", self.root / "receipt.json"
        body = b"%PDF-1.7\nbody"
        result = self.fetch("/ok", out, report_path=receipt, expected_type="pdf", expected_sha256=hashlib.sha256(body).hexdigest())
        count = self.counts["/ok"]
        again = dl.download(self.get("/ok"), out, report_path=receipt, expected_type="pdf", min_free_bytes=0)
        self.assertEqual(result["status"], "succeeded"); self.assertTrue(again["reused"]); self.assertEqual(self.counts["/ok"], count)

    def test_reuse_still_checks_new_expected_hash_and_preserves_success_receipt(self):
        out, receipt = self.root / "a.pdf", self.root / "receipt.json"
        self.fetch("/ok", out, report_path=receipt, expected_type="pdf")
        original = receipt.read_text()
        result = dl.download(self.get("/ok"), out, report_path=receipt, expected_type="pdf", expected_sha256="0" * 64, min_free_bytes=0)
        self.assertEqual(result["code"], "hash_mismatch"); self.assertEqual(receipt.read_text(), original); self.assertEqual(self.counts["/ok"], 1)

    def test_403_diagnostics_redacted_and_no_implicit_retry(self):
        out, receipt = self.root / "a.pdf", self.root / "receipt.json"
        url = self.get("/403") + "?token=SECRET#frag"
        with patch.object(dl.urllib.request, "build_opener", return_value=FakeOpener("/403", self.counts)):
            result = dl.download(url, out, report_path=receipt, min_free_bytes=0)
        text = json.dumps(result)
        saved = receipt.read_text(encoding="utf-8")
        self.assertEqual(result["code"], "explicit_block"); self.assertIn("Denied", result["excerpt"])
        self.assertIn("Denied", saved)
        self.assertNotIn("192.0.2.5", text); self.assertNotIn("a@b.com", text); self.assertNotIn("SECRET", text); self.assertEqual(self.counts["/403"], 1)
        with patch.object(dl.urllib.request, "build_opener", return_value=FakeOpener("/403", self.counts)):
            again = dl.download(url, out, report_path=receipt, retry=True, min_free_bytes=0)
        self.assertEqual(again["code"], "explicit_block"); self.assertEqual(self.counts["/403"], 2)

    def test_429_retry_after_is_enforced_even_with_retry(self):
        out, receipt = self.root / "a.pdf", self.root / "receipt.json"
        with patch.object(dl.urllib.request, "build_opener", return_value=FakeOpener("/429", self.counts)):
            result = dl.download(self.get("/429"), out, report_path=receipt, min_free_bytes=0)
        saved = json.loads(receipt.read_text())
        again = dl.download(self.get("/429"), out, report_path=receipt, retry=True, min_free_bytes=0)
        self.assertEqual(result["code"], "http_429"); self.assertEqual(again["code"], "http_429"); self.assertTrue(again["retry_blocked"]); self.assertEqual(self.counts["/429"], 1)
        self.assertEqual(json.loads(receipt.read_text()), saved)

    def test_html_disguised_as_pdf_rejected(self):
        result = self.fetch("/html", self.root / "a.pdf", expected_type="pdf")
        self.assertEqual(result["code"], "html_instead_of_file"); self.assertFalse((self.root / "a.pdf").exists())

    def test_content_length_truncation(self):
        result = self.fetch("/truncated", self.root / "a.pdf")
        self.assertEqual(result["code"], "truncated")

    def test_limit_and_existing_destination_preserved(self):
        out = self.root / "a.pdf"
        result = self.fetch("/ok", out, max_bytes=3)
        self.assertEqual(result["code"], "size_limit"); self.assertFalse(out.exists())
        kept = self.root / "keep.pdf"
        kept.write_bytes(b"keep")
        result = dl.download(self.get("/ok"), kept, min_free_bytes=0)
        self.assertEqual(result["code"], "destination_exists"); self.assertEqual(kept.read_bytes(), b"keep")

    def test_changed_source_cannot_reuse_old_receipt_or_destination(self):
        out, receipt = self.root / "a.pdf", self.root / "receipt.json"
        self.fetch("/ok", out, report_path=receipt)
        result = dl.download(self.get("/different"), out, report_path=receipt, min_free_bytes=0)
        self.assertEqual(result["code"], "report_path_conflict")

    def test_unknown_expected_type_fails_before_request(self):
        result = self.fetch("/ok", self.root / "a.pdf", expected_type="madeup")
        self.assertEqual(result["code"], "invalid_expected_type"); self.assertNotIn("/ok", self.counts)

    def test_unmounted_volumes_path_does_not_fall_back_to_local_directory(self):
        path = Path("/Volumes") / ("readiz-download-unmounted-" + os.urandom(5).hex()) / "file.pdf"
        with patch.object(dl.urllib.request, "build_opener") as opener:
            result = dl.download(self.get("/ok"), path, min_free_bytes=0)
        opener.assert_not_called()
        self.assertEqual(result["code"], "mount_unavailable")
        self.assertFalse(path.parent.exists())

    def test_sensitive_values_are_redacted_in_json_and_paths(self):
        text = dl._redact('{"error":"Invalid credential", "token":"v7X3abcQ9LM2", "access_token":"SECRET"}')
        path = dl._redact("https://example.org/token/v7X3abcQ9LM2/file.pdf")
        self.assertNotIn("v7X3abcQ9LM2", text + path); self.assertNotIn("SECRET", text + path); self.assertIn("file.pdf", path)

    def test_utf8_csv_with_multibyte_character_at_sample_boundary(self):
        file = self.root / "boundary.csv"
        file.write_bytes(b"a" * 4095 + "가".encode("utf-8") + b"\n")
        self.assertIsNone(dl._validate_type(file, "csv", "text/csv"))

    def test_unsupported_hardlink_uses_exclusive_reservation_and_replace(self):
        out = self.root / "a.pdf"
        unsupported = OSError(errno.ENOTSUP, "unsupported")
        with patch.object(dl.os, "link", side_effect=unsupported):
            result = self.fetch("/ok", out, expected_type="pdf")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(out.read_bytes(), b"%PDF-1.7\nbody")

    def test_reservation_guard_failure_cleans_only_owned_reservation(self):
        out, report = self.root / "a.pdf", self.root / "receipt.json"
        stages = []
        def guard(stage, path):
            stages.append(stage)
            if stage == "before_replace":
                raise RuntimeError("stop")
        with patch.object(dl.os, "link", side_effect=OSError(errno.ENOTSUP, "unsupported")):
            with patch.object(dl.urllib.request, "build_opener", return_value=FakeOpener("/ok", self.counts)):
                result = dl.download(self.get("/ok"), out, report_path=report, expected_type="pdf", min_free_bytes=0, guard=guard)
        self.assertEqual(result["code"], "guard_failed")
        self.assertIn("before_reservation_cleanup", stages)
        self.assertFalse(out.exists())

    def test_failed_cleanup_guard_leaves_empty_reservation_uncompleted(self):
        out, report = self.root / "a.pdf", self.root / "receipt.json"
        def guard(stage, path):
            if stage in ("before_replace", "before_reservation_cleanup"):
                raise RuntimeError("stop")
        with patch.object(dl.os, "link", side_effect=OSError(errno.ENOTSUP, "unsupported")):
            with patch.object(dl.urllib.request, "build_opener", return_value=FakeOpener("/ok", self.counts)):
                result = dl.download(self.get("/ok"), out, report_path=report, expected_type="pdf", min_free_bytes=0, guard=guard)
        self.assertEqual(result["code"], "guard_failed")
        self.assertTrue(result["reservation_left"])
        self.assertTrue(out.exists()); self.assertEqual(out.read_bytes(), b"")
        self.assertEqual(json.loads(report.read_text())["status"], "failed")

    def test_reservation_race_preserves_competing_file(self):
        out = self.root / "a.pdf"
        changed = False
        def guard(stage, path):
            nonlocal changed
            if stage == "before_replace" and not changed:
                changed = True
                path.unlink()
                path.write_bytes(b"other writer")
        with patch.object(dl.os, "link", side_effect=OSError(errno.ENOTSUP, "unsupported")):
            with patch.object(dl.urllib.request, "build_opener", return_value=FakeOpener("/ok", self.counts)):
                result = dl.download(self.get("/ok"), out, expected_type="pdf", min_free_bytes=0, guard=guard)
        self.assertEqual(result["code"], "reservation_lost")
        self.assertEqual(out.read_bytes(), b"other writer")

    def test_reservation_with_unexpected_contents_is_not_replaced_or_removed(self):
        out = self.root / "a.pdf"
        written = False
        def guard(stage, path):
            nonlocal written
            if stage == "before_replace" and not written:
                written = True
                path.write_bytes(b"unexpected contents")
        with patch.object(dl.os, "link", side_effect=OSError(errno.ENOTSUP, "unsupported")):
            with patch.object(dl.urllib.request, "build_opener", return_value=FakeOpener("/ok", self.counts)):
                result = dl.download(self.get("/ok"), out, expected_type="pdf", min_free_bytes=0, guard=guard)
        self.assertEqual(result["code"], "reservation_lost")
        self.assertEqual(out.read_bytes(), b"unexpected contents")

    def test_destination_created_before_reservation_is_preserved(self):
        out = self.root / "a.pdf"
        created = False
        def guard(stage, path):
            nonlocal created
            if stage == "before_reservation" and not created:
                created = True
                path.write_bytes(b"other writer")
        with patch.object(dl.os, "link", side_effect=OSError(errno.ENOTSUP, "unsupported")):
            with patch.object(dl.urllib.request, "build_opener", return_value=FakeOpener("/ok", self.counts)):
                result = dl.download(self.get("/ok"), out, expected_type="pdf", min_free_bytes=0, guard=guard)
        self.assertEqual(result["code"], "destination_exists")
        self.assertEqual(out.read_bytes(), b"other writer")

    def test_hardlink_storage_error_reports_safe_stage_and_errno(self):
        out = self.root / "a.pdf"
        with patch.object(dl.os, "link", side_effect=OSError(errno.EIO, "private path should not leak")):
            result = self.fetch("/ok", out, expected_type="pdf")
        self.assertEqual(result["code"], "storage_error")
        self.assertEqual(result["errno"], errno.EIO)
        self.assertEqual(result["stage"], "hardlink")
        self.assertEqual(result["error"], os.strerror(errno.EIO))
        self.assertNotIn("private path", json.dumps(result))

    def test_replace_storage_error_cleans_owned_reservation(self):
        out, report = self.root / "a.pdf", self.root / "receipt.json"
        real_replace = dl.os.replace
        calls = 0
        def fail_data_replace_once(source, destination):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError(errno.EIO, "private path should not leak")
            return real_replace(source, destination)
        with patch.object(dl.os, "link", side_effect=OSError(errno.ENOTSUP, "unsupported")):
            with patch.object(dl.os, "replace", side_effect=fail_data_replace_once):
                with patch.object(dl.urllib.request, "build_opener", return_value=FakeOpener("/ok", self.counts)):
                    result = dl.download(self.get("/ok"), out, report_path=report, expected_type="pdf", min_free_bytes=0)
        self.assertEqual(result["code"], "storage_error")
        self.assertEqual(result["stage"], "replace")
        self.assertEqual(result["errno"], errno.EIO)
        self.assertEqual(result["reservation_cleanup"], "removed")
        self.assertFalse(out.exists())
        self.assertEqual(json.loads(report.read_text())["status"], "failed")

    def test_report_cannot_overwrite_destination_through_direct_or_normalized_path(self):
        dest = self.root / "keep.pdf"
        dest.write_bytes(b"keep")
        result = dl.download("file:///invalid", dest, report_path=dest)
        self.assertEqual(result["code"], "invalid_report_path"); self.assertEqual(dest.read_bytes(), b"keep")
        alias = self.root / "sub" / ".." / "keep.pdf"
        with patch.object(dl.urllib.request, "build_opener") as opener:
            result = dl.download(self.get("/ok"), dest, report_path=alias, min_free_bytes=0)
        self.assertEqual(result["code"], "invalid_report_path"); opener.assert_not_called(); self.assertEqual(dest.read_bytes(), b"keep")

    def test_report_hardlink_to_destination_is_rejected(self):
        dest, report = self.root / "keep.pdf", self.root / "receipt.json"
        dest.write_bytes(b"keep")
        os.link(dest, report)
        result = dl.download("file:///invalid", dest, report_path=report)
        self.assertEqual(result["code"], "invalid_report_path"); self.assertEqual(dest.read_bytes(), b"keep"); self.assertEqual(report.read_bytes(), b"keep")

    def test_arbitrary_existing_report_is_never_overwritten(self):
        report = self.root / "receipt.json"
        report.write_text("user data, not a receipt")
        with patch.object(dl.urllib.request, "build_opener") as opener:
            result = dl.download(self.get("/ok"), self.root / "a.pdf", report_path=report, min_free_bytes=0)
        self.assertEqual(result["code"], "report_path_conflict"); opener.assert_not_called(); self.assertEqual(report.read_text(), "user data, not a receipt")


if __name__ == "__main__":
    unittest.main()
