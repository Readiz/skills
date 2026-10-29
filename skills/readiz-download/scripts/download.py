#!/usr/bin/env python3
"""Bounded, no-retry HTTP(S) file downloader with a redacted receipt."""
from __future__ import annotations

import argparse
import codecs
import datetime as dt
import errno
import hashlib
import html
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

UA = "ReadizDownload/1.0"
MAX_DIAGNOSTIC = 8192
MAX_EXCERPT = 500
ALLOWED_HEADERS = ("Content-Type", "Content-Length", "Retry-After", "cf-mitigated")
PDF_MAGIC = b"%PDF-"


class NoDowngradeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(req.full_url).scheme == "https" and urllib.parse.urlsplit(newurl).scheme != "https":
            raise urllib.error.URLError("HTTPS redirect downgrade refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_url(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    try:
        ipaddress.ip_address(host)
        host = "[ip]"
    except ValueError:
        pass
    if ":" in host and host != "[ip]":
        host = f"[{host}]"
    try:
        port = p.port
    except ValueError:
        port = None
    if port:
        host += f":{port}"
    path = _redact_path(p.path)
    return urllib.parse.urlunsplit((p.scheme.lower(), host, path, "", ""))


def _redact_path(value: str) -> str:
    value = re.sub(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email]", value)
    value = re.sub(r"(?<![\w])(?:\d{1,3}\.){3}\d{1,3}(?![\w])", "[ip]", value)
    value = re.sub(r"(?i)(?<=/)(access[_-]?token|token|api[_-]?key|secret|signature|credential|auth)/[^/?#]+", r"\1=[redacted]", value)
    return value[:MAX_EXCERPT]


def _fingerprint(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _redact(s: str) -> str:
    s = html.unescape(s)
    def clean_url(m):
        try:
            return _safe_url(m.group(0))
        except Exception:
            return "[url]"
    s = re.sub(r"(?i)https?://[^\s\"'<>]+", clean_url, s)
    s = re.sub(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email]", s)
    s = re.sub(r"(?<![\w])(?:\d{1,3}\.){3}\d{1,3}(?![\w])", "[ip]", s)
    s = re.sub(r"(?<![\w])\[?[0-9a-fA-F:]{3,}(?:%[\w.-]+)?\]?(?![\w])", lambda m: "[ip]" if ":" in m.group(0) and _is_ipv6(m.group(0)) else m.group(0), s)
    key = r"(?:access[_-]?token|bearer|token|api[_-]?key|secret|signature|credential|auth)"
    s = re.sub(rf"(?i)([\"']?{key}[\"']?)\s*[:=]\s*(?!\[redacted\])[\"']?[^\s,\"'}}&]+", r"\1: [redacted]", s)
    s = re.sub(rf"(?i)\b{key}\s+[^\s,\"'}}&]+", "[redacted]", s)
    s = re.sub(r"(?i)(authorization|cookie|set-cookie)\s*:\s*[^\r\n]+", r"\1: [redacted]", s)
    return s[:MAX_EXCERPT]


def _body_excerpt(raw: bytes) -> tuple[str | None, str | None]:
    text = raw.decode("utf-8", errors="replace")
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    title = _redact(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", title_match.group(1))).strip()) if title_match else None
    clean = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    clean = re.sub(r"(?s)<[^>]+>", " ", clean)
    excerpt = _redact(re.sub(r"\s+", " ", clean).strip()) or None
    return title, excerpt


def _headers(h) -> dict:
    result = {}
    for name in ALLOWED_HEADERS:
        value = h.get(name)
        if value is None:
            continue
        limit = 128 if name.lower() in ("retry-after", "cf-mitigated") else 256
        result[name] = _redact(str(value).replace("\r", " ").replace("\n", " "))[:limit]
    return result


def _is_ipv6(value: str) -> bool:
    try:
        ipaddress.IPv6Address(value.strip("[]"))
        return True
    except ValueError:
        return False


def _failure(code: str, url: str, **extra) -> dict:
    return {"receipt_version": 1, "status": "failed", "code": code, "url": _safe_url(url), **extra}


def _load_report(path: Path | None) -> dict | None:
    if not path or not path.is_file():
        return None
    try:
        if path.stat().st_size > 1024 * 1024:
            return None
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) and obj.get("receipt_version") == 1 else None
    except Exception:
        return None


def _blocked_receipt(prior: dict) -> dict:
    safe = {"receipt_version": 1, "status": "failed", "code": str(prior.get("code", "previous_failure"))[:64], "url": _safe_url(str(prior.get("url", ""))), "retry_blocked": True}
    for key in ("http_status", "retry_after_seconds", "observed_epoch", "bytes"):
        value = prior.get(key)
        if isinstance(value, (int, float)):
            safe[key] = value
    for key in ("title", "excerpt", "previous_code"):
        value = prior.get(key)
        if isinstance(value, str):
            safe[key] = _redact(value)
    headers = prior.get("response_headers")
    if isinstance(headers, dict):
        safe["response_headers"] = _headers({k: v for k, v in headers.items() if k in ALLOWED_HEADERS})
    fingerprint = prior.get("url_fingerprint")
    if isinstance(fingerprint, str) and re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        safe["url_fingerprint"] = fingerprint
    return safe


def _nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _same_path(left: Path, right: Path) -> bool:
    try:
        if left.resolve(strict=False) == right.resolve(strict=False):
            return True
        if left.exists() and right.exists() and os.path.samefile(left, right):
            return True
    except OSError:
        return True
    return False


def _hash_file(path: Path, return_size: bool = False):
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            count += len(chunk)
            digest.update(chunk)
    return (count, digest.hexdigest()) if return_size else digest.hexdigest()


def _reservation_matches(fd: int, path: Path) -> bool:
    try:
        owned = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        return (owned.st_dev == current.st_dev and owned.st_ino == current.st_ino
                and owned.st_size == 0 and current.st_size == 0)
    except OSError:
        return False


def _storage_failure(error: OSError, stage: str) -> _DownloadError:
    number = error.errno if isinstance(error.errno, int) else -1
    message = os.strerror(number) if number >= 0 else "I/O error"
    return _DownloadError("storage_error", errno=number, error=message, stage=stage)


def _cleanup_reservation(path: Path, fd: int, guard) -> dict:
    try:
        _check_io_guard(guard, "before_reservation_cleanup", path)
        if not _reservation_matches(fd, path):
            return {"reservation_cleanup": "not_owned", "reservation_left": path.exists()}
        path.unlink()
        return {"reservation_cleanup": "removed", "reservation_left": False}
    except _DownloadError:
        return {"reservation_cleanup": "guard_failed", "reservation_left": path.exists()}
    except OSError as error:
        number = error.errno if isinstance(error.errno, int) else -1
        message = os.strerror(number) if number >= 0 else "I/O error"
        return {"reservation_cleanup": "storage_error", "reservation_left": path.exists(), "cleanup_errno": number, "cleanup_error": message}


def _commit_verified_file(tmp: Path, destination: Path, guard) -> str:
    """Commit a verified same-directory temp without replacing an unrelated file."""
    try:
        _check_io_guard(guard, "before_hardlink", destination)
        os.link(tmp, destination)
        return "linked"
    except OSError as error:
        unsupported = {getattr(errno, "ENOTSUP", -2), getattr(errno, "EOPNOTSUPP", -3)}
        if error.errno not in unsupported:
            raise _storage_failure(error, "hardlink")

    reservation_fd = None
    owned = False
    replaced = False
    try:
        _check_io_guard(guard, "before_reservation", destination)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        reservation_fd = os.open(str(destination), flags, 0o600)
        owned = True
        _check_io_guard(guard, "before_replace", destination)
        if not _reservation_matches(reservation_fd, destination):
            raise _DownloadError("reservation_lost")
        try:
            os.replace(tmp, destination)
        except OSError as error:
            raise _storage_failure(error, "replace")
        replaced = True
        return "replaced"
    except _DownloadError as error:
        if owned and not replaced:
            error.details.update(_cleanup_reservation(destination, reservation_fd, guard))
        raise
    except OSError as error:
        if error.errno == errno.EEXIST:
            failure = _DownloadError("destination_exists")
        else:
            failure = _storage_failure(error, "reserve")
        if owned and not replaced:
            failure.details.update(_cleanup_reservation(destination, reservation_fd, guard))
        raise failure
    finally:
        if reservation_fd is not None:
            try:
                os.close(reservation_fd)
            except OSError:
                pass


def _check_io_guard(guard, stage: str, path: Path):
    if path.absolute().parts[:2] == ("/", "Volumes") or path.resolve(strict=False).parts[:2] == ("/", "Volumes"):
        try:
            _assert_volume_mount(path)
        except OSError:
            raise _DownloadError("mount_unavailable")
    if guard:
        _run_guard(guard, stage, path)


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            when = dt.datetime.strptime(value, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=dt.timezone.utc)
            return max(0.0, (when - dt.datetime.now(dt.timezone.utc)).total_seconds())
        except ValueError:
            return None


def _assert_volume_mount(path: Path) -> None:
    """Refuse /Volumes paths unless an actual mounted volume is an ancestor."""
    try:
        absolute = path.resolve(strict=False)
        if absolute.parts[:2] != ("/", "Volumes"):
            return
        candidate = absolute.parent
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        while candidate != Path("/Volumes") and candidate != candidate.parent:
            if os.path.ismount(candidate):
                return
            candidate = candidate.parent
        raise OSError("destination is not inside a mounted volume")
    except OSError:
        raise


def _validate_type(path: Path, expected: str | None, content_type: str | None) -> str | None:
    with path.open("rb") as f:
        sample = f.read(4096)
    ct = (content_type or "").split(";", 1)[0].lower()
    if sample.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<head", b"<body")) or "text/html" in ct:
        return "html_instead_of_file"
    typ = (expected or "").lower().lstrip(".")
    signatures = {
        "pdf": sample.startswith(PDF_MAGIC), "zip": sample.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")),
        "docx": sample.startswith(b"PK\x03\x04"), "xlsx": sample.startswith(b"PK\x03\x04"), "pptx": sample.startswith(b"PK\x03\x04"),
        "odt": sample.startswith(b"PK\x03\x04"), "ods": sample.startswith(b"PK\x03\x04"), "odp": sample.startswith(b"PK\x03\x04"),
        "gz": sample.startswith(b"\x1f\x8b"), "gzip": sample.startswith(b"\x1f\x8b"), "tar": len(sample) >= 262 and sample[257:262] == b"ustar",
        "archive": sample.startswith((b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b")) or (len(sample) >= 262 and sample[257:262] == b"ustar"),
        "doc": sample.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"), "xls": sample.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"), "ppt": sample.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
        "csv": b"\x00" not in sample, "txt": b"\x00" not in sample, "json": b"\x00" not in sample,
        "rtf": sample.lstrip().startswith(b"{\\rtf"), "xml": b"\x00" not in sample and sample.lstrip().startswith(b"<"),
    }
    if typ in ("csv", "txt", "json"):
        try:
            decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
            with path.open("rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        decoder.decode(b"", final=True)
                        break
                    if b"\x00" in chunk:
                        raise UnicodeDecodeError("utf-8", chunk, 0, 1, "binary NUL")
                    decoder.decode(chunk, final=False)
        except UnicodeDecodeError:
            signatures[typ] = False
    if typ in signatures and not signatures[typ]:
        return "type_mismatch"
    return None


def download(url, destination, *, report_path=None, expected_type=None,
             expected_sha256=None, max_bytes=104857600,
             min_free_bytes=1073741824, timeout=45, guard=None,
             retry=False) -> dict:
    """Download one public file. Failures return a receipt; never auto-retry."""
    dest = Path(destination)
    report = Path(report_path) if report_path is not None else dest.with_name(dest.name + ".receipt.json")
    if _same_path(report, dest):
        return _failure("invalid_report_path", str(url))
    if report.is_symlink():
        return _failure("report_path_conflict", str(url))
    prior_report = _load_report(report)
    if report.exists():
        requested_fingerprint = _fingerprint(str(url))
        if prior_report is None:
            return _failure("report_path_conflict", str(url), url_fingerprint=requested_fingerprint)
        if prior_report.get("url_fingerprint") != requested_fingerprint:
            return _failure("report_path_conflict", str(url), url_fingerprint=requested_fingerprint)
    def finish(value):
        try:
            if _same_path(report, dest):
                return {**value, "receipt_write_error": "invalid_report_path"}
            if report.is_symlink():
                return {**value, "receipt_write_error": "report_path_conflict"}
            if report.exists():
                existing = _load_report(report)
                if existing is None or existing.get("url_fingerprint") != value.get("url_fingerprint"):
                    return {**value, "receipt_write_error": "report_path_conflict"}
            _check_io_guard(guard, "before_receipt", report)
            _atomic_json(report, value)
        except Exception:
            value = {**value, "receipt_write_error": "guard_failed"}
        return value
    fp = None
    tmp = None
    storage_stage = "request"
    try:
        p = urllib.parse.urlsplit(str(url))
        if p.scheme not in ("http", "https") or not p.hostname or p.username is not None or p.password is not None:
            result = _failure("invalid_url", str(url))
            return finish(result)
        allowed_types = {"pdf", "zip", "docx", "xlsx", "pptx", "odt", "ods", "odp", "gz", "gzip", "tar", "archive", "doc", "xls", "ppt", "csv", "txt", "json", "rtf", "xml"}
        if expected_type is not None and str(expected_type).lower().lstrip(".") not in allowed_types:
            result = _failure("invalid_expected_type", str(url), expected_type=str(expected_type)[:32])
            return finish(result)
        if max_bytes <= 0 or min_free_bytes < 0 or timeout <= 0:
            return finish(_failure("invalid_limits", str(url)))
        if expected_sha256 is not None and not re.fullmatch(r"(?i)[a-f0-9]{64}", str(expected_sha256)):
            return finish(_failure("invalid_expected_sha256", str(url)))
        raw_url = str(url)
        fp = _fingerprint(raw_url)
        if dest.absolute().parts[:2] == ("/", "Volumes") or dest.resolve(strict=False).parts[:2] == ("/", "Volumes"):
            try:
                _assert_volume_mount(dest)
            except OSError:
                raise _DownloadError("mount_unavailable")
        prior = _load_report(report)
        if prior and prior.get("url_fingerprint") == fp and prior.get("status") == "succeeded":
            if prior.get("destination") == str(dest) and dest.is_file():
                digest = _hash_file(dest)
                size = dest.stat().st_size
                if size == prior.get("bytes") and digest == prior.get("sha256"):
                    if size > max_bytes:
                        return _failure("size_limit", raw_url, url_fingerprint=fp, bytes=size)
                    if expected_sha256 and digest.lower() != str(expected_sha256).lower():
                        return _failure("hash_mismatch", raw_url, url_fingerprint=fp, destination=str(dest), bytes=size, sha256=digest)
                    cached_type_error = _validate_type(dest, expected_type, (prior.get("response_headers") or {}).get("Content-Type"))
                    if cached_type_error:
                        return _failure(cached_type_error, raw_url, url_fingerprint=fp, destination=str(dest))
                    return {"receipt_version": 1, "status": "succeeded", "code": "ok", "url": _safe_url(raw_url), "url_fingerprint": fp, "destination": str(dest), "bytes": dest.stat().st_size, "sha256": digest, "reused": True}
        if prior and prior.get("status") == "failed" and not retry:
            return _blocked_receipt(prior)
        if prior and prior.get("code") == "http_429":
            wait = prior.get("retry_after_seconds")
            recorded = prior.get("observed_epoch", time.time())
            if wait is None or time.time() < recorded + wait:
                return _blocked_receipt(prior)
        if dest.exists():
            result = _failure("destination_exists", raw_url, url_fingerprint=fp, destination=str(dest))
            return finish(result)
        existing_parent = _nearest_existing(dest.parent)
        if shutil.disk_usage(existing_parent).free < min_free_bytes:
            result = _failure("insufficient_space", raw_url, url_fingerprint=fp)
            return finish(result)
        _check_io_guard(guard, "before_write", dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        opener = urllib.request.build_opener(NoDowngradeRedirect())
        req = urllib.request.Request(raw_url, headers={"User-Agent": UA, "Accept": "application/pdf,text/csv,application/zip,application/octet-stream,*/*;q=0.5"})
        try:
            response = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = e.read(MAX_DIAGNOSTIC)
            hdrs = _headers(e.headers)
            title, excerpt = _body_excerpt(body)
            marker = (title or "") + " " + (excerpt or "")
            block = bool(re.search(r"(?i)(access denied|you have been blocked|request blocked|challenge required|verify you are human)", marker))
            challenge = hdrs.get("cf-mitigated", "").lower() == "challenge" or bool(re.search(r"(?i)(challenge page|verify you are human|checking your browser)", marker))
            code = "http_429" if e.code == 429 else "http_401" if e.code == 401 else "challenge" if e.code == 403 and challenge else "explicit_block" if block else "http_403" if e.code == 403 else "challenge" if challenge else "explicit_block" if block else "http_5xx" if 500 <= e.code < 600 else "http_other"
            result = _failure(code, raw_url, url_fingerprint=fp, http_status=e.code, response_headers=hdrs, title=title, excerpt=excerpt, retry_after_seconds=_retry_after_seconds(hdrs.get("Retry-After")), observed_epoch=time.time())
            return finish(result)
        with response:
            hdrs = _headers(response.headers)
            if hdrs.get("cf-mitigated", "").lower() == "challenge":
                title, excerpt = _body_excerpt(response.read(MAX_DIAGNOSTIC))
                result = _failure("challenge", raw_url, url_fingerprint=fp, http_status=response.status, response_headers=hdrs, title=title, excerpt=excerpt)
                return finish(result)
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                result = _failure("size_limit", raw_url, url_fingerprint=fp, http_status=response.status, response_headers=hdrs)
                return finish(result)
            tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.part")
            h = hashlib.sha256()
            count = 0
            storage_stage = "partial_open"
            with tmp.open("xb") as f:
                while True:
                    _check_io_guard(guard, "during_write", dest)
                    chunk = response.read(min(65536, max_bytes + 1 - count))
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > max_bytes:
                        raise _DownloadError("size_limit")
                    h.update(chunk)
                    storage_stage = "partial_write"
                    f.write(chunk)
                storage_stage = "partial_sync"
                f.flush()
                os.fsync(f.fileno())
            if length is not None and count != int(length):
                raise _DownloadError("truncated")
            _check_io_guard(guard, "before_commit", dest)
            mismatch = _validate_type(tmp, expected_type, hdrs.get("Content-Type"))
            if mismatch:
                title = excerpt = None
                code = mismatch
                if mismatch == "html_instead_of_file":
                    with tmp.open("rb") as body_file:
                        title, excerpt = _body_excerpt(body_file.read(MAX_DIAGNOSTIC))
                    marker = (title or "") + " " + (excerpt or "")
                    if hdrs.get("cf-mitigated", "").lower() == "challenge" or re.search(r"(?i)(challenge page|verify you are human|checking your browser)", marker):
                        code = "challenge"
                    elif re.search(r"(?i)(access denied|you have been blocked|request blocked)", marker):
                        code = "explicit_block"
                raise _DownloadError(code, title=title, excerpt=excerpt, response_headers=hdrs, http_status=response.status)
            digest = h.hexdigest()
            if expected_sha256 and digest.lower() != expected_sha256.lower():
                raise _DownloadError("hash_mismatch")
            storage_stage = "partial_verify"
            reread_count, reread_digest = _hash_file(tmp, return_size=True)
            if reread_count != count or reread_digest != digest:
                raise _DownloadError("verification_mismatch")
            if dest.exists():
                raise _DownloadError("destination_exists")
            # Hard-link commit never replaces a destination created concurrently.
            storage_stage = "commit"
            committed_by = _commit_verified_file(tmp, dest, guard)
            if committed_by == "linked":
                storage_stage = "partial_cleanup"
                tmp.unlink()
            tmp = None
            result = {"receipt_version": 1, "status": "succeeded", "code": "ok", "url": _safe_url(raw_url), "url_fingerprint": fp, "destination": str(dest), "bytes": count, "sha256": digest, "http_status": response.status, "response_headers": hdrs, "reused": False}
            return finish(result)
    except _DownloadError as e:
        result = _failure(str(e), str(url), url_fingerprint=fp, destination=str(dest), **e.details)
    except urllib.error.URLError as e:
        result = _failure("network_error", str(url), url_fingerprint=fp, error=type(getattr(e, "reason", None)).__name__ if getattr(e, "reason", None) is not None else "network error")
    except http.client.IncompleteRead:
        result = _failure("truncated", str(url), url_fingerprint=fp, destination=str(dest))
    except (TimeoutError, socket.timeout, ConnectionError, socket.gaierror) as e:
        result = _failure("network_error", str(url), url_fingerprint=fp, error=type(e).__name__)
    except OSError as e:
        number = e.errno if isinstance(e.errno, int) else -1
        message = os.strerror(number) if number >= 0 else "I/O error"
        result = _failure("storage_error", str(url), url_fingerprint=fp, error=message, errno=number, stage=storage_stage)
    except Exception as e:
        result = _failure("network_error" if isinstance(e, (TimeoutError, OSError)) else "download_error", str(url), url_fingerprint=fp, error=type(e).__name__)
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
    return finish(result)


class _DownloadError(Exception):
    def __init__(self, code, **details):
        super().__init__(code)
        self.details = details


def _run_guard(guard, stage, destination):
    try:
        guard(stage, destination)
    except Exception:
        raise _DownloadError("guard_failed")


def _cli_guard(path: Path):
    def check(stage, destination):
        _assert_volume_mount(destination)
    return check


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url")
    ap.add_argument("destination")
    ap.add_argument("--report")
    ap.add_argument("--expected-type")
    ap.add_argument("--expected-sha256")
    ap.add_argument("--max-bytes", type=int, default=104857600)
    ap.add_argument("--min-free-bytes", type=int, default=1073741824)
    ap.add_argument("--timeout", type=float, default=45)
    ap.add_argument("--retry", action="store_true")
    a = ap.parse_args(argv)
    dest = Path(a.destination)
    result = download(a.url, dest, report_path=a.report, expected_type=a.expected_type, expected_sha256=a.expected_sha256, max_bytes=a.max_bytes, min_free_bytes=a.min_free_bytes, timeout=a.timeout, retry=a.retry, guard=_cli_guard(dest) if str(dest).startswith("/Volumes/") else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
