# Canvas REST API: student endpoint map

Reviewed on 2026-11-12 against the official docs at https://canvas.instructure.com/doc/api/. Those docs say they are moving to https://developerdocs.instructure.com/services/canvas.

Paths are relative to `https://<canvas>`. `self` can replace any `:user_id`, per users.html ("the `:user_id` parameter can be replaced with `self`") and file.object_ids.html.

**Verified** means the endpoint was read in the verbatim doc text. **Summary** means it came from a truncated page via an extracted summary, so confirm with a real call before relying on it. **CLI** names the command that wraps it; `get`/`api` means no dedicated command.

## Contents
1. Conventions
2. Users, profile, activity
3. Courses, enrollments, grades
4. Assignments and submissions
5. Planner and calendar
6. Modules, pages, files
7. Discussions and announcements
8. Quizzes
9. Groups, peer reviews, rubrics
10. Conversations and search
11. Settings and notifications
12. Deliberately not wrapped

## 1. Conventions (verified)

| Topic | Rule | Source |
|---|---|---|
| Auth | `Authorization: Bearer <token>`. A query-string token is discouraged. Manual tokens are password-equivalent. A 401 with `WWW-Authenticate` means a bad or expired token. | file.oauth.html |
| Pagination | Lists default to 10 items. The general page says "an unspecified limit" for `per_page`, while per-endpoint docs say "Maximum of 100". The CLI requests 100 (clamping larger values) and always follows `Link` rel `next` until it is absent. URLs are opaque, so follow them verbatim. Header case is not guaranteed. | file.pagination.html (re-checked on developerdocs 2026-11-12) |
| Throttling | The page says throttled requests get "429 Forbidden (Rate Limit Exceeded)". The CLI also retries a 403 whose body says "Rate Limit Exceeded": this is an inference, since that mismatched wording suggests older instances may send 403. `X-Request-Cost` is always present; `X-Rate-Limit-Remaining` appears when throttling applies. Parallel requests pay a pre-flight penalty. No backoff schedule is documented. | file.throttling.html |
| Encoding | POST/PUT take form encoding or JSON. Arrays repeat `key[]`, nested values use `a[b]`. Timestamps are ISO 8601 UTC. Booleans accept `true/false`. | index.html |
| Uploads | 1) POST `name,size,content_type,parent_folder_path,on_duplicate` to a files endpoint, which returns `upload_url` and `upload_params`. 2) multipart POST to `upload_url` with every `upload_params` field unchanged, `file` last, and **no token**. 3) a 3xx means GET `Location` with the token; a 201 means GET `Location` for the File JSON. | file.file_uploads.html |

## 2. Users, profile, activity

| Endpoint | CLI | Status |
|---|---|---|
| GET /api/v1/users/self/profile | `whoami` | Summary (endpoint), verified (Profile object) |
| GET /api/v1/users/self ("Shows details for user"; the doc example uses `self`) | used internally for the user id | Verified |
| GET /api/v1/users/self/activity_stream `only_active_courses` | `activity` | Verified |
| GET /api/v1/users/self/activity_stream/summary (not paginated: one row per type) | `activity --summary` | Verified |
| DELETE /api/v1/users/self/activity_stream/:id | `api DELETE` | Summary |
| GET /api/v1/users/self/todo `include[]=ungraded_quizzes`, `course_ids[]` (paginated) | `todo` | Verified |
| GET /api/v1/users/self/todo_item_count | `get` | Verified |
| GET /api/v1/users/self/upcoming_events ("A paginated list") | `upcoming` | Verified |
| GET /api/v1/users/self/missing_submissions `include[]=course,planner_overrides`, `filter[]=submittable,current_grading_period` (paginated) | `missing` | Verified |
| GET/PUT /api/v1/users/self/colors[/:asset_string] `hexcode` | `get` / `api PUT` | Summary |

## 3. Courses, enrollments, grades

| Endpoint | CLI | Status |
|---|---|---|
| GET /api/v1/courses `enrollment_type=student`, `enrollment_state`, `state[]` (defaults to `available` for students, so `--state completed/all` also sends `available,completed`), `include[]=term,total_scores,current_grading_period_scores,syllabus_body,teachers,course_progress...` | `courses` | Verified |
| GET /api/v1/courses/:id `include[]` | `course` | Summary |
| GET /api/v1/courses/:id/users/self/progress | `get` | Verified (`self` explicitly allowed) |
| GET /api/v1/courses/:id/tabs | `tabs` | Verified |
| GET /api/v1/courses/:id/activity_stream[/summary], /todo | `get` | Summary |
| GET /api/v1/courses/:id/settings, /effective_due_dates | `get` | Summary |
| GET /api/v1/users/self/enrollments `type[]=StudentEnrollment`, `state[]`, `include[]=current_points`, `grading_period_id` → `grades{current_score,final_score,current_grade,final_grade}` | `grades` | Verified ("a user can return his/her own enrollments") |
| POST /api/v1/courses/:id/enrollments/:id/accept and /reject | `api POST` | Verified |
| GET /api/v1/courses/:id/grading_periods → `{"grading_periods":[...]}` | `grading-periods` | Verified |

