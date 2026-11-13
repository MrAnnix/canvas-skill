# canvas_api.py command reference

Invoke every command as `python <skill-dir>/scripts/canvas_api.py COMMAND [args]`. Every command accepts:

- `--limit N`: cap the number of list items. Lists are otherwise fully paginated (100 per page, following `Link` headers). When a cap cuts results short, stderr shows `{"warning": "Showing the first N results; more exist..."}`.
- `--full`: return the raw Canvas JSON instead of the compact view.

Commands marked **(write)** also accept `--confirm`. Without it they only print a dry run.

IDs can be numeric or SIS-style (`sis_course_id:MAT-101`). They are URL-escaped into a single path segment. Output is JSON on stdout. Errors are JSON on stderr. Exit codes: 0 ok, 1 API/validation/file/network error, 2 configuration error, 3 invalid arguments (`{"error": ..., "kind": "usage"}`).

## Contents
1. Profile and courses
2. Assignments and submissions
3. Overview: to-do, planner, calendar
4. Course content: modules, pages, files
5. Announcements and discussions
6. Quizzes and groups
7. Inbox
8. Settings
9. Generic access to any endpoint

## 1. Profile and courses

| Command | What it returns |
|---|---|
| `whoami` | id, name, email, login, `time_zone`, locale. Use it to check the token. |
| `courses [--state active\|completed\|invited_or_pending\|all]` | Student courses with term and current grade (`computed_current_score/grade`, current grading period). Default `active`. `completed` and `all` include concluded courses. |
| `course COURSE` | Details, teachers, module progress and the syllabus as plain text. |
| `tabs COURSE` | Navigation sections the course shows (useful to know whether Files or Pages are enabled). |
| `grades [--course ID] [--grading-period ID] [--include-completed]` | Per course: `current_score`, `current_grade`, `final_score`, `final_grade`, `current_points`. |
| `grading-periods COURSE` | Periods with dates and `is_closed`. Use their ids with `grades --grading-period`. |

`current_score` counts only graded work, while `final_score` treats ungraded work as zero. Explain this when the two differ.

## 2. Assignments and submissions

| Command | What it returns |
|---|---|
| `assignments COURSE [--bucket B] [--search TEXT]` | Ordered by due date, each with your `submission` status (`workflow_state`, `score`, `grade`, `late`, `missing`). Buckets: `past overdue undated ungraded unsubmitted upcoming future`. |
| `assignment COURSE ASSIGNMENT` | Description (text), `submission_types`, `allowed_extensions`, `allowed_attempts`, rubric criteria, lock info. |
| `submission COURSE ASSIGNMENT [--history]` | Your submission: grade, score, text body, attachments, teacher comments, rubric assessment, earlier attempts. |
| `submissions COURSE [--workflow-state submitted\|unsubmitted\|graded\|pending_review] [--graded-since DATE]` | All your submissions in the course with the assignment name. |
| `submit COURSE ASSIGNMENT (--text T \| --text-file F \| --url U \| --file F [--file F2]) [--html] [--comment C]` **(write)** | Submits an assignment. Before anything is sent it checks that the assignment accepts that type, is not locked, that file extensions are allowed and that the text is non-empty UTF-8. The dry run flags a RESUBMISSION, warns when no attempts are left (`allowed_attempts` plus `extra_attempts`), warns on group assignments (the submission may count for the whole group) and says LATE when the due date has passed. `--text-file` accepts UTF-8 with or without BOM and UTF-16 with BOM (what Notepad and PowerShell 5.1 write). Plain text becomes HTML paragraphs unless `--html` is given. Files go through the 3-step upload and are then attached. |
| `comment COURSE ASSIGNMENT --text T [--file F] [--attempt N]` **(write)** | Adds a comment, with optional files, to your submission. |
| `peer-reviews COURSE ASSIGNMENT` | Peer reviews visible to you (assigned/completed). |

Examples:
```
assignments 4321 --bucket upcoming
submit 4321 998 --file "C:\Users\me\Docs\practica2.pdf" --comment "Adjunto la práctica 2"
submit 4321 999 --text-file respuesta.txt
```

## 3. Overview: to-do, planner, calendar

| Command | What it returns |
|---|---|
| `planner [--start D] [--end D] [--filter new_activity\|incomplete_items\|complete_items] [--course ID]...` | Default today to +14 days. Each item has type, title, due date, `status` (submitted, graded, missing, late, with_feedback...) and `marked_complete`. This is the best single answer to "what do I have to do". |
| `todo` | The Canvas to-do sidebar: items needing submission, including ungraded quizzes. |
| `missing` | Past-due work with no submission, with course name. |
| `upcoming` | Upcoming calendar events and assignments, as on the dashboard (paginated endpoint). |
| `calendar [--start D] [--end D] [--course ID]... [--type event\|assignment\|all]` | Your personal calendar plus courses. Canvas allows at most 10 calendars per request, so with more than 9 active courses a warning is shown; pass `--course`. |
| `activity [--summary]` | Recent activity stream (default 30 items), or counts per type. |
| `planner-done TYPE ID [--undo]` **(write)** | Ticks an item in your planner only. It does not submit anything. Types: `assignment quiz discussion_topic wiki_page announcement planner_note calendar_event ...`. |
| `planner-notes [--start D] [--end D]` | Your personal to-do notes. |
| `planner-note-add --title T --date YYYY-MM-DD [--details X] [--course ID]` **(write)** | Personal reminder in the planner. |
| `planner-note-delete ID` **(write)** | |
| `event-add --title T --start ISO [--end ISO] [--description X] [--location L]` **(write)** | Event in your personal calendar. Give an offset in the time: `2026-11-20T16:00:00+01:00`. |
| `event-delete ID` **(write)** | Deletes one of your events. |

