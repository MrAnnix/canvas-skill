---
name: canvas-student
description: >-
  Work with the Canvas LMS as a student through its REST API, using a bundled zero-dependency Python CLI. It covers courses, grades, assignments and deadlines, the to-do list, planner and calendar, missing work, submitting assignments (text, URL or file uploads), feedback and rubrics, modules, pages, files and downloads, announcements, discussions, quizzes (metadata and scores), groups and the Canvas inbox. Use this skill whenever the user mentions Canvas, Instructure, their LMS or "campus virtual", or asks about their courses, homework, deadlines, grades, teacher feedback, course materials, announcements or messages from teachers, even without naming Canvas. Examples: "¿qué tengo pendiente esta semana?", "entrega la práctica 2", "qué nota llevo en Física", "descarga los apuntes del tema 3", "did my professor reply?".
compatibility: Python 3.10+ (standard library only). Needs the CANVAS_BASE_URL and CANVAS_API_TOKEN environment variables.
---

# Canvas for students

This skill lets you read and act on the student's own Canvas account through `scripts/canvas_api.py`. The script handles authentication, pagination, rate limits, the 3-step file upload and safe downloads. It prints compact JSON, so read it and answer the user in plain language rather than pasting raw JSON.

## Setup check

Run the CLI as `python <this skill's directory>/scripts/canvas_api.py COMMAND`. Use the absolute path, because the working directory is usually elsewhere. The command names below omit that prefix. To filter its JSON, parse it with `ConvertFrom-Json` in PowerShell or run the pipeline in Bash: Windows PowerShell 5.1 prefixes text piped into another program with a BOM, which breaks `json.load`.

Start each session with `whoami`. Exit code 2 means configuration is missing. If the error says the variable is saved in the Windows user environment, it is already set: the user only needs to fully restart Claude. Otherwise, walk the user through it:

1. `CANVAS_BASE_URL`: the address they use to open Canvas, e.g. `https://myschool.instructure.com`.
2. `CANVAS_API_TOKEN`: Canvas → Account → Settings → *Approved integrations* → **New access token**. Canvas shows the token once, and it has the same power as their password.

The user sets these variables themselves (PowerShell: `setx CANVAS_API_TOKEN "..."`, then fully restart Claude, because `setx` only reaches programs started afterwards). Never ask the user to paste the token into the chat. Never echo it, write it to files, or pass it as an argument, because chat logs and shell history would keep a copy. Some institutions disable personal tokens. If so, the user has to ask their Canvas admin; there is no workaround to offer.

`whoami` returns `time_zone`. Canvas timestamps are UTC (`...Z`), so convert due dates to that time zone before telling the user, and give relative dates when helpful ("mañana a las 23:59").

## How changes work: dry run, then confirm

Read-only commands run directly. Every command that changes something in Canvas (submit, comment, post, send, mark done, create or delete) prints a **dry run** unless you pass `--confirm`:

1. Run the command without `--confirm`.
2. Show the user the `summary` and anything they wrote: the text, the file names, the recipients.
3. Rerun with `--confirm` only after the user explicitly approves that exact action.

The reason: submissions, posts and messages are visible to teachers or classmates, and some can't be undone (a submission counts as an attempt). Approval covers one action; ask again for the next one. If a dry-run summary says `RESUBMISSION` or `WARNING` (no attempts left, or a message that reaches a whole course), point it out to the user.

## Academic integrity

Help the student learn and manage their work. Don't do graded work in their name:

- Submit only content the student wrote or explicitly gave you to submit. If they ask you to write the assignment and submit it, explain that you can help them understand, plan, outline or review their own draft, but the submitted work has to be theirs.
- Quizzes and exams: the CLI refuses to start, answer or complete quizzes and to read quiz questions, including through `get` and `api`. Help with preparation (explain topics from the course pages and files, make practice questions) and with reading results afterwards (`quiz`, `submission`).
- Discussion posts and messages go out under the student's name. Draft them with the student and send only what they approve.

## Common workflows

| The user wants... | Commands |
|---|---|
| Weekly overview, "¿qué tengo pendiente?" | `planner` (next 14 days, with submission status), then `missing`; `announcements` for news |
| Grades | `grades`, or `courses` (includes current grade); per assignment `submissions COURSE --workflow-state graded` |
| Feedback on a task | `submission COURSE ASSIGNMENT` (score, teacher comments, rubric assessment) |
| Understand an assignment | `assignment COURSE ASSIGNMENT` (description as text, rubric, accepted file types, attempts) |
| Submit | `assignment` to check type, then `submit COURSE ASSIGNMENT --file path` / `--text` / `--url` (dry run → confirm) |
| Study material | `modules COURSE` (structured syllabus), `page COURSE SLUG`, `files COURSE --search tema`, `download FILE_ID --dest folder` |
| Messages | `unread-count`, `inbox --scope unread`, `conversation ID`; `recipients --search name --context course_ID` then `send` |
| Discussions | `discussions COURSE --unread`, `discussion COURSE TOPIC`, `discussion-post` (dry run → confirm) |
| Calendar | `calendar` (events plus due dates), `event-add` for personal events, `planner-note-add` for reminders |

Course and assignment IDs come from `courses` and `assignments`. When the user names a course loosely ("Física"), list courses and match by name, and ask if two match. Before a long chain of calls, make the cheap overview call first (`planner`, `courses`), because it often answers the question.

Every list is fully paginated: the CLI follows Canvas's `Link` headers until the end, so results are complete. Useful options on every command: `--limit N` caps list size; `--full` returns the raw Canvas JSON when the compact view lacks a field you need. If stderr shows a warning that more results exist (from `--limit` or a default cap such as the 30 newest inbox items), tell the user or rerun with a higher `--limit` before concluding something is missing.

## Command reference

Read `references/commands.md` for every command with its options and examples. Read `references/api-endpoints.md` when you need an endpoint the CLI doesn't wrap. You can then call it with `get PATH -p key=value [--key NAME]` or `api METHOD PATH -d key=value` (dry run → confirm), which handle auth, full pagination and host checks. That file also records which behaviors were verified against the official docs.

## Errors

Errors arrive on stderr as JSON with exit code 1:

- `HTTP 401 ... token is invalid, expired or revoked`: the user needs a new token.
- `HTTP 401 ... permission` or `HTTP 403`: not available to students in this course, or the item is locked. Say so rather than retrying.
- `HTTP 404`: wrong ID, or the item is hidden from students (unpublished, not yet available).
- Rate limits (HTTP 429, or 403 "Rate Limit Exceeded") are retried automatically. Run commands one at a time instead of in parallel, since Canvas penalises concurrent requests.
- `Refusing ... quiz`: the academic-integrity guard blocked a quiz-taking or quiz-question endpoint. Don't look for a way around it.
- `This assignment accepts [...]`, `Extension not allowed` or `The text to submit is empty`: the submission was blocked locally before anything was sent. Tell the user what the assignment accepts.
- `response that is not JSON`: Canvas answered with a web page instead of data, usually maintenance or a wrong `CANVAS_BASE_URL`. Show the snippet to the user.

Some data depends on the teacher's settings. Grades may be hidden, `score_statistics` may be absent and quiz results may be hidden. An empty field usually means the teacher hasn't released it, not that the call failed.
