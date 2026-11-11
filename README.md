# Pipefy Assignee Swap CLI

A small Python CLI that finds every card in a specified Pipefy pipe (under the **Up and Up** organization) currently assigned to a given user and **swaps** that user for another user — at both the card level and inside any user/assignee fields — **without disturbing any other assignees**, while skipping cards in excluded phases (default: `Cancelled`, `Completed`, `Finance Recon`).

## Install

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configure

Copy `config.example.ini` to `config.ini` and fill in your Pipefy API token:

```ini
[Pipefy]
source = up-and-up
pipefy_api_url = https://api.pipefy.com/graphql
pipefy_api_token = <your token>

[PaperTrail]
papertrail_log_token = <optional>
papertrail_host = logs.papertrailapp.com
papertrail_port = <optional>

[Audit]
# Optional. Mirror applied swaps into a shared Google Sheet. Pick ONE method:
# A) Service account (works even with locked-down Workspaces) — recommended:
sheet_id = <spreadsheet id>
service_account_file = C:\path\to\service-account-key.json
# B) Apps Script web app (only if your Workspace allows anonymous web apps):
# webhook_url = <Apps Script /exec URL>
```

You can also override the token with the `PIPEFY_API_TOKEN` environment variable. Tokens are never logged in full — only `****` + last 4 chars at DEBUG level.

> `config.ini` is gitignored because it holds secrets (the Pipefy API token and your service-account key path). Only the secret-free `config.example.ini` template is committed — never commit your real `config.ini` or any service-account `.json` key.

> The endpoint is `https://api.pipefy.com/graphql`. The CLI rewrites the legacy `app.pipefy.com/graphql` URL automatically.

## Usage

Dry-run preview (will interactively prompt for which assignee field(s) to swap):

```powershell
python -m swap_assignee.cli `
  --pipe-id 123456 `
  --old-user kate.smith@upandup.com `
  --new-user robyn.seth@upandup.com
```

Non-interactive: explicit field, apply, skip confirmation, write report:

```powershell
python -m swap_assignee.cli `
  --pipe-id 123456 `
  --old-user kate.smith@upandup.com `
  --new-user robyn.seth@upandup.com `
  --field creative_specialist `
  --apply --yes `
  --report swap_2026-05-06.csv
```

Operate on every assignee field on the pipe (skip card-level):

```powershell
python -m swap_assignee.cli `
  --pipe-id 123456 `
  --old-user kate.smith@upandup.com `
  --new-user robyn.seth@upandup.com `
  --all-fields --no-include-card-level `
  --apply --yes
```

### Flags

See `python -m swap_assignee.cli --help`. Highlights:

- `--exclude-phase` (repeatable). Defaults to `Cancelled`, `Completed`, `Finance Recon`. If you pass any value, the default list is fully replaced.
- `--field` (repeatable) restricts to specific assignee field IDs. `--all-fields` opts in to every assignee field on the pipe.
- `--apply` is required to perform real mutations. Without it, the CLI is a dry-run.
- `--operator-email` is **required with `--apply`** — it's stamped on every audit record so the team knows who ran the swap. Must be on an approved Up&Up domain (falls back to the `SWAP_OPERATOR_EMAIL` env var).
- `--yes` skips the confirmation prompt that otherwise fires when more than `--confirm-threshold` (default 5) cards would change.
- `--throttle-ms` controls the sleep between mutations (default 250 ms).
- `--report PATH` writes a CSV log of every planned/executed action.
- `--card-add-only` only **adds** the new user at card-level; the old user is left in place. Pipefy's `updateCard` mutation re-pings every assignee whenever the list changes, so swapping spams everyone. Appending instead means only the new user gets notified — remove the old user manually in Pipefy's admin panel afterwards. Field-level swaps are unaffected.

## Web UI

```powershell
python -m swap_assignee.web
```

Then open http://127.0.0.1:5000/user-management/. The UI mirrors the CLI with a few quality-of-life extras. The app's browser and API endpoints are mounted under `/user-management` for reverse-proxy hosting.