Dates accept `YYYY-MM-DD` or ISO 8601. Canvas returns UTC.

## 4. Course content: modules, pages, files

| Command | What it returns |
|---|---|
| `modules COURSE` | Modules in order with `state` (locked/unlocked/started/completed) and items (type, title, `content_id`, `page_url`, completion `requirement`, due date). It fetches item lists that Canvas leaves out. |
| `module-done COURSE MODULE ITEM [--undo]` **(write)** | Fulfils a "mark as done" requirement. |
| `module-read COURSE MODULE ITEM` **(write)** | Fulfils a "must view" requirement. |
| `pages COURSE [--search T]` | Wiki pages (`url` slug, title, updated). |
| `page COURSE SLUG` / `page COURSE --front` / `page COURSE page_id:123` `[--html]` | Page body as plain text, with links kept as `text (url)`. |
| `files [COURSE] [--folder ID] [--search T] [--content-type application/pdf] [--sort name\|size\|created_at\|updated_at\|content_type]` | Course files, a folder's files, or (no args) your personal files. |
| `folders [COURSE]` | Folder tree with file counts. |
| `download FILE_ID [--dest DIR] [--overwrite]` | Saves the file. The name is sanitized and an existing file is not overwritten without `--overwrite`. The token is sent only to the Canvas host, never to storage redirects. |
| `quota` | Personal storage `quota` / `quota_used` (bytes). |
| `upload FILE [--folder-path P] [--endpoint groups/ID/files]` **(write)** | Uploads to your personal files (default) or a group's files. Duplicates are renamed. |

Module items of type `Page` give a `page_url` for `page`. Type `File` gives a `content_id` for `download`. Types `Assignment`, `Quiz` and `Discussion` give a `content_id` for `assignment`, `quiz` and `discussion`.

## 5. Announcements and discussions

| Command | What it returns |
|---|---|
| `announcements [--course ID]... [--start D] [--end D]` | Default: all active courses, last 14 days. Each has title, date, author and message text. |
| `discussions COURSE [--unread] [--search T]` | Topics by recent activity, with unread counts and `require_initial_post`. |
| `discussion COURSE TOPIC` | The topic text plus threaded replies with author names. If the topic requires your post first, it says so. |
| `discussion-post COURSE TOPIC --message M [--reply-to ENTRY] [--html]` **(write)** | New entry or reply, visible to the class. |
| `discussion-read COURSE TOPIC` **(write)** | Marks the topic and all its entries read. |

## 6. Quizzes and groups

| Command | What it returns |
|---|---|
| `quizzes COURSE [--new]` | Classic quizzes (time limit, attempts, question count, dates), or New Quizzes with `--new`. |
| `quiz COURSE QUIZ` | Settings, instructions, and your attempts with `score`, `kept_score`, time spent. |
| `groups` | Your groups. |
| `group-members GROUP` | Members (id, name). |

The CLI never starts, answers or submits quizzes. See the academic-integrity section in SKILL.md.

## 7. Inbox

| Command | What it returns |
|---|---|
| `inbox [--scope unread\|starred\|archived\|sent]` | Conversations (default 30) with participants and last message. |
| `unread-count` | `{"unread_count": "N"}`. |
| `conversation ID [--mark-read]` | Full thread. It does **not** mark the thread as read unless `--mark-read` is given, so reading through Claude doesn't hide new messages from the user. |
| `recipients --search NAME [--context course_ID] [--type user\|context]` | Finds user ids (or course/group contexts) to message. |
| `send --to ID [--to ID2] --subject S --body B [--course ID] [--group]` **(write)** | New message. `--to` accepts user ids or contexts like `course_123`, which can reach a whole course, and the dry run then shows a WARNING for any recipient that is not a numeric user id or `uuid:...`. The subject is limited to 255 characters. Without `--group`, each recipient gets a private copy. |
| `reply ID --body B` **(write)** | Replies in a conversation. |
| `conversation-update ID [--state read\|unread\|archived] [--star\|--unstar]` **(write)** | |

## 8. Settings

| Command | What it returns |
|---|---|
| `nicknames` | Your course nicknames. |
| `nickname COURSE --set NAME` / `--clear` **(write)** | Personal course nickname, shown only to you. Must be shorter than 60 characters. |
| `channels` | Your notification channels (email/push/sms addresses). |

## 9. Generic access to any endpoint

```
get PATH [-p KEY=VALUE]... [--key NAME] [--limit N]
api POST|PUT|DELETE PATH [-d KEY=VALUE]... [--json '{...}'] [--confirm]
```
- `PATH` is relative to `/api/v1/` (`users/self/colors`), absolute (`/api/quiz/v1/...`), or a full URL on the same Canvas host. Other hosts are refused, so the token can't leak. Quiz-taking and quiz-question endpoints (`.../quizzes/ID/submissions` writes, `quiz_submissions/...`, `.../questions`, New Quizzes `items`) are refused for academic integrity; `GET .../quizzes/ID/submissions` (your attempts) stays available.
- Repeat `-p`/`-d` for arrays and use Canvas bracket names: `-p include[]=term -p include[]=teachers`, `-d calendar_event[title]=X`.
- `get` always follows pagination when the response is a list. `-p per_page=N` is honored up to 100. For lists wrapped in an object (`{"quiz_submissions": [...]}`, `{"grading_periods": [...]}`), pass `--key quiz_submissions` so every page is merged. Object responses are returned as-is.
- Use these for endpoints listed in `api-endpoints.md` that have no dedicated command (for example `users/self/colors`, `courses/ID/users/self/progress`, submission read state, or `get courses/ID/assignment_groups -p include[]=assignments -p include[]=submission` for grade weights per group).
