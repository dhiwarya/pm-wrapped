# pm-dash

Code of the Project Manager Dashboard (PMD): a one-user web dashboard that answers "will this project pass its next gate?". The design lives in a separate, local design package.

## Status

**Phase 1, manual tracker: built.** Projects, gates, acceptance evidence, health confirmed by hand, tasks with late days, holidays and working days, the Today view and the project pages, in the bold tile look. There is no run yet, so nothing is read from repositories.

| Phase | Content | State |
|---|---|---|
| 1 | Store, web app and visual language, projects, gates, evidence, tasks, holidays, Today, date rules | Done |
| 2 | Reading by program: git mirrors, GitHub reader, AI-DLC parsers, WBS reader, snapshots, schedule | Not started |
| 3 | Reading by model: prose reader with quote check, health proposals, digest, story of the day | Not started |
| 4 | People view, gate report export, retention, backup | Not started |

## Rules for this repository

- **No data of a tracked project, ever.** No mirrors, no database, no digests, no logs, no real repository names in tests. Tests use invented projects only.
- **No secrets.** Tokens live in the data directory under `secrets/`, never here.
- The data directory (default `~/.local/share/pm-dash/`) is outside this repository. `pm-dash doctor` and `pm-dash web` refuse to start when it is inside a git work tree.

## Run it locally

Needs Python 3.14 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pm-dash doctor      # checks the installation
uv run pm-dash web         # serves http://127.0.0.1:8787
uv run pytest              # tests
```

Installation settings are read from `$PMD_CONFIG` or `~/.config/pm-dash/config.toml` (all optional):

```toml
data_dir = "~/.local/share/pm-dash"
port = 8787
tailnet_name = "laptop.tailXXXX.ts.net"   # the host name Tailscale serves the dashboard under
allowed_login = "you@example.com"         # refused when Tailscale reports another login
```

The server listens on the loopback address only. It is meant to be published inside a tailnet with `tailscale serve`, never on a public address. There is no login. Instead, the request guard accepts only known host names, refuses a different Tailscale login, and accepts changes only from PMD's own pages.

## Layout

```
pmd/
├── cli.py          pm-dash web | doctor
├── config.py       installation settings
├── data/
│   ├── db.py       connection, transactions, migrations, change-log triggers
│   ├── hand.py     every write of hand data (the run must never import it)
│   └── migrations/ numbered SQL files
├── rules/          pure rules: working days, next gate, late tasks, need-your-call count
└── web/
    ├── app.py      routes and actions
    ├── guard.py    host, Tailscale identity, own-origin check, security headers
    ├── present.py  rows → what the templates show
    ├── templates/
    └── static/     pmd.css, pmd.js, theme.js, fonts/ (Bricolage Grotesque, SIL OFL 1.1)
tests/              invented projects only
```

## Licenses

All runtime dependencies are permissive: FastAPI (MIT), Starlette (BSD-3), Uvicorn (BSD-3), Jinja2 (BSD-3), python-multipart (Apache-2.0), Pydantic (MIT). The display font Bricolage Grotesque is under the SIL Open Font License 1.1; its license text is in `pmd/web/static/fonts/OFL.txt`.
