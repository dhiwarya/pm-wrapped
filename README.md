# pm-dash

Code of the Project Manager Dashboard (PMD). The design is in the parent folder: start at `../00_START_HERE.md`.

## Rules for this repository

- **No data of a tracked project, ever.** No mirrors, no database, no digests, no logs, no real repository names in tests. Tests use invented projects only (Planning NFR-05, §10 L-1 and L-5).
- **No secrets.** The GitHub token and the Claude Code token live in the data directory under `secrets/`, never here (Detailed Design Part 3 §11-2).
- The data directory (default `~/.local/share/pm-dash/`) is outside this repository. `pm-dash doctor` refuses to start when it is inside a git work tree (Part 1 §5-1).

## Planned layout

From Detailed Design Part 1 §4:

```
pmd/
├── web/          pages, forms, the request guard
│   ├── templates/
│   └── static/   pmd.css, pmd.js, fonts/
├── data/         the only code that touches the database (hand.py, run.py)
├── run/          the run controller and its stages
├── adapters/     git, github, aidlc, aidlc_dashboard, wbs
├── model/        reader, judge, CLI wrapper, quote check
├── rules/        health floors, blockers, item states, disagreements, working days
├── digest/       diff, digest text, story cards, senders
└── cli.py        pm-dash web | run | doctor | backup
tests/
└── fixtures/     invented projects: fx-aidlc, fx-wbs, fx-github, fx-manual, fx-inject
deploy/           systemd units and the timer
```

## Build order

Planning §13: 1 manual tracker, 2 reading by program, 3 reading by model, 4 finish.
