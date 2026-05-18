# Agent Brief — Pipefy Assignee Swap CLI

> Drop this file to an AI coding agent. It is self-contained: goal, constraints, API contracts, CLI shape, file layout, edge cases, and acceptance tests.

---

## 1. Goal (one sentence)

Build a Python CLI that finds every card in a specified Pipefy pipe (under the **Up and Up** organization) currently assigned to a given user and **swaps** that user for another user — at both the card level and inside any user/assignee fields — **without disturbing any other assignees**, and **skipping** cards whose current phase is in a user-supplied exclusion list (default: `Cancelled`, `Completed`, `Finance Recon`).

---

## 2. Hard requirements (do not violate)

1. **Swap, don't replace.** Other assignees on the card / in the field MUST remain. Only the matched old user is removed and the new user added in their place.
2. **Idempotent.** If the new user is already on the card and the old user is already gone, do nothing for that card. If the new user is already there alongside the old one, just remove the old one.
3. **No duplicate assignment.** If the new user is already present on a card alongside the old user, remove the old user and do NOT add the new user a second time.
4. **Phase exclusion is case-insensitive and trimmed.** `"finance recon"`, `"Finance Recon"`, `" FINANCE RECON "` all match.
5. **Dry-run by default.** Real mutations require explicit `--apply`. Without it, the tool only prints what it *would* do.
6. **Confirm before bulk apply.** When `--apply` is set and >N cards (default 5) would be modified, prompt `"Modify {N} cards? [y/N]"` unless `--yes` is passed.
7. **Resolve users by email primarily, name as fallback.** Names are ambiguous; emails are not.
8. **Never invent user IDs.** If old or new user cannot be resolved, fail fast with a clear error before touching any card.
9. **Respect Pipefy rate limits.** Backoff with retry on 429 / "rate limit" / "throttle" errors. Sleep between mutations (configurable, default 250 ms).
10. **Log every action** to stdout and to PaperTrail (if `papertrail_log_token` is configured).

---

## 3. Credentials & config

Read from a config file (`config.ini` by default, overridable with `--config PATH`):

```ini
[Pipefy]
source = up-and-up
pipefy_api_url = https://api.pipefy.com/graphql
pipefy_api_token = <token>

[PaperTrail]
papertrail_log_token = <token>
papertrail_host = logs.papertrailapp.com
papertrail_port = <port>            ; optional
```

> **NOTE:** The user-supplied URL `https://app.pipefy.com/graphql` is wrong. The correct GraphQL endpoint is `https://api.pipefy.com/graphql`. The agent should default to the correct URL but accept the config-provided one if given.

Also support env-var overrides:
- `PIPEFY_API_TOKEN`
- `PAPERTRAIL_LOG_TOKEN`

Never log the token. Mask it in any debug output (`****` + last 4 chars).

---

## 4. CLI shape

```
swap-assignee \
  --pipe-id <PIPE_ID> \
  --old-user <EMAIL_OR_NAME> \
  --new-user <EMAIL_OR_NAME> \
  [--exclude-phase "Cancelled" --exclude-phase "Completed" --exclude-phase "Finance Recon"] \
  [--field <FIELD_ID>]                 # restrict to a specific user/assignee field; repeatable.
                                       #   If omitted, the CLI INTERACTIVELY prompts the user
                                       #   to pick from the list of user/assignee fields it
                                       #   discovers on the pipe (see §4.1).
  [--all-fields]                       # skip the prompt and operate on every user/assignee field.
  [--include-card-level / --no-include-card-level]   # default: include
  [--apply]                            # actually mutate; default is dry-run
  [--yes]                              # skip confirmation prompt
  [--config PATH]                      # default: ./config.ini
  [--limit N]                          # cap cards processed (for testing)
  [--throttle-ms 250]
  [--log-level INFO|DEBUG]
  [--report PATH]                      # write CSV report of actions taken
```

### Default exclusion phases
If `--exclude-phase` is **not** passed at all, default to: `Cancelled`, `Completed`, `Finance Recon`.
If passed even once, the user list **replaces** the default (explicit > implicit).

### 4.1 Interactive field selection

Fields are **never hardcoded**. "Creative Specialist" was an illustrative example only. The CLI must discover assignee-bearing fields at runtime by inspecting the pipe schema, then:

