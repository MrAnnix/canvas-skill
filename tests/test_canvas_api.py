"""End-to-end tests for canvas_api.py against a local fake Canvas server.

Each test drives the real CLI entry point (`main`) over real HTTP, so the
pagination, retry, redirect and upload logic is exercised exactly as in use.
Run with:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "canvas-student" / "scripts"))
import canvas_api  # noqa: E402

TOKEN = "test-token-123"


class FakeServer:
    """A tiny HTTP server whose routes are plain functions set per test."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], object] = {}
        self.fallback = None  # optional route used when no exact route matches
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output clean
                pass

            def _handle(self, method: str) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                record = {
                    "method": method,
                    "path": parsed.path,
                    "query": urllib.parse.parse_qs(parsed.query),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                }
                server.requests.append(record)
                route = server.routes.get((method, parsed.path)) or server.fallback
                if route is None:
                    status, headers, payload = 404, {}, {"errors": [{"message": "The specified resource does not exist."}]}
                else:
                    status, headers, payload = route(record)
                if status is None:  # simulate a server that drops the connection without answering
                    self.close_connection = True
                    return
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                if "Content-Type" not in headers:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

            def do_DELETE(self):
                self._handle("DELETE")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def route(self, method: str, path: str, status: int = 200, payload=None, headers=None):
        self.routes[(method, path)] = lambda _r: (status, headers or {}, payload if payload is not None else {})


class CanvasTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.canvas = FakeServer()
        self.addCleanup(self.canvas.close)
        self.sleeps: list[float] = []

    def run_cli(self, *argv: str, env: dict | None = None) -> tuple[int, object, str]:
        out, err = io.StringIO(), io.StringIO()
        environment = {"CANVAS_BASE_URL": self.canvas.url, "CANVAS_API_TOKEN": TOKEN}
        if env is not None:
            environment = env
        code = canvas_api.main(list(argv), env=environment, out=out, err=err, sleep=self.sleeps.append)
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else None), err.getvalue()


class ConfigTests(CanvasTestCase):
    def test_missing_token_exits_with_config_error(self):
        code, _, err = self.run_cli("whoami", env={"CANVAS_BASE_URL": self.canvas.url})
        self.assertEqual(code, canvas_api.EXIT_CONFIG_ERROR)
        self.assertIn("CANVAS_API_TOKEN", err)

    def test_missing_variable_saved_by_setx_suggests_restart(self):
        out, err = io.StringIO(), io.StringIO()
        code = canvas_api.main(["whoami"], env={}, out=out, err=err, user_env_has={"CANVAS_BASE_URL"}.__contains__)
        self.assertEqual(code, canvas_api.EXIT_CONFIG_ERROR)
        self.assertIn("restart", err.getvalue())

    def test_missing_variable_not_saved_anywhere_gives_plain_error(self):
        out, err = io.StringIO(), io.StringIO()
        code = canvas_api.main(["whoami"], env={}, out=out, err=err, user_env_has=lambda name: False)
        self.assertEqual(code, canvas_api.EXIT_CONFIG_ERROR)
        self.assertNotIn("restart", err.getvalue())

    def test_plain_http_rejected_for_remote_hosts(self):
        code, _, err = self.run_cli("whoami", env={"CANVAS_BASE_URL": "http://school.instructure.com", "CANVAS_API_TOKEN": TOKEN})
        self.assertEqual(code, canvas_api.EXIT_CONFIG_ERROR)
        self.assertIn("https", err)

    def test_base_url_with_api_suffix_is_normalized(self):
        self.assertEqual(canvas_api.normalize_base_url("https://x.instructure.com/api/v1/"), "https://x.instructure.com")


