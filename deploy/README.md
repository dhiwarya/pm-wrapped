# Deployment files

Not installed yet. They are for a Linux host with systemd (open point OP-02).

User services, installed with `systemctl --user`:

| File | Content |
|---|---|
| `systemd/pm-dash-web.service` | `pm-dash web` on 127.0.0.1, restarted on failure |
| `systemd/pm-dash-run.service` | One scheduled run |
| `systemd/pm-dash-run.timer` | Weekdays at 07:00, with a 09:30 catch-up |
| `systemd/pm-dash-backup.service` | Create and verify an online SQLite backup |
| `systemd/pm-dash-backup.timer` | Nightly at 02:00 in the host's time zone, with catch-up after downtime |

Before enabling: `pm-dash doctor` must pass, the tokens must be in `<data_dir>/secrets/` with mode 600, and
`loginctl enable-linger` keeps user services running without a login session.

Enable `pm-dash-web.service`, `pm-dash-run.timer`, and `pm-dash-backup.timer`. The backup timer keeps the last 14 copies in `<data_dir>/backup/`.
