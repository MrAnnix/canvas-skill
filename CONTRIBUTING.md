# Contributing

Thanks for helping. This project keeps a few rules because it handles a student's account and academic record.

## Ground rules

- **Read [DECISIONS.md](DECISIONS.md) first.** Changes must keep its design decisions, especially: the token comes only from `CANVAS_API_TOKEN` (D3), it is sent only to the configured Canvas origin (D4), writes are dry runs without `--confirm` (D6), and quiz-taking stays blocked (D11). To change a decision, open an issue with evidence first.
- **Standard library only** (Python 3.10+). No dependencies, so there is nothing to install or audit.
- **Keep `canvas-student` student-only.** Instructor and admin endpoints fail with a student token and would only add noise to its instructions. Teacher or admin support belongs in a separate skill folder with its own safety rules; open an issue to discuss it first.
- **Check the official docs** at https://canvas.instructure.com/doc/api/ for every endpoint you add or change, and record its status in `canvas-student/references/api-endpoints.md`.
- **Never test writes against a real account.** Use the fake server in `tests/`. Never commit a token, an institution URL or personal data.
- Keep backward compatibility for existing commands, flags, output fields and exit codes.

## Workflow

1. Write a failing test in `tests/test_canvas_api.py` that shows the bug or the new behavior.
2. Make the smallest change that makes it pass.
3. Run the full suite:

   ```
   python -m unittest discover -s tests -v
   ```

4. Update `SKILL.md`, `references/` and `README.md` when behavior changes, so the docs match the code.
5. Open a pull request with one focused change and a clear English description.

Repository text (code, comments, docs, commits) is in US English.