class RequestTests(CanvasTestCase):
    def test_whoami_sends_bearer_token_and_slims_output(self):
        self.canvas.route("GET", "/api/v1/users/self/profile", payload={"id": 7, "name": "Ana", "time_zone": "Europe/Madrid", "bio": "x"})
        code, data, _ = self.run_cli("whoami")
        self.assertEqual(code, 0)
        self.assertEqual(data, {"id": 7, "name": "Ana", "time_zone": "Europe/Madrid"})
        self.assertEqual(self.canvas.requests[0]["headers"]["authorization"], f"Bearer {TOKEN}")

    def test_array_params_use_bracket_syntax(self):
        self.canvas.route("GET", "/api/v1/courses", payload=[])
        self.run_cli("courses")
        query = self.canvas.requests[0]["query"]
        self.assertIn("term", query["include[]"])
        self.assertEqual(query["enrollment_type"], ["student"])

    def test_pagination_follows_link_header_until_limit(self):
        base = self.canvas.url

        def page(record):
            n = int(record["query"].get("page", ["1"])[0])
            link = f'<{base}/api/v1/courses/1/assignments?page={n + 1}>; rel="next"' if n < 3 else ""
            return 200, ({"Link": link} if link else {}), [{"id": n * 10 + i, "name": f"A{n}{i}"} for i in range(2)]

        self.canvas.routes[("GET", "/api/v1/courses/1/assignments")] = page
        code, data, _ = self.run_cli("assignments", "1")
        self.assertEqual(code, 0)
        self.assertEqual(len(data), 6)
        self.assertEqual(self.canvas.requests[0]["query"]["per_page"], ["100"])
        code, data, _ = self.run_cli("assignments", "1", "--limit", "3")
        self.assertEqual(len(data), 3)

    def _two_page_route(self, path: str, payload_key: str | None = None):
        base = self.canvas.url

        def page(record):
            n = int(record["query"].get("page", ["1"])[0])
            items = [{"id": n * 10 + i, "title": f"T{n}{i}"} for i in range(2)]
            headers = {"Link": f'<{base}{path}?page=2>; rel="next"'} if n == 1 else {}
            return 200, headers, ({payload_key: items} if payload_key else items)

        self.canvas.routes[("GET", path)] = page

    def test_upcoming_events_follows_pagination(self):
        self._two_page_route("/api/v1/users/self/upcoming_events")
        code, data, err = self.run_cli("upcoming")
        self.assertEqual(code, 0, err)
        self.assertEqual([e["id"] for e in data], [10, 11, 20, 21])

    def test_generic_get_paginates_by_default_and_unwraps_key(self):
        self._two_page_route("/api/v1/courses/1/quizzes/3/submissions", payload_key="quiz_submissions")
        code, data, err = self.run_cli("get", "courses/1/quizzes/3/submissions", "--key", "quiz_submissions")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(data), 4)

    def test_per_page_is_clamped_to_maximum(self):
        self.canvas.route("GET", "/api/v1/courses", payload=[])
        self.run_cli("get", "courses", "-p", "per_page=500")
        self.assertEqual(self.canvas.requests[0]["query"]["per_page"], [str(canvas_api.MAX_PER_PAGE)])

    def test_truncation_by_limit_is_reported(self):
        self._two_page_route("/api/v1/conversations")
        code, data, err = self.run_cli("inbox", "--limit", "3")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(data), 3)
        self.assertIn("--limit", err)
        code, data, err = self.run_cli("inbox", "--limit", "4")
        self.assertEqual(len(data), 4)
        self.assertNotIn("--limit", err)

    def test_throttled_request_is_retried(self):
        calls = {"n": 0}

        def throttled(_record):
            calls["n"] += 1
            if calls["n"] == 1:
                return 403, {}, b"403 Forbidden (Rate Limit Exceeded)"
            if calls["n"] == 2:
                return 429, {"Retry-After": "3"}, b""
            return 200, {}, {"id": 1, "name": "Ana"}

        self.canvas.routes[("GET", "/api/v1/users/self/profile")] = throttled
        code, data, _ = self.run_cli("whoami")
        self.assertEqual(code, 0)
        self.assertEqual(data["name"], "Ana")
        self.assertEqual(self.sleeps, [1, 3])

    def test_api_error_message_is_reported_without_token(self):
        self.canvas.route("GET", "/api/v1/courses/99", status=401,
                          payload={"errors": [{"message": "Invalid access token."}]},
                          headers={"WWW-Authenticate": "Bearer"})
        code, _, err = self.run_cli("course", "99")
        self.assertEqual(code, canvas_api.EXIT_API_ERROR)
        self.assertIn("Invalid access token.", err)
        self.assertIn("expired", err)
        self.assertNotIn(TOKEN, err)

    def test_refuses_to_send_token_to_another_host(self):
        code, _, err = self.run_cli("get", "https://evil.example.com/api/v1/users/self")
        self.assertEqual(code, canvas_api.EXIT_API_ERROR)
        self.assertIn("different host", err)
        self.assertEqual(self.canvas.requests, [])

    def test_generic_get_with_params(self):
        self.canvas.route("GET", "/api/v1/users/self/colors", payload={"custom_colors": {}})
        code, data, _ = self.run_cli("get", "users/self/colors", "-p", "a=1", "-p", "ids[]=2", "-p", "ids[]=3")
        self.assertEqual(code, 0)
        self.assertEqual(data, {"custom_colors": {}})
        query = self.canvas.requests[0]["query"]
        self.assertEqual(query["ids[]"], ["2", "3"])
        self.assertEqual(query["a"], ["1"])


