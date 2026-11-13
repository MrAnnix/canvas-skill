# Security policy

This skill acts on a student's Canvas account with a personal access token, which Canvas treats as equivalent to a password. Security reports are welcome.

## Reporting a vulnerability

Report it privately through GitHub: **Security → Report a vulnerability** on this repository. Please don't open a public issue for a vulnerability, and never include a real Canvas token, URL of your institution or personal data in a report. A fake token and `https://myschool.instructure.com` are enough to describe a problem.

You can expect an acknowledgement within a week. Fixes land on `main` with a regression test.

## In scope

- The token leaving the configured Canvas origin: other hosts, file-storage redirects, upload URLs, logs, error messages or dry-run output.
- A write reaching Canvas without `--confirm`.
- A way around the academic-integrity guard (starting, answering or completing quizzes, or reading quiz questions).
- Path traversal or overwriting files through downloads.
- Instructions in `SKILL.md` that would lead an agent to act on text written by others in Canvas.

## Out of scope

- What Canvas itself allows a student token to do. Report those to Instructure.
- Problems that require an attacker who already controls the user's machine or environment variables.

## Supported versions

Only the latest commit on `main` is supported.