- **Sign in with your company email.** On first load an email gate asks for an approved Up&Up-group email (used for the audit trail). It's remembered in your browser and re-checked server-side on every apply.
- **Pick a pipe** from the horizontal chip selector (pipe names only; the pipe id shows as a tooltip), or choose **Other** to type an id manually.
- **Card-level assignees are unticked by default.** Touching the top-of-card assignee list makes Pipefy notify _every_ assignee on the card — there's no API way around it — so the safe default is a surgical field-only swap. Tick **Card-level assignees** only when you deliberately want to change the top-of-card avatars.
- **Test run first.** The dry run streams matched cards into a table as they're found, with running totals and a phase-skip strip.
- **Choose which cards to change.** Every preview row has a checkbox (all ticked by default) plus a select-all toggle in the header. **Apply** only changes the ticked cards. Un-ticked cards stay in the table as `planned` and selectable, so you can apply in batches — tick more and **Apply** again without re-running the dry run.
- **History panel.** Expand **History** to see the last 30 days of real applies (operator, card, before → after, status), pulled from the local audit store.
- **Filterable history.** Filter applies by board, client or swapped user, over the last 30 days, 3/6/12 months, all time or custom dates. When the shared Google Sheet is configured, History (and notifications) are read back from it, so they survive redeploys and free-tier restarts. Without it, history comes from the local audit store, which defaults to 365 days (`SWAP_AUDIT_RETENTION_DAYS`, minimum 30) and is wiped whenever a hosted instance restarts.
- **Developer settings.** The advanced settings and live activity log are visible only to signed-in `@dydx.digital` users. Other approved-domain users can use the normal scan, preview, apply, and history workflows.
- **Notifications bell.** The header bell shows a count and opens a short list of assignee changes from the last 30 days, written plainly ("Simphiwe removed Magda and assigned Simphiwe on Levergy work for Acme."). `@dydx.digital` users see every change plus developer warnings and errors; other agencies only see changes made by, or involving, people on their own email domain.

> Note: per-card selection is a web-UI feature. The CLI applies every matched card in one pass (subject to `--apply`/`--yes`).

## Hosting (Render / Fly.io)

The web UI can be hosted on a free-tier platform — no domain needed, you get a
`*.onrender.com` / `*.fly.dev` subdomain. Before exposing it publicly you
**must** set `SWAP_WEB_PASSWORD`: with it set, every route requires signing in
with an approved company email **and** the shared password (signed session
cookie, 14-day lifetime, HTTPS-only).

All secrets come from env vars — never commit `config.ini` or key files:

| Env var                           | Required     | Purpose                                                                                      |
| --------------------------------- | ------------ | -------------------------------------------------------------------------------------------- |
| `PIPEFY_API_TOKEN`                | yes          | Pipefy API token                                                                             |
| `SWAP_WEB_PASSWORD`               | yes (hosted) | Shared sign-in password for the UI                                                           |
| `SWAP_WEB_SECRET_KEY`             | no           | Session signing key (auto-derived from the password if omitted)                              |
| `PAPERTRAIL_LOG_TOKEN`            | no           | Remote logging                                                                               |
| `SWAP_AUDIT_SHEET_ID`             | no           | Google Sheets audit mirror                                                                   |
| `SWAP_AUDIT_SERVICE_ACCOUNT_JSON` | no           | Paste the whole service-account key-file JSON (hosted alternative to `service_account_file`) |

**Render** — push the repo to GitHub, then _New → Blueprint_; [render.yaml](render.yaml)
defines the service. Set the secrets in the dashboard. Free plan sleeps after
~15 min idle (first request after that takes ~30–60 s).

**Fly.io** — uses the [Dockerfile](Dockerfile) + [fly.toml](fly.toml):

```bash
fly launch --no-deploy
fly secrets set PIPEFY_API_TOKEN=... SWAP_WEB_PASSWORD=...
fly deploy
```

Notes:

- The server runs `gunicorn --workers 1 --threads 8` — keep **one worker**;
  preview plans and the Developer-panel log ring live in process memory.
- Free-tier disks are ephemeral, so the local JSONL audit history resets on
  redeploy/restart. Configure the Google Sheets sink if you need a durable
  audit trail when hosted.
- To test password mode locally over plain HTTP, also set
  `SWAP_WEB_ALLOW_INSECURE_COOKIE=1` (never set this in production).

## Audit trail

Every **real apply** (not dry runs) is recorded so you can see who changed what and when. This works in two tiers:

**Tier 1 — local store (always on).**
Each applied card writes one row per change (card-level + each field) to a rolling JSONL log on the machine that ran the swap:

- Default path: `~/.swap_assignee/history.jsonl` (override with the `SWAP_AUDIT_STORE` env var).
- **30-day retention** — records older than 30 days are pruned automatically on the next write.
- In the web UI, expand the **History** panel to view the last 30 days of applies.

**Tier 2 — shared Google Sheet (optional).**
Each apply is _also_ mirrored into a shared Google Sheet — a central, company-wide log on top of each operator's local copy. Mirroring is best-effort: if the Sheet is unreachable, the Pipefy change still succeeds and the record is kept locally. There are two ways to connect the Sheet; pick **one**:

**Option A — Service account (recommended, works with locked-down Workspaces).**
The tool writes straight to the Sheet via the Google Sheets API. This avoids public web apps entirely, so it works even when your Google Workspace blocks anonymous Apps Script access (the "You need access" page).

1. Install the optional deps: `pip install -r requirements.txt`.
2. Follow the step-by-step setup at the top of [swap_assignee/sheets_sink.py](swap_assignee/sheets_sink.py) (create a service account + JSON key, enable the Sheets API, **share the Sheet with the service account's `client_email` as Editor**).
3. In `config.ini`:
   ```ini
   [Audit]
   sheet_id = 1f74P1uusrYh3pNzYnc1kQqyBCptVc8CMKHizRztwH9M
   service_account_file = C:\path\to\service-account-key.json
   ```
   (Or set `SWAP_AUDIT_SHEET_ID` / `SWAP_AUDIT_SERVICE_ACCOUNT_FILE` env vars.) The tool auto-creates an `Audit` tab + header row on first write.

> Keep the service-account JSON key out of git — it grants write access to the Sheet. The `.gitignore` already excludes `*.json` (except `fixtures/`), `up-up-swap-users-*.json`, and `service-account*.json`. If a key is ever exposed, rotate it in the Google Cloud console.

**Option B — Apps Script web app (only if your Workspace allows anonymous web apps).**
If `[Audit] webhook_url` (or `SWAP_AUDIT_WEBHOOK_URL`) is set, each apply is POSTed to a Google Apps Script Web App that appends rows. Follow the instructions at the top of [google_apps_script/audit_sink.gs](google_apps_script/audit_sink.gs):

1. Open your audit Sheet → **Extensions → Apps Script** and paste that file.
2. **Deploy → New deployment → Web app** (Execute as: _Me_, Who has access: _Anyone_).
3. Copy the Web app URL into `config.ini` under `[Audit] webhook_url`.

> If the deployed URL returns **403 / "You need access"**, your Workspace blocks anonymous web apps — use **Option A** instead.

### Operator identity

Because the audit answers "who did this", an approved company email is required before any apply:

- **Web UI:** an email gate blocks the app on load until you enter an approved Up&Up-group email (remembered in your browser via `localStorage`). The server re-validates it on every apply — the gate is not just cosmetic.
- **CLI:** pass `--operator-email you@upandupgroup.com` (or set `SWAP_OPERATOR_EMAIL`). Applies are refused without a valid one.

Approved domains: `upandupgroup.com`, `dydx.digital`, `mcsaatchiabel.co.za`, `upandupconnect.com`, `inblackandwhite.co.za`, `levergy.co.za`, `razorpr.co.za`.

## Tests

```powershell
pytest -q
```

All tests are offline; the Pipefy client is mocked.