class WriteSafetyTests(CanvasTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.canvas.route("GET", "/api/v1/courses/1/assignments/5", payload={
            "id": 5, "name": "Essay", "submission_types": ["online_text_entry", "online_upload"],
            "allowed_extensions": ["pdf"], "locked_for_user": False})
        self.canvas.route("POST", "/api/v1/courses/1/assignments/5/submissions",
                          payload={"assignment_id": 5, "workflow_state": "submitted", "attempt": 1, "submission_type": "online_text_entry"})

    def test_write_without_confirm_is_a_dry_run(self):
        code, data, _ = self.run_cli("submit", "1", "5", "--text", "Hola")
        self.assertEqual(code, 0)
        self.assertTrue(data["dry_run"])
        self.assertFalse(any(r["method"] == "POST" for r in self.canvas.requests))

    def test_submit_text_with_confirm_posts_html_body(self):
        code, data, _ = self.run_cli("submit", "1", "5", "--text", "Line <1>\n\nPara 2", "--comment", "Done", "--confirm")
        self.assertEqual(code, 0)
        self.assertTrue(data["done"])
        post = [r for r in self.canvas.requests if r["method"] == "POST"][0]
        form = urllib.parse.parse_qs(post["body"].decode())
        self.assertEqual(form["submission[submission_type]"], ["online_text_entry"])
        self.assertEqual(form["submission[body]"], ["<p>Line &lt;1&gt;</p>\n<p>Para 2</p>"])
        self.assertEqual(form["comment[text_comment]"], ["Done"])

    def test_submit_rejects_type_the_assignment_does_not_accept(self):
        code, _, err = self.run_cli("submit", "1", "5", "--url", "https://example.com", "--confirm")
        self.assertEqual(code, canvas_api.EXIT_API_ERROR)
        self.assertIn("online_url", err)
        self.assertFalse(any(r["method"] == "POST" for r in self.canvas.requests))

    def test_submit_rejects_disallowed_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "essay.docx")
            path.write_bytes(b"x")
            code, _, err = self.run_cli("submit", "1", "5", "--file", str(path), "--confirm")
        self.assertEqual(code, canvas_api.EXIT_API_ERROR)
        self.assertIn("pdf", err)

    def test_generic_api_write_requires_confirm(self):
        self.canvas.route("PUT", "/api/v1/users/self/colors/course_1", payload={"hexcode": "#ffffff"})
        code, data, _ = self.run_cli("api", "PUT", "users/self/colors/course_1", "-d", "hexcode=#ffffff")
        self.assertTrue(data["dry_run"])
        self.assertEqual(self.canvas.requests, [])
        code, data, _ = self.run_cli("api", "PUT", "users/self/colors/course_1", "-d", "hexcode=#ffffff", "--confirm")
        self.assertEqual(code, 0)
        self.assertEqual(data["result"], {"hexcode": "#ffffff"})


class UploadTests(CanvasTestCase):
    """The 3-step file upload flow from file.file_uploads.html."""

    def setUp(self) -> None:
        super().setUp()
        self.storage = FakeServer()  # different port = different origin, like S3/inst-fs
        self.addCleanup(self.storage.close)
        self.canvas.route("GET", "/api/v1/courses/1/assignments/5", payload={
            "id": 5, "submission_types": ["online_upload"], "allowed_extensions": None, "locked_for_user": False})
        self.canvas.route("POST", "/api/v1/courses/1/assignments/5/submissions/self/files", payload={
            "upload_url": f"{self.storage.url}/upload", "upload_params": {"key": "abc", "policy": "p"}})
        self.canvas.route("GET", "/api/v1/files/77/create_success", payload={"id": 77, "display_name": "essay.pdf"})
        self.canvas.route("POST", "/api/v1/courses/1/assignments/5/submissions", payload={"workflow_state": "submitted"})

    def _submit_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "essay.pdf")
            path.write_bytes(b"%PDF-1.4 test")
            return self.run_cli("submit", "1", "5", "--file", str(path), "--confirm")

    def test_redirect_confirmation_flow(self):
        self.storage.route("POST", "/upload", status=301,
                           headers={"Location": f"{self.canvas.url}/api/v1/files/77/create_success?uuid=X"})
        code, data, err = self._submit_file()
        self.assertEqual(code, 0, err)
        upload = self.storage.requests[0]
        self.assertNotIn("authorization", upload["headers"])  # token never goes to storage
        body = upload["body"]
        self.assertLess(body.index(b'name="key"'), body.index(b'name="file"'))  # file field last
        self.assertIn(b"%PDF-1.4 test", body)
        step1 = urllib.parse.parse_qs(self.canvas.requests[1]["body"].decode())
        self.assertEqual(step1["name"], ["essay.pdf"])
        self.assertEqual(step1["size"], [str(len(b"%PDF-1.4 test"))])
        confirm = [r for r in self.canvas.requests if r["path"].endswith("create_success")][0]
        self.assertEqual(confirm["headers"]["authorization"], f"Bearer {TOKEN}")
        final = urllib.parse.parse_qs(self.canvas.requests[-1]["body"].decode())
        self.assertEqual(final["submission[file_ids][]"], ["77"])

    def test_created_response_with_body(self):
        self.storage.route("POST", "/upload", status=201, payload={"id": 77, "display_name": "essay.pdf"})
        code, _, err = self._submit_file()
        self.assertEqual(code, 0, err)
        final = urllib.parse.parse_qs(self.canvas.requests[-1]["body"].decode())
        self.assertEqual(final["submission[file_ids][]"], ["77"])


