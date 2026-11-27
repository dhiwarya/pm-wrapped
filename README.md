# pm-dash

Code of the Project Manager Dashboard (PMD): a one-user web dashboard that answers "will this project pass its next gate?". The design lives in a separate, local design package.

## Status

| Phase | Content | State |
|---|---|---|
| 1 | Store, web app and visual language, projects, gates, evidence, tasks, holidays, Today, date rules | Done |
| 2 | Reading by program: git mirrors, read-only GitHub client, AI-DLC, dashboard and WBS adapters, blockers, evidence states, snapshots, `pm-dash run`, Timeline, Units, Runs, stale banner | Done |
| 2b | Onboarding: add a project from a GitHub URL or a git repository on this machine; the agent proposes the setup, you accept each item | Done |
| 3 | Verified prose facts, gate health proposals and confirmations, Changes, weekday digest and story, Codex fallback | Done |
| 4 | People view, Markdown gate export, weekly snapshot retention, nightly SQLite backup and restore | Done |

Not deployed yet: `deploy/` holds the systemd units for the host.

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
uv run pm-dash run         # read all active projects now and publish a snapshot
uv run pm-dash onboard --project KEY   # propose the setup of a project from its sources
uv run pm-dash backup                  # write an online SQLite copy under the data directory
uv run pytest              # tests
```

Installation settings are read from `$PMD_CONFIG` or `~/.config/pm-dash/config.toml` (all optional):

```toml
data_dir = "~/.local/share/pm-dash"
port = 8787
tailnet_name = "laptop.tailXXXX.ts.net"   # the host name Tailscale serves the dashboard under
allowed_login = "you@example.com"         # refused when Tailscale reports another login
codex_fallback = true                     # use Codex when Claude reports a usage limit
# claude_model = "claude-opus-5-5"        # optional model pin
# claude_effort = "high"
# codex_model = "gpt-6.2-sol"              # optional model pin
# codex_effort = "high"
```

The server listens on the loopback address only. It is meant to be published inside a tailnet with `tailscale serve`, never on a public address. There is no login. Instead, the request guard accepts only known host names, refuses a different Tailscale login, and accepts changes only from PMD's own pages.

Secrets, both optional, in `<data_dir>/secrets/` with mode 600:

- `github-token`: a read-only token scoped to the tracked repositories. For full private-repository CI coverage, use a GitHub App token with read access to Contents, Pull requests, Issues, Commit statuses and Checks. A fine-grained personal access token can read PRs and reviews, but GitHub limits access to check runs. PM Dash lists the affected PRs with their available review data and records the missing CI access in Runs. [GitHub Checks permissions](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference)
- `claude-token`: a Claude Code token from `claude setup-token`. Without it, the CLI uses the login it already has.

Enter the project team (one name per line) before adding and reading repositories. On an existing project, review the team on the Sources tab before starting onboarding again. The team is entered by the user.

The onboarding agent calls the Claude Code CLI with read tools only (`--restricted --tools Read,Glob,Grep`), inside a copy of the repository without agent instruction files, with a money limit (Settings, default $2 per source).

## Model fallback and weekday digest

Claude Code is the primary model for onboarding, prose reading and gate health. If it reports a usage, rate or budget limit, PM Dash tries `codex exec` using the remaining time for that call. Install Codex and sign in as the account that runs PM Dash. Codex receives bounded excerpts with shell, web search, apps and hooks disabled. Both providers use the same quote and citation checks. Runs shows which provider handled each call. If neither provider can read the prose or produce a valid judgment, the run reports the problem and publishes any usable facts and rules-only proposals.

Projects with **Prose reading** on can send project excerpts to Anthropic and, when fallback runs, to OpenAI. Set `codex_fallback = false` in the installation config to use Claude alone. Turn off **Prose reading** on a project to keep its processing local. PM Dash records Claude's reported spend. Codex usage is managed by the account the CLI uses.

**Run now** refreshes Today and Changes. To generate a digest and story locally on a weekday, use `uv run pm-dash run --trigger schedule`. Each scheduled day gets one digest, stored in Changes and in `<data_dir>/digests/`. The dashboard keeps a history of previous digests. The digest stays on the host by default.

## People, reports, and upkeep

- **Pull requests** lists open PRs from enabled GitHub sources after each run. Today highlights PRs that need attention or look ready to merge. The weekday digest names each actionable PR with its reason and GitHub URL. Ready to merge requires an approval on the latest commit, passing CI and a clean GitHub merge status. Review the diff on GitHub before merging.
- **Timeline** uses the mapped WBS.csv in any folder of a linked repository (filename matched case-insensitively). When it is missing, onboarding continues and the timeline is skipped.
- **People** remembers your choice of grid or rows in this browser. Expand a person and then a PR to see its review and CI details. The page shows pending reviews, open PRs authored, and active tasks by person. Attach unmatched repository names to a person from the same page.
- **Gates → Download Markdown report** exports confirmed health or the outcome, evidence, linked facts, open blockers, and the snapshot's commits. Add `?include_proposal=true` to the report URL to include the current pending judgment in its own section.
- **Settings → Retention** defaults to 90 days of full snapshots and 14 days of run and onboarding work files. Older history keeps the last snapshot in each week. Digests and hand records, including confirmations and audit history, are retained. Removing a source deletes its PM Dash mirror. Deleting a project removes the daily digests that included it, including shared digests. Earlier backups preserve their original contents.
- **Backups** use SQLite's online backup and keep the last 14 copies in `<data_dir>/backup/`. The database contains dashboard data and digests. Mirrors, work files, and credentials live outside those copies.

To verify or recover a backup, make a separate config file whose `data_dir` points to an empty directory outside Git. Then run `uv run pm-dash --config /path/to/restore.toml restore /path/to/backup.sqlite3`. Start the dashboard with the recovery config to inspect the restored copy. In-progress runs in the backup are marked failed so they can be retried.

## Layout

```
pmd/
├── cli.py          pm-dash web | run | onboard | doctor
├── adapters/       aidlc, dashboard file, WBS, GitHub: files and saved answers to rows
├── sources/        git mirrors (fetch only) and the GitHub client (GET only)
├── run/            run controller, assessments, changes and digest
├── onboard/        the onboarding agent: finds by program, model reading, checks
├── model/          Claude and Codex routing, prose extraction and gate judgment
├── config.py       installation settings
├── data/
│   ├── db.py       connection, transactions, migrations, change-log triggers
│   ├── hand.py     every write of hand data (the run must never import it)
│   ├── run.py      run-table writes for the run and onboarding processes
│   ├── views.py    run-table reads for the web
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

All runtime dependencies are permissive: FastAPI (MIT), Starlette (BSD-3), Uvicorn (BSD-3), Jinja2 (BSD-3), python-multipart (Apache-2.0), Pydantic (MIT), httpx (BSD-3). The display font Bricolage Grotesque is under the SIL Open Font License 1.1; its license text is in `pmd/web/static/fonts/OFL.txt`.
