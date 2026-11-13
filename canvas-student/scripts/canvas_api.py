#!/usr/bin/env python3
"""Canvas LMS command-line client for students (Python standard library only).

Configuration comes from environment variables, never from arguments, so the
token does not end up in shell history or process listings:

    CANVAS_BASE_URL   e.g. https://myschool.instructure.com
    CANVAS_API_TOKEN  a personal access token (Account > Settings > New Access Token)

Every command prints JSON to stdout. Errors go to stderr as JSON with a
non-zero exit code. Commands that change anything in Canvas print a dry run
unless --confirm is passed. Run `canvas_api.py -h` or `canvas_api.py CMD -h`.

API reference: https://canvas.instructure.com/doc/api/ (moving to
https://developerdocs.instructure.com/services/canvas).
"""
from __future__ import annotations

import argparse
import codecs
import datetime as dt
import html
import http.client
import json
import mimetypes
import os
import posixpath
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

ENV_BASE_URL = "CANVAS_BASE_URL"
ENV_TOKEN = "CANVAS_API_TOKEN"
# Canvas pages lists at 10 items by default and documents only "an unspecified limit" for per_page
# (file.pagination.html); per-endpoint docs state "Maximum of 100". Every list call therefore asks for
# 100 and always follows the Link header, so a server-side cap never truncates results.
MAX_PER_PAGE = 100
MAX_THROTTLE_RETRIES = 3        # file.throttling.html: "retry the request at a later time"
MAX_RETRY_AFTER_S = 60
MAX_REDIRECTS = 5
REQUEST_TIMEOUT_S = 60
CALENDAR_CONTEXT_LIMIT = 10     # calendar_events.html: extra context codes are ignored
DEFAULT_STREAM_LIMIT = 30       # activity stream / inbox can be very long
DEFAULT_PLANNER_DAYS = 14
DEFAULT_ANNOUNCEMENT_DAYS = 14
MAX_NICKNAME_LENGTH = 60
# A bearer token is printable ASCII without spaces (RFC 6750 b64token is stricter); quotes come from copy errors.
TOKEN_PATTERN = re.compile(r"[!#-&(-~]+")
MAX_SUBJECT_LENGTH = 255       # conversations.html: "Maximum length is 255 characters."
# D11: quiz-taking and quiz question endpoints are refused for every command, including get/api.
# quiz_submission_questions.html, quiz_submissions.html, new_quizzes.html (items hold questions).
QUIZ_BLOCKED_ANY_METHOD = (re.compile(r"/quiz_submissions/[^/]+/questions(?=[/.]|$)"),
                           re.compile(r"/quizzes/[^/]+/questions(?=[/.]|$)"),
                           re.compile(r"^/api/quiz/v1/.*/items(?=[/.]|$)"))
QUIZ_BLOCKED_WRITES = (re.compile(r"/quizzes/[^/]+/submissions(?=[/.]|$)"),  # start, complete, file answers
                       re.compile(r"/quiz_submissions(?=[/.]|$)"))
# Arguments interpolated into URL paths; each is escaped into exactly one path segment.
PATH_ID_ARGS = ("course", "assignment", "module", "item", "topic", "quiz", "group", "id", "file_id", "reply_to",
                "folder", "grading_period")
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)})
EXIT_OK = 0
EXIT_API_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_USAGE_ERROR = 3            # distinct from 2 so a bad flag is never mistaken for missing configuration

JsonDict = dict[str, Any]


class CanvasError(Exception):
    """An API, network or validation failure the user should see."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ConfigError(Exception):
    """Missing or invalid configuration."""


class UsageError(Exception):
    """Invalid command-line arguments."""


# --------------------------------------------------------------------------- helpers

def normalize_base_url(raw: str) -> str:
    """Return scheme://host[:port] for a Canvas instance, rejecting unsafe values.

    The API lives at https://<canvas>/api/v1 (index.html), so any path, query or fragment copied from the
    browser address bar (a course page, ?login_success=1) is dropped.
    """
    parts = urllib.parse.urlsplit(raw.strip())
    if not parts.scheme or not parts.hostname:
        raise ConfigError(f"{ENV_BASE_URL} must be a full URL such as https://myschool.instructure.com")
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS):
        raise ConfigError(f"{ENV_BASE_URL} must use https (the token is sent with every request)")
    return f"{parts.scheme}://{parts.netloc}"


def origin_of(url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit(url)
    port = parts.port or {"https": 443, "http": 80}.get(parts.scheme)
    return parts.scheme, (parts.hostname or "").lower(), port


def encode_params(params: Mapping[str, Any]) -> str:
    """Form/query encoding with Canvas conventions: lists repeat the key (`include[]=a&include[]=b`)."""
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            if item is None:
                continue
            pairs.append((key, "true" if item is True else "false" if item is False else str(item)))
    return urllib.parse.urlencode(pairs)


def next_link(link_header: str) -> str | None:
    """Return the rel="next" URL from a Link header (file.pagination.html)."""
    for url, rels in re.findall(r'<([^>]+)>\s*;\s*rel="([^"]+)"', link_header or ""):
        if "next" in rels.split():
            return url
    return None


def safe_filename(name: str) -> str:
    """Reduce a server-provided name to a safe single path component."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base).strip(" .")
    if not base:
        return "download"
    if base.split(".")[0].upper() in WINDOWS_RESERVED_NAMES:
        base = "_" + base
    return base