class DownloadTests(CanvasTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.storage = FakeServer()
        self.addCleanup(self.storage.close)
        self.canvas.route("GET", "/api/v1/files/9", payload={
            "id": 9, "display_name": "../../Tema 1: intro.pdf", "url": f"{self.canvas.url}/files/9/download?download_frd=1"})
        self.canvas.route("GET", "/files/9/download", status=302, headers={"Location": f"{self.storage.url}/blob/9"})
        self.storage.route("GET", "/blob/9", payload=b"PDFDATA", headers={"Content-Type": "application/pdf"})

    def test_download_strips_token_on_cross_host_redirect_and_sanitizes_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, data, err = self.run_cli("download", "9", "--dest", tmp)
            self.assertEqual(code, 0, err)
            saved = Path(data["path"])
            self.assertEqual(saved.parent, Path(tmp).resolve())
            self.assertEqual(saved.name, "Tema 1_ intro.pdf")
            self.assertEqual(saved.read_bytes(), b"PDFDATA")
            code, _, err = self.run_cli("download", "9", "--dest", tmp)
            self.assertEqual(code, canvas_api.EXIT_API_ERROR)
            self.assertIn("--overwrite", err)
        self.assertEqual(self.canvas.requests[1]["headers"]["authorization"], f"Bearer {TOKEN}")
        self.assertNotIn("authorization", self.storage.requests[0]["headers"])


class FeatureTests(CanvasTestCase):
    def test_planner_done_updates_existing_override(self):
        self.canvas.route("GET", "/api/v1/planner/overrides", payload=[
            {"id": 3, "plannable_type": "assignment", "plannable_id": 5, "marked_complete": False}])
        self.canvas.route("PUT", "/api/v1/planner/overrides/3", payload={"id": 3, "marked_complete": True})
        code, data, _ = self.run_cli("planner-done", "assignment", "5", "--confirm")
        self.assertEqual(code, 0)
        put = [r for r in self.canvas.requests if r["method"] == "PUT"][0]
        self.assertEqual(urllib.parse.parse_qs(put["body"].decode())["marked_complete"], ["true"])

    def test_planner_done_creates_override_when_missing(self):
        self.canvas.route("GET", "/api/v1/planner/overrides", payload=[])
        self.canvas.route("POST", "/api/v1/planner/overrides", payload={"id": 4})
        code, _, _ = self.run_cli("planner-done", "assignment", "5", "--confirm")
        self.assertEqual(code, 0)
        post = [r for r in self.canvas.requests if r["method"] == "POST"][0]
        form = urllib.parse.parse_qs(post["body"].decode())
        self.assertEqual(form["plannable_type"], ["assignment"])
        self.assertEqual(form["plannable_id"], ["5"])

    def test_calendar_caps_context_codes_at_ten(self):
        self.canvas.route("GET", "/api/v1/users/self", payload={"id": 42})
        self.canvas.route("GET", "/api/v1/courses", payload=[{"id": i} for i in range(12)])
        self.canvas.route("GET", "/api/v1/calendar_events", payload=[])
        code, _, err = self.run_cli("calendar")
        self.assertEqual(code, 0)
        cal = [r for r in self.canvas.requests if r["path"] == "/api/v1/calendar_events"][0]
        codes = cal["query"]["context_codes[]"]
        self.assertEqual(len(codes), 10)
        self.assertEqual(codes[0], "user_42")
        self.assertIn("--course", err)

    def test_conversation_read_does_not_mark_as_read_by_default(self):
        self.canvas.route("GET", "/api/v1/conversations/8", payload={
            "id": 8, "subject": "Hi", "participants": [{"id": 1, "name": "Prof"}],
            "messages": [{"id": 2, "author_id": 1, "body": "Hello", "created_at": "2026-01-01T00:00:00Z"}]})
        code, data, _ = self.run_cli("conversation", "8")
        self.assertEqual(code, 0)
        self.assertEqual(self.canvas.requests[0]["query"]["auto_mark_as_read"], ["false"])
        self.assertEqual(data["messages"][0]["author"], "Prof")

    def test_send_to_whole_course_is_flagged_in_dry_run(self):
        code, data, _ = self.run_cli("send", "--to", "course_1", "--subject", "Q", "--body", "Hi")
        self.assertTrue(data["dry_run"])
        self.assertIn("course_1", data["summary"])
        self.assertIn("WARNING", data["summary"])

    def test_discussion_view_builds_threaded_entries(self):
        self.canvas.route("GET", "/api/v1/courses/1/discussion_topics/4", payload={"id": 4, "title": "Debate", "message": "<p>Go</p>"})
        self.canvas.route("GET", "/api/v1/courses/1/discussion_topics/4/view", payload={
            "participants": [{"id": 1, "display_name": "Ana"}, {"id": 2, "display_name": "Luis"}],
            "view": [{"id": 10, "user_id": 1, "message": "<p>First</p>", "created_at": "t",
                      "replies": [{"id": 11, "user_id": 2, "message": "<b>Reply</b>", "created_at": "t"}]}]})
        code, data, _ = self.run_cli("discussion", "1", "4")
        self.assertEqual(code, 0)
        self.assertEqual(data["message"], "Go")
        entry = data["entries"][0]
        self.assertEqual((entry["author"], entry["message"]), ("Ana", "First"))
        self.assertEqual(entry["replies"][0]["author"], "Luis")

    def test_quiz_submissions_unwraps_key(self):
        self.canvas.route("GET", "/api/v1/courses/1/quizzes/3", payload={"id": 3, "title": "Q1", "description": "<p>Read</p>"})
        self.canvas.route("GET", "/api/v1/courses/1/quizzes/3/submissions", payload={
            "quiz_submissions": [{"attempt": 1, "score": 8, "kept_score": 8, "workflow_state": "complete"}]})
        code, data, _ = self.run_cli("quiz", "1", "3")
        self.assertEqual(code, 0)
        self.assertEqual(data["my_attempts"][0]["score"], 8)


class SmokeTests(CanvasTestCase):
    """Every read command must run cleanly when Canvas returns empty data."""

    OBJECT_ENDPOINTS = ("/profile", "/users/self", "/courses/1", "/front_page", "/pages/intro", "/quizzes/3",
                        "/assignments/5", "/submissions/self", "/discussion_topics/4", "/view", "/conversations/8",
                        "/unread_count", "/quota", "/activity_stream/summary")

    def test_all_read_commands(self):
        def empty(record):
            is_object = record["path"].endswith(self.OBJECT_ENDPOINTS)
            payload = {"id": 1} if is_object else []
            if record["path"].endswith("/files/9"):
                payload = {"id": 9, "display_name": "x"}  # no url: download must fail cleanly
            return 200, {}, payload

        self.canvas.fallback = empty
        commands = [
            ["whoami"], ["courses"], ["courses", "--state", "all"], ["course", "1"], ["tabs", "1"], ["grades"],
            ["grades", "--course", "1", "--include-completed"], ["grading-periods", "1"],
            ["assignments", "1", "--bucket", "upcoming", "--search", "ensayo"], ["assignment", "1", "5"],
            ["submission", "1", "5", "--history"], ["submissions", "1", "--workflow-state", "graded"],
            ["peer-reviews", "1", "5"], ["todo"], ["upcoming"], ["missing"],
            ["planner", "--start", "2026-11-01", "--end", "2026-11-30", "--course", "1"], ["planner-notes"],
            ["calendar", "--course", "1", "--type", "event"], ["activity"], ["activity", "--summary"],
            ["modules", "1"], ["pages", "1", "--search", "tema"], ["page", "1", "intro"], ["page", "1", "--front"],
            ["files", "1"], ["files", "--folder", "2"], ["files"], ["folders", "1"], ["folders"], ["quota"],
            ["announcements"], ["announcements", "--course", "1"], ["discussions", "1", "--unread"],
            ["discussion", "1", "4"], ["quizzes", "1"], ["quizzes", "1", "--new"], ["quiz", "1", "3"], ["groups"],
            ["group-members", "2"], ["inbox", "--scope", "unread"], ["unread-count"], ["conversation", "8"],
            ["recipients", "--search", "garcia"], ["nicknames"], ["channels"],
        ]
        for argv in commands:
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 0, err)
        code, _, err = self.run_cli("download", "9", "--dest", tempfile.gettempdir())
        self.assertEqual(code, canvas_api.EXIT_API_ERROR)
        self.assertIn("no download URL", err)

    def test_all_write_commands_dry_run_without_writes(self):
        self.canvas.fallback = lambda record: (200, {}, {"id": 1, "submission_types": ["online_text_entry"]})
        with tempfile.TemporaryDirectory() as tmp:
            upload = Path(tmp, "notes.txt")
            upload.write_text("x", encoding="utf-8")
            commands = [
                ["submit", "1", "5", "--text", "hola"], ["comment", "1", "5", "--text", "gracias"],
                ["planner-done", "assignment", "5"], ["planner-note-add", "--title", "Estudiar", "--date", "2026-11-20"],
                ["planner-note-delete", "3"], ["event-add", "--title", "Tutoria", "--start", "2026-11-20T16:00:00Z"],
                ["event-delete", "3"], ["module-done", "1", "2", "3"], ["module-read", "1", "2", "3"],
                ["upload", str(upload)], ["discussion-post", "1", "4", "--message", "Mi aporte"],
                ["discussion-post", "1", "4", "--message", "De acuerdo", "--reply-to", "10"],
                ["discussion-read", "1", "4"], ["send", "--to", "7", "--subject", "Duda", "--body", "Hola"],
                ["reply", "8", "--body", "Gracias"], ["conversation-update", "8", "--state", "archived"],
                ["nickname", "1", "--set", "Mates"], ["nickname", "1", "--clear"],
            ]
            for argv in commands:
                with self.subTest(argv=argv):
                    code, data, err = self.run_cli(*argv)
                    self.assertEqual(code, 0, err)
                    self.assertTrue(data["dry_run"])
        self.assertFalse([r for r in self.canvas.requests if r["method"] != "GET"])


