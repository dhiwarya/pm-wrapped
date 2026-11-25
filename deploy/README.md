# Deployment files

Not installed yet. They are for a Linux host with systemd (open point OP-02).

User services, installed with `systemctl --user`:

| File | Content |
|---|---|
| `systemd/pm-dash-web.service` | `pm-dash web` on 127.0.0.1, restarted on failure |
| `systemd/pm-dash-run.service` | One scheduled run |
| `systemd/pm-dash-run.timer` | Weekdays at 07:00, with a 09:30 catch-up |

Before enabling: `pm-dash doctor` must pass, the tokens must be in `<data_dir>/secrets/` with mode 600, and
`loginctl enable-linger` keeps user services running without a login session.
