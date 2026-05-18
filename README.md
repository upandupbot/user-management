# Pipefy Assignee Swap CLI

A small Python CLI that finds every card in a specified Pipefy pipe (under the **Up and Up** organization) currently assigned to a given user and **swaps** that user for another user — at both the card level and inside any user/assignee fields — **without disturbing any other assignees**, while skipping cards in excluded phases (default: `Cancelled`, `Completed`, `Finance Recon`).

## Install

```powershell
python -m venv .venv_up_up_ui
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configure

Fill in your Pipefy API token into config.ini:

```ini
[Pipefy]
source = up-and-up
pipefy_api_url = https://api.pipefy.com/graphql
pipefy_api_token = <your token>

[PaperTrail]
papertrail_log_token = <optional>
papertrail_host = logs.papertrailapp.com
papertrail_port = <optional>
```


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
- `--yes` skips the confirmation prompt that otherwise fires when more than `--confirm-threshold` (default 5) cards would change.
- `--throttle-ms` controls the sleep between mutations (default 250 ms).
- `--report PATH` writes a CSV log of every planned/executed action.

## Tests

```powershell
pytest -q
```

All tests are offline; the Pipefy client is mocked.