## 4. Assignments and submissions

| Endpoint | CLI | Status |
|---|---|---|
| GET /api/v1/courses/:id/assignments `bucket`, `order_by`, `search_term`, `include[]=submission,all_dates,score_statistics` | `assignments` | Verified |
| GET /api/v1/courses/:id/assignments/:id `include[]=submission` | `assignment`, `submit` (pre-check) | Summary |
| GET /api/v1/courses/:id/assignments/:id/submissions/self `include[]=submission_comments,rubric_assessment,submission_history` | `submission` | Verified (path documented as `:user_id`, `self` per users.html) |
| GET /api/v1/courses/:id/students/submissions (omitting `student_ids` returns the caller's own) `workflow_state`, `graded_since`, `include[]=assignment` | `submissions` | Verified |
| POST /api/v1/courses/:id/assignments/:id/submissions `submission[submission_type]` = `online_text_entry\|online_url\|online_upload\|media_recording\|basic_lti_launch\|student_annotation`, `submission[body]`, `submission[url]`, `submission[file_ids][]`, `comment[text_comment]` | `submit` (text, url, upload) | Verified. A type the assignment doesn't accept returns 400. Needs an active enrollment. |
| POST /api/v1/courses/:id/assignments/:id/submissions/self/files (upload step 1) | `submit --file` | Verified |
| PUT /api/v1/courses/:id/assignments/:id/submissions/self `comment[text_comment]`, `comment[file_ids][]`, `comment[attempt]` | `comment` | Verified params; student use inferred (grading fields need grade permission) |
| POST .../submissions/self/comments/files (comment attachment upload) | `comment --file` | Verified |
| PUT/DELETE .../submissions/self/comments/:id `comment` (edit/delete a comment) | `api` | Verified (ownership not stated) |
| PUT/DELETE .../submissions/self/read; PUT .../read/:item (`grade`, `comment`, `rubric`) | `api` | Summary |

## 5. Planner and calendar

| Endpoint | CLI | Status |
|---|---|---|
| GET /api/v1/planner/items `start_date`, `end_date`, `context_codes[]`, `filter` (not `filter[]`) | `planner` | Verified |
| GET/POST/PUT/DELETE /api/v1/planner_notes `title`, `details`, `todo_date`, `course_id` | `planner-notes`, `planner-note-add`, `planner-note-delete` | Verified |
| GET/POST/PUT/DELETE /api/v1/planner/overrides `plannable_type`, `plannable_id`, `marked_complete`, `dismissed` | `planner-done` | Verified (PUT labels the booleans as strings; `true/false` are sent) |
| GET /api/v1/calendar_events `type=event\|assignment\|sub_assignment`, `start_date`, `end_date`, `context_codes[]` (max 10, extras ignored, default personal only) | `calendar` | Verified |
| POST /api/v1/calendar_events `calendar_event[context_code]=user_<id>`, `[title]`, `[start_at]`, `[end_at]`, `[description]`, `[location_name]` | `event-add` | Verified params; student permission not stated, so test it |
| DELETE /api/v1/calendar_events/:id | `event-delete` | Verified |
| POST /api/v1/calendar_events/:id/reservations (appointment slots) | `api POST` | Verified |

## 6. Modules, pages, files

| Endpoint | CLI | Status |
|---|---|---|
| GET /api/v1/courses/:id/modules `include[]=items,content_details` (`items` may be omitted when large; then use `items_url`) | `modules` | Verified |
| GET .../modules/:id/items `include[]=content_details` | `modules` (fallback) | Verified |
| PUT/DELETE .../modules/:m/items/:i/done; POST .../mark_read | `module-done`, `module-read` | Verified |
| GET /api/v1/courses/:id/module_item_sequence `asset_type`, `asset_id` | `get` | Verified |
| GET /api/v1/courses/:id/pages `sort`, `search_term`; GET .../pages/:url_or_id (`page_id:N` forces id); GET .../front_page | `pages`, `page` | Verified |
| GET /api/v1/courses/:id/files, /folders/:id/files, /users/self/files `search_term`, `content_types[]`, `sort`, `order` | `files` | Verified |
| GET /api/v1/files/:id → `url` (download), `locked_for_user` | `download` | Verified. Redirect behavior is undocumented, so the CLI handles it defensively. |
| GET /api/v1/courses/:id/folders, /folders/:id/folders, .../folders/by_path/*path | `folders`, `get` | Verified |
| GET /api/v1/users/self/files/quota | `quota` | Verified for `:user_id` |
| POST /api/v1/users/self/files, /groups/:id/files (upload) | `upload` | Verified |

## 7. Discussions and announcements

| Endpoint | CLI | Status |
|---|---|---|
| GET /api/v1/courses/:id/discussion_topics `order_by`, `filter_by=unread`, `search_term`, `only_announcements` | `discussions` | Verified |
| GET .../discussion_topics/:id | `discussion` | Verified |
| GET .../discussion_topics/:id/view (403 when an initial post is required, 503 while the cache builds) | `discussion` | Summary |
| POST .../discussion_topics/:id/entries `message`; POST .../entries/:id/replies | `discussion-post` | Summary |
| PUT .../discussion_topics/:id/read_all; PUT/DELETE .../read; .../entries/:id/read | `discussion-read`, `api` | Summary |
| POST .../entries/:id/rating `rating` (0 or 1) | `api POST` | Summary |
| PUT/DELETE .../entries/:id (own entry) `message` | `api` | Verified |
| GET /api/v1/announcements `context_codes[]` (required), `start_date` (default -14 days), `end_date`, `latest_only` | `announcements` | Verified |

## 8. Quizzes

| Endpoint | CLI | Status |
|---|---|---|
| GET /api/v1/courses/:id/quizzes, /quizzes/:id | `quizzes`, `quiz` | Verified. No question text is returned. |
| GET .../quizzes/:id/submissions → `{"quiz_submissions":[...]}` (a student sees only their own) | `quiz` | Verified |
| GET .../quizzes/:id/submission, .../submissions/:id/time | `get` | Verified |
| GET /api/quiz/v1/courses/:id/quizzes (New Quizzes) | `quizzes --new` | Verified path; student access not stated |
| POST .../quizzes/:id/submissions (start), POST .../submissions/:id/complete | **not wrapped** | Verified; excluded on purpose (see DECISIONS.md) |

## 9. Groups, peer reviews, rubrics

| Endpoint | CLI | Status |
|---|---|---|
| GET /api/v1/users/self/groups; GET /api/v1/groups/:id; GET /api/v1/groups/:id/users | `groups`, `group-members` | Verified |
| POST /api/v1/groups/:id/memberships `user_id=self`; DELETE /api/v1/groups/:id/users/self | `api` | Verified |
| GET .../assignments/:id/peer_reviews `include[]=user,submission_comments` | `peer-reviews` | Verified; student visibility not stated |
| POST .../assignments/:id/allocate (get a peer review assigned) | `api POST` | Verified |
| Rubric for an assignment: the `rubric` field of the Assignment object | `assignment` | Rubric endpoints don't state student access |

## 10. Conversations and search

| Endpoint | CLI | Status |
|---|---|---|
| GET /api/v1/conversations `scope=unread\|starred\|archived\|sent`, `filter[]` | `inbox` | Verified |
| GET /api/v1/conversations/:id `auto_mark_as_read` (default true) | `conversation` (sends false) | Verified |
| POST /api/v1/conversations `recipients[]`, `subject`, `body`, `context_code`, `group_conversation`, `attachment_ids[]` (an existing private conversation with the same recipients is reused) | `send` | Verified |
| POST /api/v1/conversations/:id/add_message `body` | `reply` | Verified |
| PUT /api/v1/conversations/:id `conversation[workflow_state]`, `conversation[starred]` | `conversation-update` | Verified |
| GET /api/v1/conversations/unread_count; POST /mark_all_as_read | `unread-count`, `api POST` | Verified |
| GET /api/v1/search/recipients `search`, `context`, `type=user\|context`, `exclude[]` | `recipients` | Verified (`find_recipients` is deprecated) |

## 11. Settings and notifications

| Endpoint | CLI | Status |
|---|---|---|
| GET/PUT/DELETE /api/v1/users/self/course_nicknames[/:course_id] `nickname` ("non-empty and shorter than 60 characters") | `nicknames`, `nickname` | Verified (GET, PUT); DELETE summary |
| GET /api/v1/users/self/communication_channels | `channels` | Verified (`self` explicitly allowed) |
| GET .../communication_channels/:id/notification_preferences | `get` | Verified |

## 12. Deliberately not wrapped

| Area | Why |
|---|---|
| Quiz start/answer/complete (classic and New Quizzes) | Academic integrity: an agent taking graded quizzes for the student. Question endpoints aren't wrapped either. |
| `media_recording`, `student_annotation`, `basic_lti_launch` submissions | They need Canvas's media or annotation UI or an LTI tool. Use `api POST` only if the user insists and has the ids. |
| External tool sessionless launch | Returns a launch URL meant for a browser session. Not useful from a CLI. |
| Instructor/admin endpoints (grading, course editing, enrollment management, page edits) | Students lack the permissions. They would only produce 401/403 errors. |
| OAuth2 developer-key flow | Needs an admin-issued developer key. A personal token is the documented path for one's own account. |