1. **If `--field` is provided** (one or more times): use exactly those field IDs. If any provided ID is not an assignee-type field on the pipe, error out before touching cards.
2. **If `--all-fields` is provided**: operate on every user/assignee field discovered on the pipe (plus card-level unless `--no-include-card-level`).
3. **If neither flag is provided** AND stdin is a TTY: print a numbered menu of discovered fields and prompt the user to choose one or more. Example:

   ```
   Found 4 user/assignee fields on pipe "Up and Up — Creative Production":
     [0] (card-level assignees)
     [1] creative_specialist        — "Creative Specialist"        (assignee_select)
     [2] account_manager            — "Account Manager"            (assignee_select)
     [3] qa_reviewer                — "QA Reviewer"                (assignee_select)
     [4] traffic_coordinator        — "Traffic Coordinator"        (user)

   Which field(s) should the swap apply to?
     - Enter numbers separated by commas (e.g. "1,3")
     - Enter "all" to swap every field above
     - Enter "q" to abort
   > _
   ```

   Validate input. Re-prompt on bad input. `Ctrl-C` aborts cleanly.
4. **If neither flag is provided** AND stdin is **not** a TTY (piped / CI / cron): error out with `Field selection required: pass --field <ID> (repeatable) or --all-fields. Discovered fields: ...`. Never silently fall back to a default — explicit > implicit.

The discovery query is the same `pipe(id:) { start_form_fields { ... } phases { fields { ... } }` schema walk used everywhere else; assignee fields are those whose `type` is in `{"assignee_select", "user", "member"}`.

### Examples

```bash
# Dry-run preview — will INTERACTIVELY prompt for which field(s) to swap
swap-assignee --pipe-id 123456 \
  --old-user kate.smith@upandup.com \
  --new-user robyn.seth@upandup.com

# Non-interactive: explicit field, apply, skip confirmation
swap-assignee --pipe-id 123456 \
  --old-user kate.smith@upandup.com \
  --new-user robyn.seth@upandup.com \
  --field creative_specialist \
  --apply --yes \
  --report swap_2026-05-06.csv

# Operate on every assignee field on the pipe (still skips card-level if you want):
swap-assignee --pipe-id 123456 \
  --old-user kate.smith@upandup.com \
  --new-user robyn.seth@upandup.com \
  --all-fields --no-include-card-level \
  --apply --yes
```

---

## 5. Pipefy GraphQL contracts

### 5.1 Resolve users (org members of "Up and Up")

```graphql
query OrgUsers($orgId: ID!) {
  organization(id: $orgId) {
    users { id name email }
  }
}
```

The agent must accept either a numeric org ID via `--org-id` / `[Pipefy] org_id`, OR auto-detect the org from the pipe:

```graphql
query PipeOrg($pipeId: ID!) {
  pipe(id: $pipeId) {
    id name
    organization { id name }
    members { user { id name email } }
  }
}
```

Resolution order:
1. Exact email match (lowercased, trimmed) against `pipe.members[].user.email`
2. Exact email match against `organization.users[].email`
3. Exact normalized-name match against pipe members (only if unique)
4. Exact normalized-name match against org users (only if unique)
5. **Fail** with: `Could not resolve user '{input}'. Provide an email instead of a name.`

### 5.2 Page through pipe cards

Pipefy paginates via `cards(first: N, after: CURSOR)` on the pipe. Loop until `pageInfo.hasNextPage` is false.

```graphql
query PipeCards($pipeId: ID!, $cursor: String) {
  cards(pipe_id: $pipeId, first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        url
        current_phase { id name }
        assignees { id name email }
        fields {
          name
          value
          array_value
          field { id label type }
        }
      }
    }
  }
}
```

> Some Pipefy schemas use `pipe(id: $pipeId) { cards_count phases { cards(first: ...) ... } }`. If `cards(pipe_id:...)` rejects, fall back to iterating `pipe.phases[].cards`. The agent must handle both.

### 5.3 Mutations

**Card-level swap (replaces full assignee list, so we compute the new list first):**

```graphql
mutation SetAssignees($cardId: ID!, $assigneeIds: [ID!]!) {
  updateCard(input: { id: $cardId, assignee_ids: $assigneeIds }) {
    card { id assignees { id email } }
  }
}
```

**Field-level swap** (assignee_select / user / member field types):

```graphql
mutation UpdateField($cardId: ID!, $fieldId: ID!, $value: [UndefinedInput]) {
  updateCardField(input: { card_id: $cardId, field_id: $fieldId, new_value: $value }) {
    card { id }
    success
  }
}
```

`new_value` for an assignee field must be a JSON array of user IDs as strings, e.g. `["301017419", "307242749"]`.