class ReviewFixTests(CanvasTestCase):
    """Defects found in the full review (2026-11-12)."""

    def test_non_json_success_body_is_a_clean_error(self):
        self.canvas.route("GET", "/api/v1/users/self/profile", payload=b"<html>Maintenance</html>",
                          headers={"Content-Type": "text/html"})
        code, _, err = self.run_cli("whoami")
        self.assertEqual(code, canvas_api.EXIT_API_ERROR)
        self.assertIn("not JSON", err)

    def test_completed_courses_request_completed_course_state(self):
        # courses.html: state[] defaults to "available" for students, which hides concluded courses.
        self.canvas.route("GET", "/api/v1/courses", payload=[])
        self.run_cli("courses", "--state", "completed")
        query = self.canvas.requests[0]["query"]
        self.assertEqual(query["enrollment_state"], ["completed"])
        self.assertEqual(sorted(query["state[]"]), ["available", "completed"])

    def test_limit_must_be_positive(self):
        code, _, err = self.run_cli("courses", "--limit", "0")
        self.assertEqual(code, canvas_api.EXIT_USAGE_ERROR)
        self.assertIn("--limit", err)

    def test_invalid_json_body_is_a_clean_error(self):
        code, _, err = self.run_cli("api", "POST", "users/self/files", "--json", "{bad")
        self.assertEqual(code, canvas_api.EXIT_API_ERROR)
        self.assertIn("--json", err)

    def test_ids_are_escaped_into_a_single_path_segment(self):
        code, _, _ = self.run_cli("course", "1/../users/self")
        self.assertEqual(self.canvas.requests[0]["path"], "/api/v1/courses/1%2F..%2Fusers%2Fself")
        self.canvas.route("GET", "/api/v1/courses/sis_course_id:MAT-1", payload={"id": 1, "name": "Mates"})
        code, data, err = self.run_cli("course", "sis_course_id:MAT-1")
        self.assertEqual(code, 0, err)