def text_to_html(text: str) -> str:
    """Plain text -> simple HTML: blank lines split paragraphs, single newlines become <br>."""
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "\n".join("<p>" + html.escape(p.strip(), quote=False).replace("\n", "<br>") + "</p>" for p in paragraphs)


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = frozenset({"p", "div", "br", "tr", "ul", "ol", "table", "section", "article", "blockquote",
                            "pre", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "header", "footer", "dd", "dt"})
    SKIP_TAGS = frozenset({"script", "style", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._links: list[str | None] = []
        self._row_has_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self._skip += 1
        elif tag in ("td", "th"):  # keep cells apart: syllabus tables hold grade weights and dates
            if self._row_has_cell:
                self.parts.append(" | ")
            self._row_has_cell = True
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BLOCK_TAGS:
            if tag == "tr":
                self._row_has_cell = False
            self.parts.append("\n")
        elif tag == "a":
            self._links.append(dict(attrs).get("href"))
        elif tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(f"[image: {alt}]")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK_TAGS or tag == "li":
            self.parts.append("\n")
        elif tag == "a" and self._links:
            href = self._links.pop()
            if href and href.startswith(("http://", "https://", "mailto:")):
                self.parts.append(f" ({href})")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(value: str | None) -> str:
    """Readable plain text from Canvas HTML (descriptions, pages, messages)."""
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    lines = (re.sub(r"[ \t\r\f\v ]+", " ", line).strip() for line in "".join(parser.parts).split("\n"))
    return "\n".join(line for line in lines if line.strip("| "))


def pick(obj: Mapping[str, Any] | None, keys: Sequence[str]) -> JsonDict:
    """Keep only the listed keys that are present and not None."""
    if not obj:
        return {}
    return {k: obj[k] for k in keys if obj.get(k) is not None}


def parse_kv(pairs: Sequence[str] | None) -> JsonDict:
    """KEY=VALUE list -> dict; repeated keys or keys ending in [] become lists."""
    result: JsonDict = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise CanvasError(f"Expected KEY=VALUE, got {pair!r}")
        key, value = pair.split("=", 1)
        if key in result:
            existing = result[key]
            result[key] = (existing if isinstance(existing, list) else [existing]) + [value]
        else:
            result[key] = [value] if key.endswith("[]") else value
    return result


def today() -> dt.date:
    return dt.date.today()


def read_text_file(path: str) -> str:
    """Text of a file the user wrote on Windows: UTF-8 with or without BOM, or UTF-16 with BOM.

    Notepad and PowerShell 5.1 `Out-File -Encoding utf8` add a UTF-8 BOM; PowerShell 5.1 `>` writes UTF-16.
    """
    raw = Path(path).read_bytes()
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def parse_timestamp(value: Any) -> dt.datetime | None:
    """Canvas ISO 8601 timestamp (`...Z`) as an aware datetime, or None when absent or malformed."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def quiz_block_reason(method: str, url: str) -> str | None:
    """Why D11 refuses this request (quiz taking or quiz questions), or None if it is allowed."""
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    path = posixpath.normpath(re.sub(r"/+", "/", path))  # `a//b`, `x/../y` and %2F cannot dodge the check
    patterns = QUIZ_BLOCKED_ANY_METHOD + (QUIZ_BLOCKED_WRITES if method != "GET" else ())
    if any(p.search(path) for p in patterns):
        return (f"Refusing {method} {path}: starting, answering or completing quizzes and reading quiz questions "
                "are blocked for academic integrity. Quiz settings and your scores are available with `quiz`.")
    return None


def iso_date(value: str) -> str:
    """Validate YYYY-MM-DD or a full ISO 8601 timestamp."""
    try:
        if len(value) == 10:
            dt.date.fromisoformat(value)
        else:
            dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date {value!r}; use YYYY-MM-DD or ISO 8601") from exc
    return value


# --------------------------------------------------------------------------- HTTP client

@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        if not self.body.strip():
            return None
        try:
            return json.loads(self.body)
        except ValueError as exc:
            snippet = self.body[:120].decode("utf-8", "replace").strip()
            raise CanvasError(f"Canvas returned a response that is not JSON (HTTP {self.status}): {snippet!r}",
                              self.status) from exc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface 3xx responses so redirects are followed explicitly, never leaking the token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class CanvasClient:
    def __init__(self, base_url: str, token: str, *, sleep: Callable[[float], None] = time.sleep,
                 timeout: float = REQUEST_TIMEOUT_S) -> None:
        self.base_url = normalize_base_url(base_url)
        self._origin = origin_of(self.base_url)
        self._token = token
        self._sleep = sleep
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)
        self.warnings: list[str] = []  # printed to stderr by main() after the command

    # -- URL handling
    def url_for(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            if origin_of(path) != self._origin:
                raise CanvasError(f"Refusing to send the Canvas token to a different host: "
                                  f"{urllib.parse.urlsplit(path).netloc}")
            return path
        relative = path.lstrip("/")
        if not relative.startswith("api/"):
            relative = "api/v1/" + relative
        return f"{self.base_url}/{relative}"

    def checked_url(self, method: str, path: str) -> str:
        """url_for plus the D11 quiz guard; used for every request and before showing a dry run."""
        url = self.url_for(path)
        reason = quiz_block_reason(method, url)
        if reason:
            raise CanvasError(reason)
        return url

    def _auth_headers(self, url: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if origin_of(url) == self._origin else {}

    def redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    # -- transport
    def _send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> Response:
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with self._opener.open(request, timeout=self._timeout) as resp:
                return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return Response(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read())
        except urllib.error.URLError as exc:
            raise CanvasError(f"Network error contacting {urllib.parse.urlsplit(url).netloc}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise CanvasError(f"Timed out contacting {urllib.parse.urlsplit(url).netloc}") from exc
        except (OSError, http.client.HTTPException) as exc:
            # urllib wraps only errors raised while sending; a reset or truncated answer surfaces raw.
            raise CanvasError(f"Network error contacting {urllib.parse.urlsplit(url).netloc}: "
                              f"{exc or type(exc).__name__}") from exc

    @staticmethod
    def _is_throttled(resp: Response) -> bool:
        return resp.status == 429 or (resp.status == 403 and b"rate limit exceeded" in resp.body.lower())

    @staticmethod
    def _retry_delay(resp: Response, attempt: int) -> float:
        retry_after = resp.headers.get("retry-after", "")
        if retry_after.isdigit():
            return min(int(retry_after), MAX_RETRY_AFTER_S)
        return 2 ** attempt

    def request(self, method: str, path: str, *, params: Mapping[str, Any] | None = None,
                data: Mapping[str, Any] | None = None, json_body: Any = None) -> Response:
        url = self.checked_url(method, path)
        if params:
            url += ("&" if "?" in url else "?") + encode_params(params)
        headers = {"Accept": "application/json", **self._auth_headers(url)}
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif data is not None:
            body = encode_params(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif method in ("POST", "PUT"):
            body = b""
        resp = self._send(method, url, headers, body)
        for attempt in range(MAX_THROTTLE_RETRIES):
            if not self._is_throttled(resp):
                break
            self._sleep(self._retry_delay(resp, attempt))
            resp = self._send(method, url, headers, body)
        redirects = 0
        while method == "GET" and resp.status in REDIRECT_STATUSES and redirects < MAX_REDIRECTS:
            url = urllib.parse.urljoin(url, resp.headers.get("location", ""))
            resp = self._send("GET", url, {"Accept": "application/json", **self._auth_headers(url)})
            redirects += 1
        if resp.status >= 400:
            raise CanvasError(self.redact(self._error_message(resp)), resp.status)
        return resp

    @classmethod
    def _error_message(cls, resp: Response) -> str:
        detail = ""
        try:
            payload = json.loads(resp.body)
        except ValueError:
            detail = resp.body.decode("utf-8", "replace").strip()[:300]
        else:
            if isinstance(payload, dict):
                errors = payload.get("errors")
                if isinstance(errors, list):
                    detail = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors)
                elif errors:
                    detail = json.dumps(errors, ensure_ascii=False)
                else:
                    detail = str(payload.get("message") or payload.get("error") or "")
        message = f"HTTP {resp.status}" + (f": {detail}" if detail else "")
        if cls._is_throttled(resp):
            message += (f" (Canvas rate limit: still throttled after {MAX_THROTTLE_RETRIES} retries; "
                        "wait a minute, then run one command at a time)")
        elif resp.status == 401:
            message += (" (the token is invalid, expired or revoked; create a new one in Canvas)"
                        if "www-authenticate" in resp.headers else " (you do not have permission for this)")
        elif resp.status == 403:
            message += " (forbidden: not allowed for students, or the item is locked)"
        elif resp.status == 404:
            message += " (not found, or not visible to you)"
        return message

    # -- convenience
    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        return self.request(method, path, **kwargs).json()

    def get_all(self, path: str, params: Mapping[str, Any] | None = None, *, limit: int | None = None,
                key: str | None = None) -> Any:
        """GET every page by following Link rel="next" (opaque URLs, file.pagination.html).

        Non-list responses are returned as-is. When `limit` cuts the results short, a warning is recorded.
        """
        query = {**(params or {})}
        try:
            per_page = int(query.get("per_page", MAX_PER_PAGE))
        except (TypeError, ValueError) as exc:
            raise CanvasError(f"per_page must be an integer, got {query.get('per_page')!r}") from exc
        query["per_page"] = max(1, min(per_page, MAX_PER_PAGE))
        items: list[Any] = []
        url: str | None = path
        first = True
        followed: set[str] = set()
        while url:
            resp = self.request("GET", url, params=query if first else None)
            first = False
            page = resp.json()
            if key and isinstance(page, dict):
                page = page.get(key, [])
            if not isinstance(page, list):
                return page
            items.extend(page)
            url = next_link(resp.headers.get("link", ""))
            if url in followed:  # a repeated rel="next" would otherwise loop forever
                raise CanvasError(f"Canvas returned a pagination loop for {path}; results would be incomplete")
            if url:
                followed.add(url)
            if limit is not None and len(items) >= limit:
                if len(items) > limit or url:
                    self.warnings.append(f"Showing the first {limit} results; more exist. Raise --limit to see them.")
                return items[:limit]
        return items

    def upload_file(self, endpoint: str, file_path: Path, extra: Mapping[str, Any] | None = None) -> JsonDict:
        """Canvas 3-step upload (file.file_uploads.html). Returns the File JSON (with `id`)."""
        content = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        step1 = self.call("POST", endpoint, data={
            "name": file_path.name, "size": len(content), "content_type": content_type, **(extra or {})})
        upload_url = step1.get("upload_url") if isinstance(step1, dict) else None
        if not upload_url:
            raise CanvasError("Canvas did not return an upload_url for the file upload")
        body, boundary = encode_multipart(step1.get("upload_params") or {}, file_path.name, content_type, content)
        # Step 2: the token is NOT sent to the upload URL (it may be a third-party storage host).
        resp = self._send("POST", upload_url, {"Content-Type": f"multipart/form-data; boundary={boundary}"}, body)
        location = urllib.parse.urljoin(upload_url, resp.headers.get("location", "")) if resp.headers.get("location") else ""
        if resp.status in REDIRECT_STATUSES and location:
            return self.call("GET", location)  # Step 3: authenticated confirmation on Canvas
        if resp.status in (200, 201):
            if location and origin_of(location) == self._origin:
                return self.call("GET", location)
            payload = resp.json() if resp.body.strip() else None
            if isinstance(payload, dict) and "id" in payload:
                return payload
        raise CanvasError(f"File upload failed: {self.redact(self._error_message(resp))}", resp.status)

    def download(self, url: str, target: Path) -> int:
        """Stream a file to `target`, sending the token only to the Canvas host."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            scheme, host, _port = origin_of(current)
            if scheme != "https" and host not in LOCAL_HOSTS:
                raise CanvasError(f"Refusing non-https download URL on {host}")
            request = urllib.request.Request(current, headers=self._auth_headers(current))
            try:
                with self._opener.open(request, timeout=self._timeout) as resp:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
                        try:
                            shutil.copyfileobj(resp, tmp)
                        except BaseException:
                            tmp.close()
                            Path(tmp.name).unlink(missing_ok=True)
                            raise
                    Path(tmp.name).replace(target)
                    return target.stat().st_size
            except urllib.error.HTTPError as exc:
                with exc:
                    if exc.code in REDIRECT_STATUSES and exc.headers.get("Location"):
                        current = urllib.parse.urljoin(current, exc.headers["Location"])
                        continue
                    resp = Response(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read())
                raise CanvasError(f"Download failed: {self.redact(self._error_message(resp))}", exc.code) from exc
            except urllib.error.URLError as exc:
                raise CanvasError(f"Network error during download: {exc.reason}") from exc
            except (ConnectionError, TimeoutError, http.client.HTTPException) as exc:
                # Network failures after sending; local file errors (other OSErrors) still reach main().
                raise CanvasError(f"Network error during download: {exc or type(exc).__name__}") from exc
        raise CanvasError("Download failed: too many redirects")


def encode_multipart(fields: Mapping[str, Any], filename: str, content_type: str,
                     content: bytes) -> tuple[bytes, str]:
    """multipart/form-data with upload_params unchanged and the `file` field last (required by Canvas)."""
    boundary = f"----canvas{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    quoted = filename.replace('"', "%22")
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{quoted}"\r\n'
                  f"Content-Type: {content_type}\r\n\r\n".encode())
    chunks.append(content)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


# --------------------------------------------------------------------------- write safety

@dataclass
class Plan:
    """A change to Canvas, shown as a dry run unless --confirm is given."""
    summary: str
    requests: list[str]
    execute: Callable[[], Any]


def run_plan(plan: Plan, confirm: bool) -> JsonDict:
    if not confirm:
        return {"dry_run": True, "summary": plan.summary, "requests": plan.requests,
                "next_step": "Show this to the user and rerun with --confirm only after they explicitly approve it."}
    return {"done": True, "summary": plan.summary, "result": plan.execute()}


# --------------------------------------------------------------------------- slimming

COURSE_KEYS = ["id", "name", "course_code", "workflow_state", "start_at", "end_at", "access_restricted_by_date"]
ASSIGNMENT_KEYS = ["id", "name", "due_at", "unlock_at", "lock_at", "points_possible", "submission_types",
                   "locked_for_user", "html_url", "quiz_id", "discussion_topic"]
SUBMISSION_KEYS = ["workflow_state", "submitted_at", "score", "grade", "late", "missing", "excused", "attempt",
                   "submission_type", "graded_at", "posted_at", "points_deducted", "late_policy_status"]


def slim_assignment(a: JsonDict) -> JsonDict:
    out = pick(a, ASSIGNMENT_KEYS)
    if "discussion_topic" in out:
        out["discussion_topic"] = pick(out["discussion_topic"], ["id", "title"])
    if a.get("submission"):
        out["submission"] = pick(a["submission"], SUBMISSION_KEYS)
    return out


def slim_rubric(rubric: list[JsonDict] | None) -> list[JsonDict]:
    return [{**pick(c, ["id", "description", "long_description", "points"]),
             "ratings": [pick(r, ["description", "points", "long_description"]) for r in c.get("ratings") or []]}
            for c in rubric or []]


def slim_submission(s: JsonDict) -> JsonDict:
    out = pick(s, SUBMISSION_KEYS + ["url", "preview_url", "seconds_late"])
    if s.get("body"):
        out["body"] = html_to_text(s["body"])
    if s.get("attachments"):
        out["attachments"] = [pick(f, ["id", "display_name", "size", "url"]) for f in s["attachments"]]
    if s.get("submission_comments"):
        out["comments"] = [{**pick(c, ["id", "author_name", "created_at", "attempt"]),
                            "comment": c.get("comment", "")} for c in s["submission_comments"]]
    if s.get("rubric_assessment"):
        out["rubric_assessment"] = s["rubric_assessment"]
    if s.get("submission_history"):
        out["history"] = [pick(h, ["attempt", "submitted_at", "score", "grade", "submission_type", "workflow_state"])
                          for h in s["submission_history"]]
    return out


def maybe(full: bool, raw: Any, slimmer: Callable[[Any], Any]) -> Any:
    if full:
        return raw
    if isinstance(raw, list):
        return [slimmer(x) for x in raw]
    return slimmer(raw)


# --------------------------------------------------------------------------- commands: profile & courses

def cmd_whoami(c: CanvasClient, a: argparse.Namespace) -> Any:
    profile = c.call("GET", "users/self/profile")
    return maybe(a.full, profile, lambda p: pick(p, ["id", "name", "short_name", "primary_email", "login_id",
                                                      "time_zone", "locale"]))


def _active_course_ids(c: CanvasClient) -> list[str]:
    courses = c.get_all("courses", {"enrollment_type": "student", "enrollment_state": "active"})
    return [str(x["id"]) for x in courses if not x.get("access_restricted_by_date")]


def cmd_courses(c: CanvasClient, a: argparse.Namespace) -> Any:
    params: JsonDict = {"enrollment_type": "student",
                        "include[]": ["term", "total_scores", "current_grading_period_scores"]}
    if a.state != "active":
        # courses.html: state[] defaults to "available" for students, which would hide concluded courses.
        params["state[]"] = ["available", "completed"]
    if a.state != "all":
        params["enrollment_state"] = a.state

    def slim(course: JsonDict) -> JsonDict:
        out = pick(course, COURSE_KEYS)
        if course.get("term"):
            out["term"] = course["term"].get("name")
        for enrollment in course.get("enrollments") or []:
            if enrollment.get("type") in ("student", "StudentEnrollment"):
                grade = pick(enrollment, ["computed_current_score", "computed_current_grade", "computed_final_score",
                                          "computed_final_grade", "current_grading_period_title",
                                          "current_period_computed_current_score"])
                if grade:
                    out["grade"] = grade
        return out

    return maybe(a.full, c.get_all("courses", params, limit=a.limit), slim)


def cmd_course(c: CanvasClient, a: argparse.Namespace) -> Any:
    course = c.call("GET", f"courses/{a.course}",
                    params={"include[]": ["syllabus_body", "term", "teachers", "course_progress"]})
    if a.full:
        return course
    out = pick(course, COURSE_KEYS + ["time_zone", "default_view", "course_progress"])
    if course.get("term"):
        out["term"] = course["term"].get("name")
    if course.get("teachers"):
        out["teachers"] = [pick(t, ["id", "display_name"]) for t in course["teachers"]]
    if course.get("syllabus_body"):
        out["syllabus"] = html_to_text(course["syllabus_body"])
    return out


def cmd_tabs(c: CanvasClient, a: argparse.Namespace) -> Any:
    tabs = c.get_all(f"courses/{a.course}/tabs")
    if a.full:
        return tabs
    return [pick(t, ["id", "label", "type", "html_url"]) for t in tabs if not t.get("hidden")]


def cmd_grades(c: CanvasClient, a: argparse.Namespace) -> Any:
    params: JsonDict = {"type[]": ["StudentEnrollment"], "include[]": ["current_points"],
                        "state[]": ["active", "completed"] if a.include_completed else ["active"]}
    if a.grading_period:
        params["grading_period_id"] = a.grading_period
    enrollments = c.get_all("users/self/enrollments", params)
    if a.course:
        enrollments = [e for e in enrollments if str(e.get("course_id")) == str(a.course)]
    if a.full:
        return enrollments
    names = {str(x.get("id")): x.get("name") for x in
             c.get_all("courses", {"enrollment_type": "student", "state[]": ["available", "completed"]})}
    return [{"course_id": e.get("course_id"), "course_name": names.get(str(e.get("course_id"))),
             "enrollment_state": e.get("enrollment_state"),
             **pick(e.get("grades"), ["current_score", "current_grade", "final_score", "final_grade",
                                      "current_points", "html_url"]),
             **pick(e, ["current_grading_period_title"])} for e in enrollments]


def cmd_grading_periods(c: CanvasClient, a: argparse.Namespace) -> Any:
    return c.get_all(f"courses/{a.course}/grading_periods", key="grading_periods")


# --------------------------------------------------------------------------- commands: assignments & submissions

def cmd_assignments(c: CanvasClient, a: argparse.Namespace) -> Any:
    params: JsonDict = {"include[]": ["submission"], "order_by": "due_at"}
    if a.bucket:
        params["bucket"] = a.bucket
    if a.search:
        params["search_term"] = a.search
    return maybe(a.full, c.get_all(f"courses/{a.course}/assignments", params, limit=a.limit), slim_assignment)


def cmd_assignment(c: CanvasClient, a: argparse.Namespace) -> Any:
    asg = c.call("GET", f"courses/{a.course}/assignments/{a.assignment}", params={"include[]": ["submission"]})
    if a.full:
        return asg
    out = slim_assignment(asg)
    out.update(pick(asg, ["grading_type", "allowed_extensions", "allowed_attempts", "lock_explanation",
                          "peer_reviews", "group_category_id", "omit_from_final_grade", "has_submitted_submissions"]))
    if asg.get("description"):
        out["description"] = html_to_text(asg["description"])
    if asg.get("rubric"):
        out["rubric"] = slim_rubric(asg["rubric"])
    return out


def _submission_path(course: str, assignment: str) -> str:
    return f"courses/{course}/assignments/{assignment}/submissions/self"


def cmd_submission(c: CanvasClient, a: argparse.Namespace) -> Any:
    include = ["submission_comments", "rubric_assessment"] + (["submission_history"] if a.history else [])
    sub = c.call("GET", _submission_path(a.course, a.assignment), params={"include[]": include})
    return maybe(a.full, sub, slim_submission)


def cmd_submissions(c: CanvasClient, a: argparse.Namespace) -> Any:
    params: JsonDict = {"include[]": ["assignment"]}
    if a.workflow_state:
        params["workflow_state"] = a.workflow_state
    if a.graded_since:
        params["graded_since"] = a.graded_since

    def slim(s: JsonDict) -> JsonDict:
        return {"assignment_id": s.get("assignment_id"), "assignment": (s.get("assignment") or {}).get("name"),
                "due_at": (s.get("assignment") or {}).get("due_at"), **pick(s, SUBMISSION_KEYS)}

    return maybe(a.full, c.get_all(f"courses/{a.course}/students/submissions", params, limit=a.limit), slim)


def _resolve_files(paths: Sequence[str]) -> list[Path]:
    resolved = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise CanvasError(f"File not found: {path}")
        resolved.append(path.resolve())
    return resolved


def cmd_submit(c: CanvasClient, a: argparse.Namespace) -> Any:
    asg = c.call("GET", f"courses/{a.course}/assignments/{a.assignment}", params={"include[]": ["submission"]})
    accepted = asg.get("submission_types") or []
    if a.file:
        stype = "online_upload"
    elif a.url:
        stype = "online_url"
    else:
        stype = "online_text_entry"
    if stype not in accepted:
        raise CanvasError(f"This assignment accepts {accepted}, not {stype}")
    if asg.get("locked_for_user"):
        raise CanvasError(f"Assignment is locked: {asg.get('lock_explanation') or 'no explanation given'}")
    data: JsonDict = {"submission[submission_type]": stype}
    files: list[Path] = []
    if stype == "online_upload":
        files = _resolve_files(a.file)
        allowed = [ext.lower().lstrip(".") for ext in asg.get("allowed_extensions") or []]
        bad = [f.name for f in files if allowed and f.suffix.lower().lstrip(".") not in allowed]
        if bad:
            raise CanvasError(f"Extension not allowed for {bad}; this assignment accepts: {allowed}")
    elif stype == "online_url":
        if not a.url.startswith(("http://", "https://")):
            raise CanvasError("--url must start with http:// or https://")
        data["submission[url]"] = a.url
    else:
        try:
            text = read_text_file(a.text_file) if a.text_file else a.text
        except UnicodeDecodeError as exc:
            raise CanvasError(f"{a.text_file} is not valid UTF-8 or UTF-16 text; save it as UTF-8 and retry") from exc
        if not text.strip():
            raise CanvasError("The text to submit is empty")
        data["submission[body]"] = text if a.html else text_to_html(text)
    if a.comment:
        data["comment[text_comment]"] = a.comment
    endpoint = f"courses/{a.course}/assignments/{a.assignment}/submissions"
    requests = [f"POST {_submission_path(a.course, a.assignment)}/files + upload: {f.name} ({f.stat().st_size} bytes)"
                for f in files]
    preview = {k: (v[:200] + "..." if isinstance(v, str) and len(v) > 200 else v) for k, v in data.items()}
    requests.append(f"POST {endpoint} {json.dumps(preview, ensure_ascii=False)}")

    def execute() -> Any:
        if files:
            data["submission[file_ids][]"] = [
                c.upload_file(f"{_submission_path(a.course, a.assignment)}/files", f)["id"] for f in files]
        return slim_submission(c.call("POST", endpoint, data=data))

    submission = asg.get("submission") or {}
    previous = submission.get("attempt")
    summary = f"Submit {stype} to assignment {a.assignment} ('{asg.get('name')}') in course {a.course}"
    if previous:
        summary += f". This is a RESUBMISSION (attempt {previous + 1})"
        allowed = asg.get("allowed_attempts")  # assignments.html: -1 is unlimited
        if isinstance(allowed, int) and allowed > 0:
            total = allowed + (submission.get("extra_attempts") or 0)
            summary += f" of {total} allowed"
            if previous >= total:
                summary += "; WARNING: no attempts left, Canvas will likely reject it"
    if asg.get("group_category_id"):  # assignments.html: set "if this is a group assignment"
        summary += "; WARNING: group assignment, the submission may count for your whole group"
    due = parse_timestamp(asg.get("due_at"))  # assignments.html: due_at already reflects your overrides
    if due and due < dt.datetime.now(dt.timezone.utc):
        summary += f"; LATE: it was due at {asg['due_at']}, so Canvas will likely mark it late"
    return run_plan(Plan(summary, requests, execute), a.confirm)


def cmd_comment(c: CanvasClient, a: argparse.Namespace) -> Any:
    path = _submission_path(a.course, a.assignment)
    files = _resolve_files(a.file or [])
    data: JsonDict = {"comment[text_comment]": a.text}
    if a.attempt:
        data["comment[attempt]"] = a.attempt
    requests = [f"POST {path}/comments/files + upload: {f.name}" for f in files]
    requests.append(f"PUT {path} comment[text_comment]={a.text[:200]!r}")

    def execute() -> Any:
        if files:
            data["comment[file_ids][]"] = [c.upload_file(f"{path}/comments/files", f)["id"] for f in files]
        return slim_submission(c.call("PUT", path, data=data))

    return run_plan(Plan(f"Add a comment to your submission for assignment {a.assignment}", requests, execute),
                    a.confirm)


def cmd_peer_reviews(c: CanvasClient, a: argparse.Namespace) -> Any:
    reviews = c.get_all(f"courses/{a.course}/assignments/{a.assignment}/peer_reviews",
                        {"include[]": ["user", "submission_comments"]})
    return maybe(a.full, reviews, lambda r: {**pick(r, ["id", "assessor_id", "user_id", "asset_id", "workflow_state"]),
                                             "user": (r.get("user") or {}).get("display_name")})


# --------------------------------------------------------------------------- commands: overview

def cmd_todo(c: CanvasClient, a: argparse.Namespace) -> Any:
    items = c.get_all("users/self/todo", {"include[]": ["ungraded_quizzes"]}, limit=a.limit)

    def slim(t: JsonDict) -> JsonDict:
        target = t.get("assignment") or t.get("quiz") or {}
        return {**pick(t, ["type", "course_id", "context_name", "html_url"]),
                **pick(target, ["id", "name", "title", "due_at", "points_possible"])}

    return maybe(a.full, items, slim)


def cmd_upcoming(c: CanvasClient, a: argparse.Namespace) -> Any:
    events = c.get_all("users/self/upcoming_events", limit=a.limit)
    return maybe(a.full, events, lambda e: {
        **pick(e, ["id", "title", "start_at", "end_at", "context_code", "context_name", "html_url", "location_name"]),
        **({"assignment_id": e["assignment"].get("id"), "due_at": e["assignment"].get("due_at")}
           if e.get("assignment") else {})})


def cmd_missing(c: CanvasClient, a: argparse.Namespace) -> Any:
    items = c.get_all("users/self/missing_submissions",
                      {"include[]": ["course", "planner_overrides"], "filter[]": ["submittable"]}, limit=a.limit)
    return maybe(a.full, items, lambda m: {**pick(m, ["id", "name", "due_at", "points_possible", "course_id", "html_url"]),
                                           "course": (m.get("course") or {}).get("name")})


def _plannable_title(p: JsonDict) -> Any:
    return p.get("title") or p.get("name")


def cmd_planner(c: CanvasClient, a: argparse.Namespace) -> Any:
    start = a.start or today().isoformat()
    end = a.end or (today() + dt.timedelta(days=DEFAULT_PLANNER_DAYS)).isoformat()
    params: JsonDict = {"start_date": start, "end_date": end}
    if a.filter:
        params["filter"] = a.filter
    if a.course:
        params["context_codes[]"] = [f"course_{x}" for x in a.course]
    items = c.get_all("planner/items", params, limit=a.limit)

    def slim(item: JsonDict) -> JsonDict:
        plannable = item.get("plannable") or {}
        out = {**pick(item, ["plannable_type", "plannable_id", "course_id", "context_name", "plannable_date",
                             "html_url", "new_activity"]),
               "title": _plannable_title(plannable), **pick(plannable, ["due_at", "todo_date", "points_possible"])}
        if isinstance(item.get("submissions"), dict):
            out["status"] = {k: v for k, v in item["submissions"].items() if v}
        if item.get("planner_override"):
            out["marked_complete"] = item["planner_override"].get("marked_complete")
        return out

    return maybe(a.full, items, slim)


def cmd_planner_done(c: CanvasClient, a: argparse.Namespace) -> Any:
    complete = not a.undo

    def execute() -> Any:
        overrides = c.get_all("planner/overrides")
        match = next((o for o in overrides if o.get("plannable_type") == a.type
                      and str(o.get("plannable_id")) == str(a.id)), None)
        if match:
            return c.call("PUT", f"planner/overrides/{match['id']}", data={"marked_complete": complete})
        return c.call("POST", "planner/overrides",
                      data={"plannable_type": a.type, "plannable_id": a.id, "marked_complete": complete})

    return run_plan(Plan(f"Mark planner item {a.type} {a.id} as {'complete' if complete else 'not complete'} "
                         "(only affects your planner, not the submission)",
                         [f"POST or PUT planner/overrides marked_complete={str(complete).lower()}"], execute), a.confirm)


def cmd_planner_notes(c: CanvasClient, a: argparse.Namespace) -> Any:
    params: JsonDict = {}
    if a.start:
        params["start_date"] = a.start
    if a.end:
        params["end_date"] = a.end
    notes = c.get_all("planner_notes", params, limit=a.limit)
    return maybe(a.full, notes, lambda n: {**pick(n, ["id", "title", "todo_date", "course_id", "workflow_state"]),
                                           "details": n.get("details") or n.get("description")})


def cmd_planner_note_add(c: CanvasClient, a: argparse.Namespace) -> Any:
    data: JsonDict = {"title": a.title, "todo_date": a.date}
    if a.details:
        data["details"] = a.details
    if a.course:
        data["course_id"] = a.course
    return run_plan(Plan(f"Create planner note '{a.title}' on {a.date}",
                         [f"POST planner_notes {json.dumps(data, ensure_ascii=False)}"],
                         lambda: c.call("POST", "planner_notes", data=data)), a.confirm)


def cmd_planner_note_delete(c: CanvasClient, a: argparse.Namespace) -> Any:
    return run_plan(Plan(f"Delete planner note {a.id}", [f"DELETE planner_notes/{a.id}"],
                         lambda: c.call("DELETE", f"planner_notes/{a.id}")), a.confirm)


def _self_id(c: CanvasClient) -> str:
    return str(c.call("GET", "users/self")["id"])


def cmd_calendar(c: CanvasClient, a: argparse.Namespace) -> Any:
    codes = [f"user_{_self_id(c)}"]
    codes += [f"course_{x}" for x in (a.course or _active_course_ids(c))]
    if len(codes) > CALENDAR_CONTEXT_LIMIT:
        c.warnings.append(f"Canvas accepts at most {CALENDAR_CONTEXT_LIMIT} calendars per request; showing the first "
                  f"{CALENDAR_CONTEXT_LIMIT - 1} courses. Use --course to choose specific courses.")
        codes = codes[:CALENDAR_CONTEXT_LIMIT]
    start = a.start or today().isoformat()
    end = a.end or (today() + dt.timedelta(days=DEFAULT_PLANNER_DAYS)).isoformat()
    types = ["event", "assignment"] if a.type == "all" else [a.type]
    events: list[JsonDict] = []
    for event_type in types:
        events += c.get_all("calendar_events", {"type": event_type, "start_date": start, "end_date": end,
                                                "context_codes[]": codes}, limit=a.limit)
    events.sort(key=lambda e: e.get("start_at") or "")
    if a.limit is not None and len(events) > a.limit:
        c.warnings.append(f"Showing the first {a.limit} results; more exist. Raise --limit to see them.")
        events = events[:a.limit]
    return maybe(a.full, events, lambda e: {
        **pick(e, ["id", "title", "start_at", "end_at", "all_day", "context_code", "context_name",
                   "location_name", "html_url"]),
        **({"description": html_to_text(e["description"])[:500]} if e.get("description") else {})})


def cmd_event_add(c: CanvasClient, a: argparse.Namespace) -> Any:
    data: JsonDict = {"calendar_event[title]": a.title, "calendar_event[start_at]": a.start}
    if a.end:
        data["calendar_event[end_at]"] = a.end
    if a.description:
        data["calendar_event[description]"] = a.description
    if a.location:
        data["calendar_event[location_name]"] = a.location

    def execute() -> Any:
        data["calendar_event[context_code]"] = f"user_{_self_id(c)}"
        return c.call("POST", "calendar_events", data=data)

    return run_plan(Plan(f"Create personal calendar event '{a.title}' at {a.start}",
                         [f"POST calendar_events {json.dumps(data, ensure_ascii=False)}"], execute), a.confirm)


def cmd_event_delete(c: CanvasClient, a: argparse.Namespace) -> Any:
    return run_plan(Plan(f"Delete calendar event {a.id}", [f"DELETE calendar_events/{a.id}"],
                         lambda: c.call("DELETE", f"calendar_events/{a.id}")), a.confirm)


def cmd_activity(c: CanvasClient, a: argparse.Namespace) -> Any:
    if a.summary:
        return c.call("GET", "users/self/activity_stream/summary", params={"only_active_courses": True})
    items = c.get_all("users/self/activity_stream", {"only_active_courses": True},
                      limit=a.limit or DEFAULT_STREAM_LIMIT)
    return maybe(a.full, items, lambda i: {
        **pick(i, ["id", "type", "title", "course_id", "created_at", "read_state", "html_url"]),
        "message": html_to_text(i.get("message"))[:300]})


# --------------------------------------------------------------------------- commands: content

def _slim_module_item(i: JsonDict) -> JsonDict:
    out = pick(i, ["id", "title", "type", "content_id", "page_url", "external_url", "html_url", "indent"])
    if i.get("completion_requirement"):
        out["requirement"] = i["completion_requirement"]
    if i.get("content_details"):
        out.update(pick(i["content_details"], ["due_at", "points_possible", "locked_for_user"]))
    return out


def cmd_modules(c: CanvasClient, a: argparse.Namespace) -> Any:
    modules = c.get_all(f"courses/{a.course}/modules", {"include[]": ["items", "content_details"]})
    for module in modules:
        if module.get("items") is None and module.get("items_url"):
            module["items"] = c.get_all(module["items_url"], {"include[]": ["content_details"]})
    return maybe(a.full, modules, lambda m: {
        **pick(m, ["id", "name", "position", "state", "unlock_at", "completed_at", "require_sequential_progress"]),
        "items": [_slim_module_item(i) for i in m.get("items") or []]})


def _module_item_path(a: argparse.Namespace) -> str:
    return f"courses/{a.course}/modules/{a.module}/items/{a.item}"


def cmd_module_done(c: CanvasClient, a: argparse.Namespace) -> Any:
    method = "DELETE" if a.undo else "PUT"
    path = f"{_module_item_path(a)}/done"
    return run_plan(Plan(f"Mark module item {a.item} as {'not done' if a.undo else 'done'}", [f"{method} {path}"],
                         lambda: c.call(method, path)), a.confirm)


def cmd_module_read(c: CanvasClient, a: argparse.Namespace) -> Any:
    path = f"{_module_item_path(a)}/mark_read"
    return run_plan(Plan(f"Mark module item {a.item} as viewed", [f"POST {path}"],
                         lambda: c.call("POST", path)), a.confirm)


def cmd_pages(c: CanvasClient, a: argparse.Namespace) -> Any:
    params: JsonDict = {"sort": "title"}
    if a.search:
        params["search_term"] = a.search
    return maybe(a.full, c.get_all(f"courses/{a.course}/pages", params, limit=a.limit),
                 lambda p: pick(p, ["page_id", "url", "title", "updated_at", "front_page", "locked_for_user"]))


def cmd_page(c: CanvasClient, a: argparse.Namespace) -> Any:
    if not a.page and not a.front:
        raise CanvasError("Give a page URL/id or --front")
    path = f"courses/{a.course}/front_page" if a.front else \
        f"courses/{a.course}/pages/{urllib.parse.quote(str(a.page), safe=':')}"
    page = c.call("GET", path)
    if a.full:
        return page
    out = pick(page, ["page_id", "url", "title", "updated_at", "locked_for_user", "lock_explanation", "html_url"])
    out["body"] = page.get("body", "") if a.html else html_to_text(page.get("body"))
    return out


FILE_KEYS = ["id", "display_name", "size", "content-type", "updated_at", "folder_id", "locked_for_user"]


def cmd_files(c: CanvasClient, a: argparse.Namespace) -> Any:
    if a.folder:
        path = f"folders/{a.folder}/files"
    elif a.course:
        path = f"courses/{a.course}/files"
    else:
        path = "users/self/files"
    params: JsonDict = {"sort": a.sort}
    if a.search:
        params["search_term"] = a.search
    if a.content_type:
        params["content_types[]"] = a.content_type
    return maybe(a.full, c.get_all(path, params, limit=a.limit), lambda f: pick(f, FILE_KEYS))


def cmd_folders(c: CanvasClient, a: argparse.Namespace) -> Any:
    path = f"courses/{a.course}/folders" if a.course else "users/self/folders"
    return maybe(a.full, c.get_all(path, limit=a.limit),
                 lambda f: pick(f, ["id", "full_name", "files_count", "folders_count", "parent_folder_id",
                                    "locked_for_user"]))


def cmd_download(c: CanvasClient, a: argparse.Namespace) -> Any:
    meta = c.call("GET", f"files/{a.file_id}")
    url = meta.get("url")
    if not url:
        raise CanvasError(f"File has no download URL: {meta.get('lock_explanation') or 'it may be locked'}")
    dest = Path(a.dest).expanduser().resolve()
    target = dest / safe_filename(meta.get("display_name") or meta.get("filename") or f"file-{a.file_id}")
    if target.exists() and not a.overwrite:
        raise CanvasError(f"{target} already exists; pass --overwrite to replace it")
    size = c.download(url, target)
    return {"path": str(target), "size": size, "content_type": meta.get("content-type")}


def cmd_quota(c: CanvasClient, a: argparse.Namespace) -> Any:
    return c.call("GET", "users/self/files/quota")


def cmd_upload(c: CanvasClient, a: argparse.Namespace) -> Any:
    path = _resolve_files([a.file])[0]
    extra: JsonDict = {"on_duplicate": "rename"}
    if a.folder_path:
        extra["parent_folder_path"] = a.folder_path
    endpoint = a.endpoint or "users/self/files"
    c.checked_url("POST", endpoint)  # validate the host and the quiz guard before showing a plan
    return run_plan(Plan(f"Upload {path.name} ({path.stat().st_size} bytes) to {endpoint}",
                         [f"POST {endpoint} + upload {json.dumps(extra)}"],
                         lambda: pick(c.upload_file(endpoint, path, extra), FILE_KEYS + ["url"])), a.confirm)


def cmd_announcements(c: CanvasClient, a: argparse.Namespace) -> Any:
    courses = a.course or _active_course_ids(c)
    if not courses:
        return []
    params: JsonDict = {"context_codes[]": [f"course_{x}" for x in courses],
                        "start_date": a.start or (today() - dt.timedelta(days=DEFAULT_ANNOUNCEMENT_DAYS)).isoformat()}
    if a.end:
        params["end_date"] = a.end
    else:
        params["end_date"] = (today() + dt.timedelta(days=1)).isoformat()
    items = c.get_all("announcements", params, limit=a.limit)
    return maybe(a.full, items, lambda n: {
        **pick(n, ["id", "title", "posted_at", "context_code", "read_state", "html_url"]),
        "author": (n.get("author") or {}).get("display_name"), "message": html_to_text(n.get("message"))})


def cmd_discussions(c: CanvasClient, a: argparse.Namespace) -> Any:
    params: JsonDict = {"order_by": "recent_activity"}
    if a.unread:
        params["filter_by"] = "unread"
    if a.search:
        params["search_term"] = a.search
    topics = c.get_all(f"courses/{a.course}/discussion_topics", params, limit=a.limit)
    return maybe(a.full, topics, lambda t: pick(t, [
        "id", "title", "posted_at", "last_reply_at", "discussion_subentry_count", "unread_count", "read_state",
        "locked_for_user", "require_initial_post", "assignment_id", "html_url"]))


def cmd_discussion(c: CanvasClient, a: argparse.Namespace) -> Any:
    base = f"courses/{a.course}/discussion_topics/{a.topic}"
    topic = c.call("GET", base)
    out = topic if a.full else {
        **pick(topic, ["id", "title", "posted_at", "assignment_id", "require_initial_post", "locked_for_user",
                       "html_url"]),
        "author": (topic.get("author") or {}).get("display_name"), "message": html_to_text(topic.get("message"))}
    try:
        view = c.call("GET", f"{base}/view")
    except CanvasError as exc:
        if exc.status == 403:
            out["entries_note"] = "You must post an initial reply before you can see other replies."
            return out
        if exc.status == 503:
            out["entries_note"] = "Canvas is still preparing this discussion; try again in a few seconds."
            return out
        raise
    if a.full:
        out["view"] = view
        return out
    names = {p.get("id"): p.get("display_name") for p in view.get("participants") or []}

    def thread(entry: JsonDict) -> JsonDict:
        node = {"id": entry.get("id"), "author": names.get(entry.get("user_id")), "created_at": entry.get("created_at"),
                "message": "[deleted]" if entry.get("deleted") else html_to_text(entry.get("message"))}
        if entry.get("replies"):
            node["replies"] = [thread(r) for r in entry["replies"]]
        return node

    out["entries"] = [thread(e) for e in view.get("view") or []]
    return out


def _message_html(a: argparse.Namespace) -> str:
    return a.message if a.html else text_to_html(a.message)


def cmd_discussion_post(c: CanvasClient, a: argparse.Namespace) -> Any:
    path = f"courses/{a.course}/discussion_topics/{a.topic}/entries"
    if a.reply_to:
        path += f"/{a.reply_to}/replies"
    data = {"message": _message_html(a)}
    target = f"reply to entry {a.reply_to}" if a.reply_to else "new entry"
    return run_plan(Plan(f"Post a {target} in discussion {a.topic} (visible to the class)",
                         [f"POST {path} message={a.message[:200]!r}"],
                         lambda: c.call("POST", path, data=data)), a.confirm)


def cmd_discussion_read(c: CanvasClient, a: argparse.Namespace) -> Any:
    path = f"courses/{a.course}/discussion_topics/{a.topic}/read_all"
    return run_plan(Plan(f"Mark discussion {a.topic} and all its entries as read", [f"PUT {path}"],
                         lambda: c.call("PUT", path)), a.confirm)


def cmd_quizzes(c: CanvasClient, a: argparse.Namespace) -> Any:
    if a.new:
        quizzes = c.get_all(f"/api/quiz/v1/courses/{a.course}/quizzes", limit=a.limit)
        return maybe(a.full, quizzes, lambda q: pick(q, ["id", "title", "due_at", "unlock_at", "lock_at",
                                                         "points_possible", "published"]))
    quizzes = c.get_all(f"courses/{a.course}/quizzes", limit=a.limit)
    return maybe(a.full, quizzes, lambda q: pick(q, [
        "id", "title", "quiz_type", "due_at", "unlock_at", "lock_at", "time_limit", "allowed_attempts",
        "question_count", "points_possible", "locked_for_user", "assignment_id", "html_url"]))


def cmd_quiz(c: CanvasClient, a: argparse.Namespace) -> Any:
    quiz = c.call("GET", f"courses/{a.course}/quizzes/{a.quiz}")
    attempts = c.get_all(f"courses/{a.course}/quizzes/{a.quiz}/submissions", key="quiz_submissions")
    if a.full:
        return {"quiz": quiz, "my_attempts": attempts}
    out = pick(quiz, ["id", "title", "quiz_type", "due_at", "unlock_at", "lock_at", "time_limit", "allowed_attempts",
                      "question_count", "points_possible", "scoring_policy", "one_question_at_a_time", "cant_go_back",
                      "hide_results", "show_correct_answers", "locked_for_user", "lock_explanation", "html_url"])
    if quiz.get("description"):
        out["description"] = html_to_text(quiz["description"])
    out["my_attempts"] = [pick(s, ["attempt", "workflow_state", "score", "kept_score", "started_at", "finished_at",
                                   "time_spent", "end_at"]) for s in attempts]
    return out


def cmd_groups(c: CanvasClient, a: argparse.Namespace) -> Any:
    return maybe(a.full, c.get_all("users/self/groups", limit=a.limit),
                 lambda g: pick(g, ["id", "name", "course_id", "members_count", "join_level", "description"]))


def cmd_group_members(c: CanvasClient, a: argparse.Namespace) -> Any:
    return maybe(a.full, c.get_all(f"groups/{a.group}/users", limit=a.limit),
                 lambda u: pick(u, ["id", "name", "short_name"]))


# --------------------------------------------------------------------------- commands: inbox

def cmd_inbox(c: CanvasClient, a: argparse.Namespace) -> Any:
    params: JsonDict = {}
    if a.scope:
        params["scope"] = a.scope
    items = c.get_all("conversations", params, limit=a.limit or DEFAULT_STREAM_LIMIT)
    return maybe(a.full, items, lambda m: {
        **pick(m, ["id", "subject", "workflow_state", "last_message_at", "message_count", "starred", "context_name"]),
        "last_message": (m.get("last_message") or "")[:200],
        "participants": [p.get("name") for p in m.get("participants") or []]})


def cmd_unread_count(c: CanvasClient, a: argparse.Namespace) -> Any:
    return c.call("GET", "conversations/unread_count")


def cmd_conversation(c: CanvasClient, a: argparse.Namespace) -> Any:
    conv = c.call("GET", f"conversations/{a.id}", params={"auto_mark_as_read": a.mark_read})
    if a.full:
        return conv
    names = {p.get("id"): p.get("name") for p in conv.get("participants") or []}
    return {**pick(conv, ["id", "subject", "workflow_state", "context_name", "starred"]),
            "participants": [{"id": k, "name": v} for k, v in names.items()],
            "messages": [{"id": m.get("id"), "created_at": m.get("created_at"), "author": names.get(m.get("author_id")),
                          "body": m.get("body"),
                          **({"attachments": [pick(f, ["id", "display_name", "url"]) for f in m["attachments"]]}
                             if m.get("attachments") else {})}
                         for m in conv.get("messages") or []]}


def cmd_recipients(c: CanvasClient, a: argparse.Namespace) -> Any:
    params: JsonDict = {"search": a.search}
    if a.context:
        params["context"] = a.context
    if a.type:
        params["type"] = a.type
    return maybe(a.full, c.get_all("search/recipients", params, limit=a.limit or 20),
                 lambda r: pick(r, ["id", "name", "type", "user_count", "common_courses"]))


def cmd_send(c: CanvasClient, a: argparse.Namespace) -> Any:
    if len(a.subject) > MAX_SUBJECT_LENGTH:
        raise CanvasError(f"The subject must be at most {MAX_SUBJECT_LENGTH} characters (it has {len(a.subject)})")
    data: JsonDict = {"recipients[]": a.to, "subject": a.subject, "body": a.body}
    if a.course:
        data["context_code"] = f"course_{a.course}"
    if a.group:
        data["group_conversation"] = True
    # conversations.html: a user is a numeric id or "uuid:..."; anything else (course_1, group_2...) is a context.
    broad = [r for r in a.to if not (str(r).isdigit() or str(r).startswith("uuid:"))]
    summary = f"Send message '{a.subject}' to {', '.join(map(str, a.to))}"
    if broad:
        summary += f". WARNING: {', '.join(broad)} may reach many people (a whole course, group or role)"
    return run_plan(Plan(summary, [f"POST conversations {json.dumps(data, ensure_ascii=False)}"],
                         lambda: c.call("POST", "conversations", data=data)), a.confirm)


def cmd_reply(c: CanvasClient, a: argparse.Namespace) -> Any:
    path = f"conversations/{a.id}/add_message"
    return run_plan(Plan(f"Reply in conversation {a.id}", [f"POST {path} body={a.body[:200]!r}"],
                         lambda: c.call("POST", path, data={"body": a.body})), a.confirm)


def cmd_conversation_update(c: CanvasClient, a: argparse.Namespace) -> Any:
    data: JsonDict = {}
    if a.state:
        data["conversation[workflow_state]"] = a.state
    if a.star is not None:
        data["conversation[starred]"] = a.star
    if not data:
        raise CanvasError("Nothing to update: pass --state and/or --star/--unstar")
    return run_plan(Plan(f"Update conversation {a.id}: {data}", [f"PUT conversations/{a.id}"],
                         lambda: c.call("PUT", f"conversations/{a.id}", data=data)), a.confirm)


# --------------------------------------------------------------------------- commands: settings & generic

def cmd_nicknames(c: CanvasClient, a: argparse.Namespace) -> Any:
    return c.get_all("users/self/course_nicknames")


def cmd_nickname(c: CanvasClient, a: argparse.Namespace) -> Any:
    path = f"users/self/course_nicknames/{a.course}"
    if a.clear:
        return run_plan(Plan(f"Remove nickname of course {a.course}", [f"DELETE {path}"],
                             lambda: c.call("DELETE", path)), a.confirm)
    if not a.set or not a.set.strip():
        raise CanvasError("Pass --set NAME or --clear")
    if len(a.set) >= MAX_NICKNAME_LENGTH:  # users.html: "non-empty and shorter than 60 characters"
        raise CanvasError(f"Nickname must be shorter than {MAX_NICKNAME_LENGTH} characters")
    return run_plan(Plan(f"Set nickname of course {a.course} to '{a.set}'", [f"PUT {path} nickname={a.set!r}"],
                         lambda: c.call("PUT", path, data={"nickname": a.set})), a.confirm)


def cmd_channels(c: CanvasClient, a: argparse.Namespace) -> Any:
    return maybe(a.full, c.get_all("users/self/communication_channels"),
                 lambda ch: pick(ch, ["id", "type", "address", "workflow_state", "position"]))


def cmd_get(c: CanvasClient, a: argparse.Namespace) -> Any:
    return c.get_all(a.path, parse_kv(a.param), limit=a.limit, key=a.key)


def cmd_api(c: CanvasClient, a: argparse.Namespace) -> Any:
    data = parse_kv(a.data)
    try:
        json_body = json.loads(a.json) if a.json else None
    except ValueError as exc:
        raise CanvasError(f"--json is not valid JSON: {exc}") from exc
    c.checked_url(a.method, a.path)  # validate the host and the quiz guard before showing a plan
    shown = json.dumps(json_body if json_body is not None else data, ensure_ascii=False)
    return run_plan(Plan(f"{a.method} {a.path}", [f"{a.method} {a.path} {shown}"],
                         lambda: c.call(a.method, a.path, data=None if json_body is not None else data,
                                        json_body=json_body)), a.confirm)


# --------------------------------------------------------------------------- CLI wiring

def warn(err: TextIO, message: str) -> None:
    err.write(json.dumps({"warning": message}, ensure_ascii=False) + "\n")


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def escape_path_ids(args: argparse.Namespace) -> None:
    """Escape user-supplied ids so `1/../x` cannot change the endpoint; `sis_course_id:X` stays readable."""
    for name in PATH_ID_ARGS:
        value = getattr(args, name, None)
        if isinstance(value, str):
            setattr(args, name, urllib.parse.quote(value, safe=":@"))
        elif isinstance(value, list):
            setattr(args, name, [urllib.parse.quote(str(v), safe=":@") for v in value])


class JsonArgumentParser(argparse.ArgumentParser):
    """Raise UsageError instead of printing usage and exiting 2, which callers read as a config error."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(f"{message}. {self.format_usage().strip()}")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--full", action="store_true", help="return the raw Canvas JSON instead of a compact view")
    common.add_argument("--limit", type=positive_int, help="maximum number of items to return for lists")
    write = argparse.ArgumentParser(add_help=False)
    write.add_argument("--confirm", action="store_true",
                       help="actually perform the change (without it a dry run is printed)")

    parser = JsonArgumentParser(prog="canvas_api.py", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, func: Callable, help_text: str, *, writes: bool = False) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text, parents=[common] + ([write] if writes else []))
        p.set_defaults(func=func)
        return p

    add("whoami", cmd_whoami, "your profile (also checks the token works)")
    p = add("courses", cmd_courses, "your courses with current grade")
    p.add_argument("--state", choices=["active", "completed", "invited_or_pending", "all"], default="active")
    add("course", cmd_course, "course details, teachers and syllabus").add_argument("course")
    add("tabs", cmd_tabs, "navigation tabs visible in a course").add_argument("course")
    p = add("grades", cmd_grades, "current and final grades per course")
    p.add_argument("--course")
    p.add_argument("--grading-period", help="grading period id (see grading-periods)")
    p.add_argument("--include-completed", action="store_true", help="include concluded courses")
    add("grading-periods", cmd_grading_periods, "grading periods of a course").add_argument("course")

    p = add("assignments", cmd_assignments, "assignments of a course with your submission status")
    p.add_argument("course")
    p.add_argument("--bucket", choices=["past", "overdue", "undated", "ungraded", "unsubmitted", "upcoming", "future"])
    p.add_argument("--search")
    p = add("assignment", cmd_assignment, "one assignment: description, rubric, accepted file types")
    p.add_argument("course")
    p.add_argument("assignment")
    p = add("submission", cmd_submission, "your submission: grade, comments, rubric assessment")
    p.add_argument("course")
    p.add_argument("assignment")
    p.add_argument("--history", action="store_true", help="include previous attempts")
    p = add("submissions", cmd_submissions, "all your submissions in a course")
    p.add_argument("course")
    p.add_argument("--workflow-state", choices=["submitted", "unsubmitted", "graded", "pending_review"])
    p.add_argument("--graded-since", type=iso_date)
    p = add("submit", cmd_submit, "submit an assignment (text, URL or files)", writes=True)
    p.add_argument("course")
    p.add_argument("assignment")
    kind = p.add_mutually_exclusive_group(required=True)
    kind.add_argument("--text", help="plain text (converted to HTML paragraphs)")
    kind.add_argument("--text-file", help="UTF-8 file whose content is the text entry")
    kind.add_argument("--url")
    kind.add_argument("--file", action="append", help="file to upload (repeat for several)")
    p.add_argument("--html", action="store_true", help="treat --text/--text-file as HTML")
    p.add_argument("--comment", help="comment attached to the submission")
    p = add("comment", cmd_comment, "add a comment to your submission", writes=True)
    p.add_argument("course")
    p.add_argument("assignment")
    p.add_argument("--text", required=True)
    p.add_argument("--file", action="append", help="attach a file (repeat for several)")
    p.add_argument("--attempt", type=int, help="attach the comment to a specific attempt")
    p = add("peer-reviews", cmd_peer_reviews, "peer reviews of an assignment visible to you")
    p.add_argument("course")
    p.add_argument("assignment")

    add("todo", cmd_todo, "Canvas to-do list (items needing submission)")
    add("upcoming", cmd_upcoming, "upcoming events and due dates")
    add("missing", cmd_missing, "past-due assignments you have not submitted")
    p = add("planner", cmd_planner, "planner items in a date range (default: next 14 days)")
    p.add_argument("--start", type=iso_date)
    p.add_argument("--end", type=iso_date)
    p.add_argument("--filter", choices=["new_activity", "incomplete_items", "complete_items"])
    p.add_argument("--course", action="append", help="limit to a course id (repeatable)")
    p = add("planner-done", cmd_planner_done, "mark a planner item complete in your planner", writes=True)
    p.add_argument("type", choices=["announcement", "assignment", "discussion_topic", "quiz", "wiki_page",
                                    "planner_note", "calendar_event", "assessment_request", "sub_assignment"])
    p.add_argument("id")
    p.add_argument("--undo", action="store_true", help="mark as not complete")
    p = add("planner-notes", cmd_planner_notes, "your personal planner notes")
    p.add_argument("--start", type=iso_date)
    p.add_argument("--end", type=iso_date)
    p = add("planner-note-add", cmd_planner_note_add, "create a personal planner note", writes=True)
    p.add_argument("--title", required=True)
    p.add_argument("--date", required=True, type=iso_date, help="YYYY-MM-DD")
    p.add_argument("--details")
    p.add_argument("--course")
    add("planner-note-delete", cmd_planner_note_delete, "delete a planner note", writes=True).add_argument("id")
    p = add("calendar", cmd_calendar, "calendar events and assignment due dates (default: next 14 days)")
    p.add_argument("--start", type=iso_date)
    p.add_argument("--end", type=iso_date)
    p.add_argument("--course", action="append", help="course id (repeatable, max 9)")
    p.add_argument("--type", choices=["event", "assignment", "all"], default="all")
    p = add("event-add", cmd_event_add, "create an event in your personal calendar", writes=True)
    p.add_argument("--title", required=True)
    p.add_argument("--start", required=True, type=iso_date, help="ISO 8601, e.g. 2026-11-20T16:00:00+01:00")
    p.add_argument("--end", type=iso_date)
    p.add_argument("--description")
    p.add_argument("--location")
    add("event-delete", cmd_event_delete, "delete one of your calendar events", writes=True).add_argument("id")
    p = add("activity", cmd_activity, "recent activity stream across your courses")
    p.add_argument("--summary", action="store_true", help="counts per type instead of items")

    add("modules", cmd_modules, "course modules with items and completion state").add_argument("course")
    for name, func, text in (("module-done", cmd_module_done, "mark a module item done (mark-as-done requirement)"),
                             ("module-read", cmd_module_read, "mark a module item as viewed (must-view requirement)")):
        p = add(name, func, text, writes=True)
        p.add_argument("course")
        p.add_argument("module")
        p.add_argument("item")
        if name == "module-done":
            p.add_argument("--undo", action="store_true")
    p = add("pages", cmd_pages, "wiki pages of a course")
    p.add_argument("course")
    p.add_argument("--search")
    p = add("page", cmd_page, "read a page as text")
    p.add_argument("course")
    p.add_argument("page", nargs="?", help="page url slug or page_id:<id>")
    p.add_argument("--front", action="store_true", help="the course front page")
    p.add_argument("--html", action="store_true", help="keep the HTML body")
    p = add("files", cmd_files, "list files of a course, a folder or your personal files")
    p.add_argument("course", nargs="?")
    p.add_argument("--folder")
    p.add_argument("--search")
    p.add_argument("--content-type", action="append", help="e.g. application/pdf or image (repeatable)")
    p.add_argument("--sort", choices=["name", "size", "created_at", "updated_at", "content_type"], default="name")
    add("folders", cmd_folders, "folders of a course (or your personal folders)").add_argument("course", nargs="?")
    p = add("download", cmd_download, "download a file to a local folder")
    p.add_argument("file_id")
    p.add_argument("--dest", default=".")
    p.add_argument("--overwrite", action="store_true")
    add("quota", cmd_quota, "your personal file storage quota")
    p = add("upload", cmd_upload, "upload a file to your personal files (or another files endpoint)", writes=True)
    p.add_argument("file")
    p.add_argument("--folder-path", help="destination folder path, created if missing")
    p.add_argument("--endpoint", help="files endpoint, e.g. groups/12/files (default users/self/files)")
    p = add("announcements", cmd_announcements, "announcements (default: active courses, last 14 days)")
    p.add_argument("--course", action="append")
    p.add_argument("--start", type=iso_date)
    p.add_argument("--end", type=iso_date)
    p = add("discussions", cmd_discussions, "discussion topics of a course")
    p.add_argument("course")
    p.add_argument("--unread", action="store_true")
    p.add_argument("--search")
    p = add("discussion", cmd_discussion, "a discussion topic with its threaded replies")
    p.add_argument("course")
    p.add_argument("topic")
    p = add("discussion-post", cmd_discussion_post, "post an entry or a reply in a discussion", writes=True)
    p.add_argument("course")
    p.add_argument("topic")
    p.add_argument("--message", required=True)
    p.add_argument("--reply-to", help="entry id to reply to")
    p.add_argument("--html", action="store_true")
    p = add("discussion-read", cmd_discussion_read, "mark a discussion and its entries read", writes=True)
    p.add_argument("course")
    p.add_argument("topic")
    p = add("quizzes", cmd_quizzes, "quizzes of a course (metadata only)")
    p.add_argument("course")
    p.add_argument("--new", action="store_true", help="list New Quizzes instead of classic quizzes")
    p = add("quiz", cmd_quiz, "quiz settings and your attempts and scores")
    p.add_argument("course")
    p.add_argument("quiz")
    add("groups", cmd_groups, "your groups")
    add("group-members", cmd_group_members, "members of a group").add_argument("group")

    p = add("inbox", cmd_inbox, "your conversations (default 30)")
    p.add_argument("--scope", choices=["unread", "starred", "archived", "sent"])
    add("unread-count", cmd_unread_count, "number of unread conversations")
    p = add("conversation", cmd_conversation, "read a conversation (does not mark it read by default)")
    p.add_argument("id")
    p.add_argument("--mark-read", action="store_true")
    p = add("recipients", cmd_recipients, "find message recipients (people, courses, groups)")
    p.add_argument("--search", required=True)
    p.add_argument("--context", help="e.g. course_123")
    p.add_argument("--type", choices=["user", "context"])
    p = add("send", cmd_send, "start a conversation", writes=True)
    p.add_argument("--to", action="append", required=True, help="user id or context id like course_1 (repeatable)")
    p.add_argument("--subject", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--course", help="course id the message belongs to")
    p.add_argument("--group", action="store_true",
                   help="one shared conversation for all recipients (default: a private copy per recipient)")
    p = add("reply", cmd_reply, "reply in an existing conversation", writes=True)
    p.add_argument("id")
    p.add_argument("--body", required=True)
    p = add("conversation-update", cmd_conversation_update, "mark read/unread/archived or star", writes=True)
    p.add_argument("id")
    p.add_argument("--state", choices=["read", "unread", "archived"])
    star = p.add_mutually_exclusive_group()
    star.add_argument("--star", dest="star", action="store_true", default=None)
    star.add_argument("--unstar", dest="star", action="store_false")

    add("nicknames", cmd_nicknames, "your course nicknames")
    p = add("nickname", cmd_nickname, "set or clear a course nickname", writes=True)
    p.add_argument("course")
    p.add_argument("--set")
    p.add_argument("--clear", action="store_true")
    add("channels", cmd_channels, "your notification channels (email, push, sms)")

    p = add("get", cmd_get, "GET any API path, e.g. users/self/colors (lists are fully paginated)")
    p.add_argument("path")
    p.add_argument("-p", "--param", action="append", help="query parameter KEY=VALUE (repeatable)")
    p.add_argument("--key", help="paginate a list wrapped in an object, e.g. quiz_submissions")
    p = add("api", cmd_api, "POST/PUT/DELETE any API path", writes=True)
    p.add_argument("method", choices=["POST", "PUT", "DELETE"])
    p.add_argument("path")
    p.add_argument("-d", "--data", action="append", help="form field KEY=VALUE (repeatable)")
    p.add_argument("--json", help="JSON body instead of form fields")
    return parser


def windows_user_env_has(name: str) -> bool:
    """True if `name` is saved in the Windows user environment (where `setx` writes). Reads existence only."""
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return False
    return True


def missing_config_error(name: str, how: str, user_env_has: Callable[[str], bool]) -> ConfigError:
    message = f"Set {name} ({how})"
    if user_env_has(name):
        # setx only reaches processes started afterwards; the app running this CLI predates it.
        message = (f"{name} is saved in your Windows user environment, but this process started before it was set. "
                   "Fully restart the app that runs your agent (or open a new terminal) to pick it up")
    return ConfigError(message)


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None, out: TextIO | None = None,
         err: TextIO | None = None, sleep: Callable[[float], None] = time.sleep,
         user_env_has: Callable[[str], bool] = windows_user_env_has) -> int:
    env = os.environ if env is None else env
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        args = build_parser().parse_args(argv)
    except UsageError as exc:
        err.write(json.dumps({"error": str(exc), "kind": "usage"}, ensure_ascii=False) + "\n")
        return EXIT_USAGE_ERROR
    escape_path_ids(args)
    client: CanvasClient | None = None
    try:
        base_url, token = env.get(ENV_BASE_URL, "").strip(), env.get(ENV_TOKEN, "").strip()
        if not base_url:
            raise missing_config_error(ENV_BASE_URL, "e.g. https://myschool.instructure.com", user_env_has)
        if not token:
            raise missing_config_error(ENV_TOKEN, "Canvas > Account > Settings > New Access Token", user_env_has)
        if not TOKEN_PATTERN.fullmatch(token):  # never echo the value: it is password-equivalent
            raise ConfigError(f"{ENV_TOKEN} contains spaces, quotes or non-ASCII characters, so it is not a valid "
                              "token. Copy it again from Canvas and save it without surrounding quotes")
        client = CanvasClient(base_url, token, sleep=sleep)
        result = args.func(client, args)
    except ConfigError as exc:
        err.write(json.dumps({"error": str(exc), "kind": "config"}, ensure_ascii=False) + "\n")
        return EXIT_CONFIG_ERROR
    except CanvasError as exc:
        message = client.redact(str(exc)) if client else str(exc)
        err.write(json.dumps({"error": message, "status": exc.status}, ensure_ascii=False) + "\n")
        return EXIT_API_ERROR
    except OSError as exc:
        err.write(json.dumps({"error": f"Local file error: {exc}"}, ensure_ascii=False) + "\n")
        return EXIT_API_ERROR
    for message in dict.fromkeys(client.warnings):  # de-duplicated, in order
        warn(err, message)
    out.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    sys.exit(main())
