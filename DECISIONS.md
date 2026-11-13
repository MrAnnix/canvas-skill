# Work log and design decisions

Date: 2026-11-12. Goal: a complete agent skill (Agent Skills format) that gives an agent access to Canvas LMS **for a student account**.

## 1. Research

**Existing options (checked first).** Instructure has no official MCP server. A blog report says MCP support for Canvas was experimental with a pilot group as of October 2026; that is not confirmed by Instructure itself. The Claude connector registry has no Canvas connector. Community MCP servers exist (e.g. `vishalsachdev/canvas-mcp`, `DMontgomery40/mcp-canvas-lms`), but they need a token handed to third-party code and expose instructor/admin tools a student can't use. That is why this skill talks to the documented REST API directly.

**API review.** I read these official pages at https://canvas.instructure.com/doc/api/ (the docs announce a move to developerdocs.instructure.com):

- Conventions: `index`, `file.oauth`, `file.pagination`, `file.throttling`, `file.file_uploads`, `file.object_ids`.
- Resources: `users`, `courses`, `enrollments`, `assignments`, `submissions`, `submission_comments`, `planner`, `calendar_events`, `modules`, `pages`, `files`, `discussion_topics`, `announcements`, `tabs`, `quizzes`, `quiz_submissions`, `new_quizzes`, `groups`, `grading_periods`, `rubrics`, `peer_reviews`, `conversations`, `search`, `communication_channels`, `notification_preferences`, `external_tools`.

Several long pages (users, courses, assignments, submissions, discussion_topics, external_tools) were truncated by the fetch tool. Their tails came from an extracted summary. Every endpoint is therefore tagged *Verified* or *Summary* in `canvas-student/references/api-endpoints.md`.

**Inclusion criterion.** I included every endpoint a student can call on their own data or courses that is useful to an agent. Instructor and admin endpoints are excluded because they would only return 401/403 errors for a student.

## 2. Decisions

| # | Decision | Reason | Alternatives rejected |
|---|---|---|---|
| D1 | **Skill with a bundled CLI script**, not an MCP server | Works in any agent that supports the open Agent Skills format (agentskills.io lists Claude Code, Codex, Gemini CLI, GitHub Copilot, Cursor and others), and any agent with a shell can run the CLI. No server process. Progressive disclosure keeps context small: SKILL.md, then references, then the script. | An MCP server needs a running process and per-client config. Raw `curl` instructions would make every call re-derive pagination, uploads and redirects. |
| D2 | **Python standard library only** (3.10+) | Nothing to install or pin, so no supply-chain surface. Python 3.13 is present on this machine. | `requests`/`canvasapi` would add dependencies for little gain. |
| D3 | **Token only from `CANVAS_API_TOKEN`**, never an argument | Arguments end up in shell history, process lists and chat transcripts. The OAuth page calls tokens password-equivalent. | `--token` flag; a config file inside the repo. |
| D4 | **The token is sent only to the configured Canvas origin** (scheme, host, port). Automatic redirects are disabled and followed manually, dropping `Authorization` across hosts. URLs on other hosts are refused. | The upload docs say the token must not go to `upload_url`. Downloads may redirect to storage hosts. urllib's default redirect handler would forward the header. | Trusting urllib's redirects. |
| D5 | **https required**, except `http://localhost` (needed for tests) | The token travels on every request. | |
| D6 | **Writes are dry runs unless `--confirm`** | Submissions, posts and messages are visible to others, and some are irreversible (a submission consumes an attempt). This gives the agent a natural "show, then ask" step and matches the user's global approval rules. | Interactive prompts, which an agent can't answer reliably. Trusting the model alone. |
| D7 | **Local pre-checks before submitting**: accepted `submission_types`, lock state, `allowed_extensions`, and a RESUBMISSION flag | Catches the documented 400 error and wasted uploads before anything is sent. It also tells the user when a submission is a new attempt. | Letting Canvas reject it. |
| D8 | **Compact JSON by default, `--full` for raw** | Canvas objects are large (an assignment has 60+ fields). Compact output saves context and keeps the agent focused. HTML fields become plain text. | Always raw, which floods the context. |
| D9 | **Every list call is paginated to the end**: `per_page=100` (larger values clamped), Link rel `next` followed until absent, including `upcoming_events`, wrapped lists (`--key`) and the generic `get`. If `--limit` or a default cap (inbox/activity 30, recipients 20) cuts results, a stderr warning says more exist. | Canvas pages at 10 by default and caps `per_page` ("unspecified limit" generally, "Maximum of 100" per endpoint). A single call silently loses data. Link URLs are opaque per the docs. | Single requests with a big `per_page`; building page numbers by hand, which the docs advise against. |
| D10 | **Retry throttling up to 3 times** (429, or 403 with "Rate Limit Exceeded"), honoring `Retry-After`, otherwise backing off 1/2/4 s. Sequential requests only. | The docs give no schedule. Parallel requests cost a pre-flight penalty. The 403 variant is Canvas's default ([request_throttle.rb](https://github.com/instructure/canvas-lms/blob/master/app/middleware/request_throttle.rb); 429 only behind a setting). | Unlimited retries; parallel fan-out. |
| D11 | **No quiz-taking**: no start/answer/complete commands, no question endpoints, and every request (including `get`, `api` and `upload --endpoint`) to those endpoints is refused | An agent taking graded quizzes in the student's name is academic misconduct. The skill instead helps with preparation and reviewing results. | Wrapping them "for completeness". |
| D12 | **SKILL.md integrity rules**: submit only the student's own work, and draft posts and messages with the student | Same reason as D11, applied to assignments and communication. Each rule is explained rather than shouted, following the skill-writing guidance. | |
| D13 | **Reading a conversation doesn't mark it read** (`auto_mark_as_read=false`) unless `--mark-read` | The Canvas default is true. Reading through the agent would otherwise silently hide new messages from the user. | Keeping Canvas's default. |
| D14 | **`send` warns when a recipient is a context** (`course_123`...) and doesn't set `group_conversation` implicitly (`--group` opt-in) | A context id can message a whole course. | Auto-enabling group mode. |
| D15 | **Downloads**: server file names reduced to one safe component (no path traversal, Windows-reserved names prefixed), no overwrite without `--overwrite`, written to a temp file then renamed | `display_name` is server-controlled. Partial files must not replace good ones. | |
| D16 | **`calendar` caps at 10 context codes**, putting the personal calendar first and warning the user | The docs say extra codes are silently ignored. | Silent truncation. |
| D17 | **Generic `get`/`api` escape hatches** with the same auth, automatic pagination, host check, dry-run guard and D11 quiz guard | "All features" includes endpoints not worth a dedicated command (colors, read state, comment edits, group join). | Dozens of thin commands. |
| D18 | **Time zones are left to the agent** (`whoami` returns `time_zone`) | `zoneinfo` needs the `tzdata` package on Windows, which would break D2. | Converting in the script. |
| D19 | **Repository docs in English**; answers to the user follow their language | Follows the global rule for text written into repositories. The skill's description gives English trigger examples and says the user may write in any language. | |
| D20 | **Unsupported submission types** (`media_recording`, `student_annotation`, `basic_lti_launch`) are not wrapped | They need Canvas's media or annotation UI or an LTI tool. The ids they need can't be produced from a CLI. | |

