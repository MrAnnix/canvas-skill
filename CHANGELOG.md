# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-13

First public release.

### Added

- `canvas-student` skill with a zero-dependency Python CLI for courses, grades, assignments, submissions (text, URL, file upload), feedback and rubrics, planner and calendar, modules, pages, files and downloads, announcements, discussions, quiz metadata and scores, groups and the inbox.
- Dry run for every write unless `--confirm` is passed; full pagination; throttling retries; token sent only to the configured Canvas origin.
- End-to-end tests against a local fake Canvas server, and CI on Linux and Windows.

### Security

- Quiz-taking and quiz-question endpoints are refused for every command, including `get`, `api` and `upload --endpoint`.
- A malformed token is a configuration error that never prints the value.
- `SKILL.md` tells the agent to treat Canvas content as data, not instructions.

### Fixed

- Argument errors are JSON with exit code 3 instead of plain text with exit code 2.
- Dropped connections are reported as network errors; persistent throttling is reported as a rate limit.
- `submit --text-file` reads UTF-8 with BOM and UTF-16; `CANVAS_BASE_URL` is reduced to its origin; pagination loops stop with an error.
- The submit dry run flags group assignments and late submissions; `send` checks the subject length and warns only for context recipients; HTML tables keep their cells apart.

[1.0.0]: https://github.com/MrAnnix/canvas-skill/releases/tag/v1.0.0
