# canvas-skill

[![tests](https://github.com/MrAnnix/canvas-skill/actions/workflows/tests.yml/badge.svg)](https://github.com/MrAnnix/canvas-skill/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Claude skills for Canvas LMS. It currently ships **`canvas-student`**, which lets an agent work with Canvas as a student: deadlines, grades, feedback, submissions, course materials, announcements, discussions and the inbox. It runs on a single Python script with no dependencies.

Not affiliated with or endorsed by Instructure. Canvas is a trademark of Instructure, Inc.

```
canvas-skill/                   repository
├── canvas-student/             the skill (this folder is what gets installed)
│   ├── SKILL.md                instructions Claude loads when the skill triggers
│   ├── scripts/canvas_api.py   CLI client (Python 3.10+, standard library only)
│   └── references/
│       ├── commands.md         every command, option and example
│       └── api-endpoints.md    Canvas endpoint map with verification status
├── tests/test_canvas_api.py    end-to-end tests against a local fake Canvas
├── DECISIONS.md                research, design decisions and limitations
├── CHANGELOG.md, CONTRIBUTING.md, SECURITY.md, LICENSE
└── README.md
```

## 1. Get a Canvas token

1. Open Canvas → **Account** → **Settings**.
2. Under *Approved integrations*, click **+ New access token**. Give it a purpose and, ideally, an expiry date.
3. Copy the token. Canvas shows it only once, and it has the same power as your password.

Some institutions disable personal tokens. If the button is missing, ask your Canvas admin.

## 2. Configure

Windows (PowerShell):

```powershell
setx CANVAS_BASE_URL "https://yourschool.instructure.com"
setx CANVAS_API_TOKEN "paste-token-here"
```

macOS or Linux: add both variables to your shell profile (for example `~/.zshrc` or `~/.bashrc`):

```bash
export CANVAS_BASE_URL="https://yourschool.instructure.com"
export CANVAS_API_TOKEN="paste-token-here"
```

Use the address you open Canvas with; any path after the host is ignored. Open a new terminal (and restart Claude) so the variables are picked up. Both methods store the token in plain text that programs running as you can read. Revoke the token in Canvas when you no longer need it.

## 3. Install the skill

Clone the repository and link the inner `canvas-student` folder into your personal skills folder.

Windows (PowerShell):

```powershell
git clone https://github.com/MrAnnix/canvas-skill.git
cd canvas-skill
New-Item -ItemType Junction -Path "$env:USERPROFILE\.claude\skills\canvas-student" -Target "$PWD\canvas-student"
```

macOS or Linux:

```bash
git clone https://github.com/MrAnnix/canvas-skill.git
cd canvas-skill
mkdir -p ~/.claude/skills && ln -s "$PWD/canvas-student" ~/.claude/skills/canvas-student
```

Check it works:

```bash
python canvas-student/scripts/canvas_api.py whoami
```

Then ask Claude things like *"¿qué tengo pendiente esta semana?"*, *"what feedback did I get on the lab report?"* or *"descarga los PDFs del tema 3"*. Run `git pull` in the clone to update.

## 4. Safety model

- Reading is direct. **Every change (submit, comment, post, message, mark done) is a dry run** until `--confirm` is passed, and the skill tells Claude to show you the dry run and wait for your approval.
- The token is read only from the environment. It is sent only to your Canvas host, never to file-storage redirects or other hosts, and it is redacted from error messages.
- The CLI refuses to start, answer or complete quizzes or to read quiz questions, and the skill won't submit work you didn't write.
- Text from Canvas (posts, messages, descriptions) is treated as data, never as instructions to the agent.

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## 5. Development

```bash
python -m unittest discover -s tests -v
```

The 58 tests run the real CLI over HTTP against a local fake Canvas server, on Linux and Windows with Python 3.10 and 3.15 in CI. They cover full pagination of every list, throttling retries, error reporting, the 3-step upload, safe downloads, the dry-run guard and every command. They do **not** prove compatibility with every Canvas instance. See *Limitations* in [DECISIONS.md](DECISIONS.md) and the rules in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
