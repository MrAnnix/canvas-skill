# canvas-student

A Claude skill that lets an agent work with **Canvas LMS as a student**: deadlines, grades, feedback, submissions, course materials, announcements, discussions and the inbox. It runs on a single Python script with no dependencies.

```
canvas-student/                 repository
├── canvas-student/             the skill (this folder is what gets installed)
│   ├── SKILL.md                instructions Claude loads when the skill triggers
│   ├── scripts/canvas_api.py   CLI client (Python 3.10+, standard library only)
│   └── references/
│       ├── commands.md         every command, option and example
│       └── api-endpoints.md    Canvas endpoint map with verification status
├── tests/test_canvas_api.py    end-to-end tests against a local fake Canvas
├── DECISIONS.md                research, design decisions and limitations
└── README.md
```

## 1. Get a Canvas token

1. Open Canvas → **Account** → **Settings**.
2. Under *Approved integrations*, click **+ New access token**. Give it a purpose and, ideally, an expiry date.
3. Copy the token. Canvas shows it only once, and it has the same power as your password.

Some institutions disable personal tokens. If the button is missing, ask your Canvas admin.

## 2. Configure (Windows PowerShell)

```powershell
setx CANVAS_BASE_URL "https://yourschool.instructure.com"
setx CANVAS_API_TOKEN "paste-token-here"
```

Open a new terminal (and restart Claude) so the variables are picked up. `setx` stores them in your user environment in plain text, which is readable by programs running as you. Revoke the token in Canvas when you no longer need it.

## 3. Install the skill

Copy or link the inner `canvas-student` folder into your personal skills folder:

```powershell
New-Item -ItemType Junction -Path "$env:USERPROFILE\.claude\skills\canvas-student" -Target "$PWD\canvas-student"
```

Check it works:

```powershell
python canvas-student\scripts\canvas_api.py whoami
```

Then ask Claude things like *"¿qué tengo pendiente esta semana?"*, *"what feedback did I get on the lab report?"* or *"descarga los PDFs del tema 3"*.

## 4. Safety model

- Reading is direct. **Every change (submit, comment, post, message, mark done) is a dry run** until `--confirm` is passed, and the skill tells Claude to show you the dry run and wait for your approval.
- The token is read only from the environment. It is sent only to your Canvas host, never to file-storage redirects or other hosts, and it is redacted from error messages.
- The CLI cannot take quizzes, and the skill won't submit work you didn't write.

## 5. Development

```powershell
python -m unittest discover -s tests -v
```

The 43 tests run the real CLI over HTTP against a local fake Canvas server. They cover full pagination of every list, throttling retries, error reporting, the 3-step upload, safe downloads, the dry-run guard and every command. They do **not** prove compatibility with a real Canvas instance. See *Limitations* in [DECISIONS.md](DECISIONS.md).