---

## 6. Algorithm (per card)

```
For each card in pipe (paginated):
    if card.current_phase.name (normalized) ∈ excluded_phases:
        log SKIP (excluded phase); continue

    actions_for_this_card = []

    # --- card-level ---
    if include_card_level:
        ids = [a.id for a in card.assignees]
        if old_user.id in ids:
            new_ids = [aid for aid in ids if aid != old_user.id]
            if new_user.id not in new_ids:
                new_ids.append(new_user.id)
            actions_for_this_card.append(("card_level", ids, new_ids))

    # --- field-level ---
    for field in card.fields:
        if field.type not in {"assignee_select", "user", "member"}:
            continue
        if --field filter set and field.id not in filter_set:
            continue
        ids = parse_user_ids(field)        # may come from .value JSON or .array_value
        if old_user.id in ids:
            new_ids = [aid for aid in ids if aid != old_user.id]
            if new_user.id not in new_ids:
                new_ids.append(new_user.id)
            actions_for_this_card.append(("field", field.id, ids, new_ids))

    if not actions_for_this_card:
        continue   # nothing to do for this card

    log PLAN for card (title, url, actions)

    if --apply:
        for action in actions_for_this_card:
            execute mutation
            sleep(throttle_ms)
```

### Parsing field user IDs

Pipefy returns assignee fields inconsistently. Handle all of:

- `field.array_value` is a list of user dicts: `[{"id": "123", "name": "..."}]`
- `field.value` is a JSON string of an array of IDs: `'["123", "456"]'`
- `field.value` is a JSON string of an array of user objects
- `field.value` is a single ID string

Always coerce to `list[str]` of digit IDs.

---

## 7. File layout

```
swap_assignee/
  __init__.py
  cli.py                 # argparse / click entry point
  config.py              # load config.ini + env overrides; mask secrets
  pipefy_client.py       # thin GraphQL wrapper with retry/backoff
  user_resolver.py       # email/name → user_id via org + pipe membership
  card_iterator.py       # paginate pipe cards
  swap_engine.py         # plan + execute the swap algorithm
  papertrail_logger.py   # syslog handler attached to root logger if token set
  reports.py             # CSV writer for --report
tests/
  test_user_resolver.py
  test_swap_engine.py    # uses fixtures, no network
  test_field_parsing.py
  test_cli_dry_run.py
fixtures/
  card_with_old_user_only.json
  card_with_both_users.json
  card_with_field_assignee.json
  card_in_excluded_phase.json
config.example.ini
README.md
requirements.txt        # requests, python-dotenv (optional), click or argparse
```

---

## 8. Logging

- Root logger at `INFO` (configurable).
- Format: `%(asctime)s %(levelname)s %(name)s %(message)s`
- One log line per: card considered, card skipped (with reason), planned action, executed action, error.
- If `[PaperTrail]` token + host + port set, attach a `SysLogHandler`. Otherwise PaperTrail is silently disabled.
- **Never** log the bearer token or the full Authorization header.

---

## 9. Output

### Stdout summary at end

```
Pipe:           Up and Up — Creative Production (id 123456)
Old user:       Kate Smith <kate.smith@upandup.com> (id 300111)
New user:       Robyn Seth <robyn.seth@upandup.com> (id 300222)
Excluded:       Cancelled, Completed, Finance Recon
Mode:           DRY-RUN  (re-run with --apply to mutate)

Cards scanned:        1,284
Cards in excluded:      312
Cards already swapped:   47
Cards to swap:          108
   ├─ card-level:        93
   └─ field-level:       71  (across 2 fields)

Cards changed:           0   (dry-run)
Errors:                  0
```

In `--apply` mode, `Cards changed` reflects actual mutations and `Errors` lists each failure with card URL.

### CSV report (`--report`)

Columns: `card_id, card_url, title, current_phase, scope (card|field:<id>), action (would_swap|swapped|skipped|error), old_assignees, new_assignees, error_message`

---

## 10. Edge cases (must handle)