## 3. Testing

I followed red-green: `tests/test_canvas_api.py` was written first and failed on import, then the CLI was implemented until it passed.

The tests run the real `main()` over real HTTP against a local fake Canvas (`http.server`). A second server on another port plays the storage host, so cross-origin behavior is tested. 58 tests cover:

- configuration errors and URL normalization
- bearer auth, bracket array params, Link pagination with `--limit` (including `upcoming`, the generic `get` with `--key`, the `per_page` clamp and the truncation warning), both throttling forms with `Retry-After`
- error messages (401 hints, token redaction), refusal to contact foreign hosts
- the dry-run guard for every write command (no non-GET request is made)
- text submission encoding, type and extension pre-checks
- the 3-step upload in both 3xx and 201 forms (no token to storage, `file` field last, authenticated confirmation)
- downloads (token stripped on cross-host redirect, name sanitizing, no overwrite)
- planner override create/update, the calendar context cap, conversation read state, the send warning, threaded discussion rendering, quiz-submission unwrapping
- a smoke run of every read command against empty responses

Result: `Ran 58 tests ... OK` (43 at first release, 45 after the setx/BOM fixes, 58 after the 2026-11-13 audit).

**Full review, 2026-11-12.** I re-read every line of the script and re-checked doubtful usages against developerdocs.instructure.com (users, courses, submissions, assignments, calendar_events, peer_reviews, file uploads). Defects fixed, each with a test written first (red, then green):