class SubmitValidationTests(CanvasTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.canvas.route("GET", "/api/v1/courses/1/assignments/5", payload={
            "id": 5, "name": "Essay", "submission_types": ["online_text_entry"], "allowed_attempts": 2,
            "submission": {"attempt": 2, "extra_attempts": None, "workflow_state": "graded"}})

    def test_empty_text_is_rejected(self):
        code, _, err = self.run_cli("submit", "1", "5", "--text", "  \n ")
        self.assertEqual(code, canvas_api.EXIT_API_ERROR)
        self.assertIn("empty", err)

    def test_non_utf8_text_file_is_a_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "latin1.txt")
            path.write_bytes("año".encode("latin-1"))
            code, _, err = self.run_cli("submit", "1", "5", "--text-file", str(path))
        self.assertEqual(code, canvas_api.EXIT_API_ERROR)
        self.assertIn("UTF-8", err)

    def test_exhausted_attempts_are_flagged(self):
        code, data, _ = self.run_cli("submit", "1", "5", "--text", "v3")
        self.assertEqual(code, 0)
        self.assertIn("RESUBMISSION", data["summary"])
        self.assertIn("no attempts left", data["summary"])

    def test_nickname_length_is_checked(self):
        code, _, err = self.run_cli("nickname", "1", "--set", "x" * 60)
        self.assertEqual(code, canvas_api.EXIT_API_ERROR)
        self.assertIn("60", err)