| # | Case | Expected behavior |
|---|------|-------------------|
| 1 | Old user is the only assignee on a card | Swap normally — new user replaces them. Card never has zero assignees. |
| 2 | New user already present alongside old user | Just remove old user. Don't add new user twice. |
| 3 | Old user not on the card at all | Skip silently (don't even log at INFO; DEBUG only). |
| 4 | Field type unknown | Ignore that field; never guess. |
| 5 | Field has user IDs as integers, not strings | Coerce to string before comparison. |
| 6 | Pipefy returns 429 or "rate limit" message | Exponential backoff: 2s, 4s, 8s; max 3 retries; then error this card and continue. |
| 7 | Network error mid-batch | Card-scoped failure logged in CSV; processing continues. Final exit code = 1 if any errors. |
| 8 | Pipe has no cards | Print summary with zeros; exit 0. |
| 9 | `--limit N` set | Stop after N cards *considered* (not after N swapped). |
| 10 | Same email maps to two different user IDs (rare) | Fail before any mutation; print both candidates. |
| 11 | Old user equals new user | Refuse with error before fetching cards. |
| 12 | `--field FOO` given but no such field exists in pipe | **Fail fast** before processing any card; do not silently ignore. |
| 13 | Card-level only requested but card has no card-level assignees | Skip card. |
| 14 | Neither `--field` nor `--all-fields` given, non-TTY stdin | Fail with message listing discovered field IDs; do not prompt, do not default. |
| 15 | Pipe has zero user/assignee fields | If user passes `--all-fields` or selects "all" in the prompt, fall back to card-level only (warn). If `--no-include-card-level` is also set, exit with error — nothing to do. |

---

## 11. Acceptance tests (the agent must pass these before declaring done)

Use the JSON fixtures under `fixtures/`. No network in unit tests.

1. **`test_swap_when_only_old_user_card_level`** — card has `[old]`, swap produces `[new]`.
2. **`test_swap_keeps_other_assignees`** — card has `[old, X, Y]`, swap produces `[X, Y, new]` (order-insensitive).
3. **`test_idempotent_when_only_new_user`** — card has `[new]`, planner returns no actions.
4. **`test_dedup_when_both_present`** — card has `[old, new, X]`, planner produces `[new, X]` (no duplicate `new`).
5. **`test_excluded_phase_skipped`** — card phase `"Finance Recon"` is skipped regardless of assignees.
6. **`test_excluded_phase_case_insensitive`** — phase `"  finance RECON "` is skipped.
7. **`test_field_assignee_swap`** — field of type `assignee_select` with `[old, X]` becomes `[X, new]`.
8. **`test_field_filter_restricts_scope`** — with `--field creative_specialist`, no other fields are touched.
9. **`test_dry_run_makes_no_mutation_calls`** — mock client records zero `updateCard` / `updateCardField` calls.
10. **`test_apply_invokes_correct_mutations`** — mock client receives the expected mutation payloads.
11. **`test_user_resolution_prefers_email`** — name + email both supplied; email wins.
12. **`test_user_resolution_fails_on_ambiguous_name`** — two org users named "Kate Smith" → fail before mutation.
13. **`test_old_equals_new_refused`** — same email for both → exit non-zero with clear message.
14. **`test_pagination_continues_until_done`** — fixture mock returns two pages; both processed.
15. **`test_rate_limit_retry_then_succeed`** — first call returns 429, second succeeds.
16. **`test_field_prompt_lists_discovered_fields`** — with no `--field` flag and a TTY-mocked stdin, the prompt output contains every assignee-typed field discovered on the pipe (and only those).
17. **`test_field_prompt_accepts_multi_select`** — input `"1,3"` selects exactly the two corresponding field IDs.
18. **`test_field_prompt_rejects_invalid_input_then_retries`** — input `"banana"` re-prompts; `"q"` aborts cleanly with exit code 0 and zero mutations.
19. **`test_non_tty_without_field_flags_errors`** — with stdin not a TTY and neither `--field` nor `--all-fields` set, the CLI exits non-zero and the message lists discovered field IDs.
20. **`test_unknown_field_id_fails_fast`** — `--field does_not_exist` exits non-zero before any card mutation.

---

## 12. Out of scope (do not build)

- Inviting users to a pipe (just resolve and swap).
- Any UI / web server / webhook listener.
- Bulk reassignment to multiple new users (always 1→1 in this version).
- Touching comments, labels, due dates, or any field other than user/assignee fields.
- Cross-pipe swaps (one `--pipe-id` per run).

---

## 13. Definition of done

- `swap-assignee --help` prints the full CLI as in §4.
- All 15 acceptance tests in §11 pass with `pytest -q`.
- Running against a real pipe in dry-run mode produces a sensible plan and zero mutations (verifiable by inspecting Pipefy activity log: empty).
- Running with `--apply --yes` successfully swaps a test card and produces a CSV report.
- README documents installation, config file, and the example commands from §4.
- No bearer tokens appear in any log line at any log level.