| Defect | Effect before | Fix |
|---|---|---|
| `courses --state completed/all` sent no `state[]` | Canvas defaults `state[]` to `available` for students, so concluded courses were hidden | Also sends `state[]=available&state[]=completed` |
| A non-JSON success body (maintenance page, wrong base URL) | Python traceback | Clean error with an HTTP status and a snippet |
| User IDs interpolated raw into paths | `1/../users/self` could reach another endpoint | Each ID is escaped into one path segment (`:` and `@` kept for SIS ids) |
| `--limit 0` or negative | Empty results with a misleading warning | Rejected by argparse |
| Invalid `api --json`, non-UTF-8 `--text-file` | Traceback | Clean errors |
| Empty text submission | Sent to Canvas | Rejected locally |
| Exhausted attempts | Only a RESUBMISSION hint | The dry run shows "attempt N of M" and warns when no attempts are left (`allowed_attempts` + `extra_attempts`, -1 = unlimited) |
| Nickname of 60+ characters | Canvas error | Checked locally (doc: "shorter than 60 characters") |
| `calendar --limit` | Applied per event type, up to 2× the limit | Applied to the merged list, with a warning |
| SKILL.md still mentioned `get --all` | The agent would pass a removed flag | Updated |

Verified as already correct: `missing_submissions` filters, `users/self`, nickname endpoints, `todo` pagination, the 400 on a wrong submission type, the 10-calendar limit, and the upload `Location` header (step 3). Not stated in the docs, so left as-is: a `while(1);` JSON prefix (none documented for token requests) and peer-review visibility for students.

**Change, 2026-11-12:** at the user's request, every paginated call now follows pagination to the end. Before, `upcoming` and `get` without `--all` read only the first page. The pagination rule was re-checked on developerdocs.instructure.com. The tests for it were written first and failed (red) before the fix.

**Hardening audit, 2026-11-13.** Every file re-read; endpoints re-checked against canvas.instructure.com/doc/api (file.pagination, file.throttling, file.file_uploads, submissions, submission_comments, quiz_submissions, quiz_submission_questions, conversations, assignments, assignment_groups, new_quizzes) and, where the docs were silent, against canvas-lms source. Ideas from `vishalsachdev/canvas-mcp` (MIT) and `DMontgomery40/mcp-canvas-lms` were read as references only; no code was copied. Each defect got a failing test first (red, then green):

| Defect | Effect before | Fix |
|---|---|---|
| `get`/`api`/`upload --endpoint` reached quiz start, answer, complete and question endpoints | D11 and SKILL.md promised what the code did not enforce | Path is normalized (`%2F`, `//`, `..`) and refused for every request |
| Argument errors exited 2 with plain-text usage | Read as "configuration missing" (exit 2), and not JSON | JSON `{"kind": "usage"}` on stderr, exit 3 |
| Connection reset or truncated answer after the request was sent | Reported as "Local file error", or a traceback | `Network error` (exit 1), also during downloads |
| Still throttled after 3 retries | Hint said "not allowed for students" | Says it is a rate limit |
| `--text-file` with a UTF-8 BOM or UTF-16 (Notepad, PowerShell 5.1) | Invisible BOM submitted, or a misleading "not UTF-8" error | Decoded correctly |
| Token with quotes, spaces or non-ASCII characters | Traceback (`UnicodeEncodeError`) or a puzzling 401 | Config error that does not echo the token |
| `CANVAS_BASE_URL` copied with a path or query | Every call hit a wrong URL; the docstring already promised scheme://host[:port] | Reduced to the origin |
| A repeated `rel="next"` link | Infinite loop | Error |
| Group assignments and past-due submissions | Dry run silent | `WARNING` (group) and `LATE` in the summary |
| `send` subject over 255 characters; `uuid:` recipients | Canvas error; false "whole course" warning | Checked locally; only contexts are flagged |
| HTML tables (syllabus weights, schedules) | Cells merged (`Exam40%`) | Cells separated with a pipe |

Also confirmed and documented, without code changes: PowerShell 5.1 drops double quotes inside native arguments (reproduced), so SKILL.md sends free text through Bash; Canvas content is untrusted input (SKILL.md rule); the plural quiz-submissions read queues grading only of attempts already out of time.

## 4. Limitations and open points

- **No real-instance verification.** No Canvas account or token was available, so behavior against a live instance is untested. The likeliest gaps are fields that vary by Canvas version or institution settings, and the endpoints tagged *Summary*. Run `whoami`, `courses`, `planner` and a dry-run `submit` once with a real token.
- **Skill evals not run.** skill-creator's with/without-skill evaluation loop needs real Canvas data to judge outputs. Suggested first prompts: "What do I have to submit this week, and what am I missing?", "Summarize the feedback on my latest lab in <course>", "Submit <file> to the <assignment> assignment" (it should stop at the dry run).
- Teacher settings can hide grades, statistics or quiz results. Empty fields are expected then.
- Uploads read the whole file into memory. That's fine for typical coursework, not for multi-GB files.
- Personal tokens can be disabled or forced to expire by the institution. OAuth developer keys require an admin and are out of scope.