class AuditFixTests(CanvasTestCase):
    """Defects found in the hardening audit (2026-11-13)."""

    def test_generic_commands_refuse_quiz_taking_endpoints(self):
        # D11: no starting, answering or completing quizzes, and no question endpoints, even through get/api.
        blocked = [
            ["api", "POST", "courses/1/quizzes/3/submissions"],
            ["api", "POST", "courses/1/quizzes/3/submissions/9/complete", "--confirm"],
            ["api", "POST", "quiz_submissions/9/questions", "--json", "{}", "--confirm"],
            ["api", "PUT", "quiz_submissions/9/questions/4/flag"],
            ["api", "POST", "/api/v1/courses/1/quizzes/3//submissions?x=1", "--confirm"],
            ["api", "POST", "courses/1/quizzes/3/submissions%2F9%2Fcomplete", "--confirm"],
            ["get", "quiz_submissions/9/questions"],
            ["get", "quiz_submissions/9/questions.json"],
            ["get", f"{self.canvas.url}/api/v1/courses/1/quizzes/3/questions"],
            ["get", "courses/1/quizzes/3/x/../questions"],
            ["upload", __file__, "--endpoint", "courses/1/quizzes/3/submissions/self/files", "--confirm"],
        ]
        for argv in blocked:
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, canvas_api.EXIT_API_ERROR, err)
                self.assertIn("quiz", err.lower())
        self.assertEqual(self.canvas.requests, [])
        # Reading your own attempts stays allowed.
        self.canvas.route("GET", "/api/v1/courses/1/quizzes/3/submissions", payload={"quiz_submissions": []})
        code, data, err = self.run_cli("get", "courses/1/quizzes/3/submissions", "--key", "quiz_submissions")
        self.assertEqual((code, data), (0, []), err)

    def test_usage_error_is_json_with_its_own_exit_code(self):
        code, _, err = self.run_cli("courses", "--bogus")
        self.assertEqual(code, canvas_api.EXIT_USAGE_ERROR)
        self.assertNotEqual(code, canvas_api.EXIT_CONFIG_ERROR)
        payload = json.loads(err)
        self.assertEqual(payload["kind"], "usage")
        self.assertIn("--bogus", payload["error"])

    def test_dropped_connection_is_a_network_error(self):
        self.canvas.routes[("GET", "/api/v1/users/self/profile")] = lambda _r: (None, {}, b"")
        code, _, err = self.run_cli("whoami")
        self.assertEqual(code, canvas_api.EXIT_API_ERROR)
        self.assertIn("Network error", err)
        self.assertNotIn("Local file error", err)

    def test_dropped_connection_during_download_is_a_network_error(self):
        storage = FakeServer()
        self.addCleanup(storage.close)
        self.canvas.route("GET", "/api/v1/files/9", payload={"id": 9, "display_name": "a.pdf",
                                                             "url": f"{storage.url}/blob/9"})
        storage.routes[("GET", "/blob/9")] = lambda _r: (None, {}, b"")
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_cli("download", "9", "--dest", tmp)
        self.assertEqual(code, canvas_api.EXIT_API_ERROR)
        self.assertIn("Network error", err)

    def test_persistent_throttling_is_not_reported_as_forbidden(self):
        # app/middleware/request_throttle.rb answers 403 by default (429 only behind a setting).
        self.canvas.route("GET", "/api/v1/users/self/profile", status=403,
                          payload=b"403 Forbidden (Rate Limit Exceeded)\n", headers={"Content-Type": "text/plain"})
        code, _, err = self.run_cli("whoami")
        self.assertEqual(code, canvas_api.EXIT_API_ERROR)
        self.assertEqual(len(self.sleeps), canvas_api.MAX_THROTTLE_RETRIES)
        self.assertIn("rate limit", err.lower())
        self.assertNotIn("not allowed for students", err)

    def test_text_file_with_bom_or_utf16_is_decoded(self):
        self.canvas.route("GET", "/api/v1/courses/1/assignments/5", payload={
            "id": 5, "name": "Essay", "submission_types": ["online_text_entry"]})
        # Notepad and PowerShell 5.1 `Out-File -Encoding utf8` write a UTF-8 BOM; PowerShell 5.1 `>` writes UTF-16.
        for raw in ("﻿Hola ñ".encode("utf-8"), "Hola ñ".encode("utf-16")):
            with self.subTest(raw=raw[:4]), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "answer.txt")
                path.write_bytes(raw)
                code, data, err = self.run_cli("submit", "1", "5", "--text-file", str(path))
                self.assertEqual(code, 0, err)
                self.assertIn("<p>Hola ñ</p>", data["requests"][-1])
                self.assertNotIn("﻿", data["requests"][-1])

    def test_token_with_invalid_characters_is_a_config_error_without_echo(self):
        for bad in ('"1234~abc"', "1234~abc def", "1234~abc”"):
            with self.subTest(bad=bad):
                code, _, err = self.run_cli("whoami", env={"CANVAS_BASE_URL": self.canvas.url, "CANVAS_API_TOKEN": bad})
                self.assertEqual(code, canvas_api.EXIT_CONFIG_ERROR, err)
                self.assertIn("CANVAS_API_TOKEN", err)
                self.assertNotIn("1234~abc", err)
        self.assertEqual(self.canvas.requests, [])

    def test_base_url_copied_from_the_browser_is_reduced_to_its_origin(self):
        for raw in ("https://x.instructure.com/courses/123/assignments", "https://x.instructure.com/?login_success=1",
                    "https://X.instructure.com:8443/login/canvas#top"):
            with self.subTest(raw=raw):
                self.assertEqual(canvas_api.normalize_base_url(raw).lower(),
                                 "https://x.instructure.com" + (":8443" if ":8443" in raw else ""))

    def test_pagination_loop_is_an_error_not_a_hang(self):
        base = self.canvas.url
        calls = {"n": 0}

        def looping(_record):
            calls["n"] += 1
            link = f'<{base}/api/v1/courses?page=2>; rel="next"' if calls["n"] < 50 else ""
            return 200, ({"Link": link} if link else {}), [{"id": calls["n"]}]

        self.canvas.routes[("GET", "/api/v1/courses")] = looping
        code, _, err = self.run_cli("get", "courses")
        self.assertEqual(code, canvas_api.EXIT_API_ERROR)
        self.assertIn("pagination", err.lower())
        self.assertLessEqual(calls["n"], 3)

    def test_submit_dry_run_flags_group_and_late_submissions(self):
        self.canvas.route("GET", "/api/v1/courses/1/assignments/5", payload={
            "id": 5, "name": "Lab", "submission_types": ["online_text_entry"], "group_category_id": 7,
            "due_at": "2020-01-01T23:59:00Z"})
        code, data, err = self.run_cli("submit", "1", "5", "--text", "Informe")
        self.assertEqual(code, 0, err)
        self.assertIn("group", data["summary"])
        self.assertIn("LATE", data["summary"])
        self.canvas.route("GET", "/api/v1/courses/1/assignments/5", payload={
            "id": 5, "name": "Lab", "submission_types": ["online_text_entry"], "due_at": "2999-01-01T00:00:00Z"})
        code, data, err = self.run_cli("submit", "1", "5", "--text", "Informe")
        self.assertNotIn("LATE", data["summary"])
        self.assertNotIn("WARNING", data["summary"])


class HelperTests(unittest.TestCase):
    def test_html_to_text(self):
        html = "<h1>Title</h1><p>Hello <a href='https://x.org'>link</a></p><ul><li>a</li><li>b</li></ul><script>bad()</script>"
        self.assertEqual(canvas_api.html_to_text(html), "Title\nHello link (https://x.org)\n- a\n- b")

    def test_safe_filename(self):
        self.assertEqual(canvas_api.safe_filename("CON"), "_CON")
        self.assertEqual(canvas_api.safe_filename("..."), "download")
        self.assertEqual(canvas_api.safe_filename('a/b\\c:d*e?.txt'), "c_d_e_.txt")

    def test_next_link(self):
        header = '<https://c/api/v1/x?page=1>; rel="current", <https://c/api/v1/x?page=2>; rel="next"'
        self.assertEqual(canvas_api.next_link(header), "https://c/api/v1/x?page=2")
        self.assertIsNone(canvas_api.next_link('<https://c/x>; rel="last"'))


if __name__ == "__main__":
    unittest.main()
