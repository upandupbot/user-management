# Pipefy User-Swap Cheat Sheet

This document lists every Pipefy pipe we use the **swap-assignee** tool against,
the assignee fields on each pipe, and ready-made commands you can copy and run.

> You don't need to be a developer to use this — just open PowerShell, paste a
> command, and follow the prompts.

---

## How to use this document

1. **Find the pipe you want to work on** below (each pipe has its own section).
2. **Look at the field table** to see which assignee fields exist on that pipe
   (e.g. "Account Manager", "Reviewer").
3. **Copy a sample command** from the same section and edit the two email
   addresses (the person being removed and the person taking over).
4. **Run it in PowerShell.** It will show a preview first — nothing changes
   until you re-run with `--apply` at the end.

That's it.

---

## Two-minute glossary

| Term                      | What it means                                                                                                                                                                                                                    |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Pipe**                  | A Pipefy board (e.g. "Creative", "PR Management"). Each has a numeric **pipe id**.                                                                                                                                               |
| **Card**                  | A single job/ticket on a pipe.                                                                                                                                                                                                   |
| **Assignee field**        | A field on a card that holds one or more people (e.g. "Reviewer", "Traffic Manager").                                                                                                                                            |
| **Card-level assignees**  | The avatars at the very top of a card, separate from named assignee fields.                                                                                                                                                      |
| **slug**                  | The short text id of a field, e.g. `trafficker`. Use this with `--field`.                                                                                                                                                        |
| **internal_id**           | The numeric id of a field, e.g. `421110036`. Also accepted by `--field`.                                                                                                                                                         |
| **label**                 | The human name of a field, e.g. "Traffic Manager". Only safe to use with `--field` if it appears **only once** on the pipe. If a label is marked **AMBIGUOUS**, use the slug instead.                                            |
| **Dry-run**               | A preview that shows what _would_ change. The tool does this by default.                                                                                                                                                         |
| **Apply**                 | Adding `--apply` to a command actually performs the swap.                                                                                                                                                                        |
| **Add-only (card-level)** | A mode that **adds** the new person to the top-of-card assignee avatars but **does not remove** the old person. Use it to avoid notification spam (see [Avoiding notification spam](#avoiding-notification-spam-card-add-only)). |

---

## The three rules to remember

1. **Always dry-run first.** Just leave `--apply` off, look at the preview, then re-run with `--apply` if it looks right.
2. **If a label is marked AMBIGUOUS (⚠️)**, use the slug (first column of the table). Otherwise the tool will refuse and tell you which slugs to choose between.
3. **`--no-include-card-level`** means "don't touch the avatars at the top of the card, only the named field". Use this when you want a surgical change.
4. **If you're swapping someone with lots of cards, add `--card-add-only`.** It prevents Pipefy from spamming everyone on every card. You then delete the old person from each card's avatars in the Pipefy admin panel at your leisure. See [Avoiding notification spam](#avoiding-notification-spam-card-add-only).

---

## Generic command shape

```powershell
python -m swap_assignee.cli `
  --pipe-id <PIPE_ID> `
  --old-user <person.leaving@dydx.digital> `
  --new-user <person.taking.over@dydx.digital> `
  --field <slug-or-label> `
  --no-include-card-level
```

Add `--apply` at the very end to actually run the swap.

---

## Avoiding notification spam (`--card-add-only`)

When the tool changes the card-level assignees (the avatars at the top of a
card), Pipefy fires a notification to **every** assignee on that card — not
just the new one. If the person you're swapping is on dozens or hundreds of
cards, that's a flood of notifications going to everyone they're currently
working with. This is a quirk of Pipefy's `updateCard` API, not the tool.

The fix is `--card-add-only`. With this flag:

- The tool **adds** the new user to each card's avatars.
- It **does not remove** the old user.
- Pipefy only sees one new entry per card, so only the new user gets pinged.
- You then remove the old user manually in the Pipefy admin panel — those
  manual deletions don't fire notifications.
- **Field-level swaps are unaffected** — those still remove and replace
  cleanly, because they don't have the same notification storm.

The same toggle exists in the web UI as **"Card-level: add new user only"**
on the options panel.

**Example — full sweep with notification-friendly card-level handling:**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305705781 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --all-fields `
  --card-add-only
```

In the dry-run preview, you'll see card-level rows tagged `(ADD-ONLY)` and a
banner line:

```
Card-level:     ADD-ONLY (keep old user; remove manually in Pipefy to avoid notification spam)
```

After applying, sweep the Pipefy admin panel and remove the old user from the
card-level avatars at your convenience.

---

## Useful discovery commands

| What you want to know                             | Command                                                         |
| ------------------------------------------------- | --------------------------------------------------------------- |
| What assignee fields exist on a pipe?             | `python -m swap_assignee.cli --pipe-id <PIPE_ID> --list-fields` |
| Who can the tool see on a pipe (members + roles)? | `python -m swap_assignee.cli --pipe-id <PIPE_ID> --list-users`  |
| Why isn't a specific card being picked up?        | `python -m swap_assignee.cli --debug-card <CARD_ID>`            |

---

# Pipes

> **Web-UI curated list.** The web UI's pipe picker and its **Find their pipes**
> discovery scan use this curated set (see `KNOWN_PIPES` in
> `swap_assignee/web.py`): Account Management `305705781`,
> Always On (Social Organic) `306751836`, Creative `305709782`,
> Events and Activations `306828440`, Media `305743818`,
> MTN CBU | Social Media `306683702`, MTN EBU - Social Media `306766298`,
> MTN MoMo - Social Media `306766315`, PR Management `306773031`,
> Production `305787071`, Retail `305766460`,
> Social Community Management `305723961`, Strategy `305709783`.
> Slipstream Help & Support (`306283609`) is documented below for CLI use but
> intentionally left out of the web UI list.
> The four social pipes don't have field tables in this document yet — run
> `--list-fields` (see above) if you need them for CLI work; the web UI
> discovers fields automatically.

## 1. Slipstream Help & Support — pipe id `306283609`

> ⚠️ **Heads up:** This pipe has **two different fields both labelled "Assigned Team Member"**.
> If you try to use the label, the tool will stop and ask you to pick a slug.

### Assignee fields

| Field name (label)      | Slug to use with `--field`             | Internal id | Where it lives |
| ----------------------- | -------------------------------------- | ----------- | -------------- |
| Assigned Team Member ⚠️ | `team_member_to_work_on_support_issue` | 411338980   | (phase)        |
| Assigned Team Member ⚠️ | `assigned_team_member`                 | 428278716   | (phase)        |
| Tester                  | `tester`                               | 417668496   | (phase)        |
| Responsible - Escalated | `responsible`                          | 415449966   | (phase)        |
| Tester(s)               | `tester_s`                             | 411121262   | (phase)        |

### Sample commands

**Swap one person on the support "Assigned Team Member" field only**

```powershell
python -m swap_assignee.cli `
  --pipe-id 306283609 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field team_member_to_work_on_support_issue `
  --no-include-card-level
```

**Swap on both "Assigned Team Member" fields at once**

```powershell
python -m swap_assignee.cli `
  --pipe-id 306283609 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field team_member_to_work_on_support_issue `
  --field assigned_team_member `
  --no-include-card-level
```

**Swap a Tester only**

```powershell
python -m swap_assignee.cli `
  --pipe-id 306283609 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field tester --no-include-card-level
```

---

## 2. Account Management — pipe id `305705781`

✅ No duplicate labels — you can use either the label or the slug.

### Assignee fields

| Field name (label)                                   | Slug to use with `--field`                             | Internal id | Where it lives |
| ---------------------------------------------------- | ------------------------------------------------------ | ----------- | -------------- |
| Account Manager                                      | `account_manager_1`                                    | 398404355   | Start form     |
| Traffic Manager                                      | `trafficker`                                           | 413789276   | Start form     |
| Business Unit Director and/or Group Account Director | `business_unit_director_and_or_group_account_director` | 424761911   | Start form     |

### Sample commands

**Swap one Account Manager**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305705781 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field account_manager_1 --no-include-card-level
```

**Swap by label (works because labels are unique on this pipe)**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305705781 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field "Traffic Manager" --no-include-card-level
```

**Sweep across every assignee field + card-level avatars**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305705781 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --all-fields
```

---

## 3. PR Management — pipe id `306773031`

✅ No duplicate labels.

### Assignee fields

| Field name (label)                                   | Slug to use with `--field`                             | Internal id | Where it lives            |
| ---------------------------------------------------- | ------------------------------------------------------ | ----------- | ------------------------- |
| Account Manager                                      | `account_manager_1`                                    | 421110033   | Start form                |
| Traffic Manager                                      | `trafficker`                                           | 421110036   | Start form                |
| PR management team                                   | `pr_management_team`                                   | 422005357   | Start form                |
| PR internal reviewers                                | `strategy_internal_reviewers`                          | 421110034   | Start form                |
| Risk and advisory team                               | `risk_and_advisory_team`                               | 422005358   | Start form                |
| Business Unit Director and/or Group Account Director | `business_unit_director_and_or_group_account_director` | 424764157   | Start form                |
| Reviewer                                             | `reviewer`                                             | 421110037   | Start form                |
| Responsible - Reviewer                               | `responsible_reviewer`                                 | 421110251   | Phase: PR Internal Review |

### Sample commands

**Swap one Reviewer**

```powershell
python -m swap_assignee.cli `
  --pipe-id 306773031 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field reviewer --no-include-card-level
```

**Swap the phase-scoped Responsible reviewer**

```powershell
python -m swap_assignee.cli `
  --pipe-id 306773031 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field responsible_reviewer --no-include-card-level
```

**Multiple specific fields in one run**

```powershell
python -m swap_assignee.cli `
  --pipe-id 306773031 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field trafficker --field reviewer --field responsible_reviewer `
  --no-include-card-level
```

**Full pipe sweep**

```powershell
python -m swap_assignee.cli `
  --pipe-id 306773031 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --all-fields
```

---

## 4. Creative — pipe id `305709782`

> ⚠️ **Big pipe with lots of duplicate labels.** Many of the same role names
> ("Creative specialist(s)", "DTP specialist(s)", "QA", "Reviewer",
> "Responsible - Reviewer") appear on multiple phases. **Always use the slug**
> when working on this pipe — the tool will refuse the label form for these.

### Assignee fields — Start form

| Field name (label)                                   | Slug to use with `--field`                             | Internal id |
| ---------------------------------------------------- | ------------------------------------------------------ | ----------- |
| blank_assignee                                       | `blank_assignee`                                       | 415004648   |
| Reviewer                                             | `reviewer`                                             | 414378631   |
| Account Manager                                      | `account_manager_1`                                    | 398477953   |
| Traffic Manager                                      | `trafficker`                                           | 398478477   |
| QA ⚠️                                                | `qa`                                                   | 407799726   |
| Creative specialist(s) ⚠️                            | `creative_specialist_s`                                | 398680607   |
| Creative Internal Reviewers ⚠️                       | `creative_internal_reviewers`                          | 401058975   |
| DTP specialist(s) ⚠️                                 | `dtp_specialist_s`                                     | 402538970   |
| Animation Producer                                   | `animation_producer`                                   | 424033770   |
| Animation specialist(s) ⚠️                           | `animation_specialist_s`                               | 419641035   |
| Business Unit Director and/or Group Account Director | `business_unit_director_and_or_group_account_director` | 424764459   |

### Assignee fields — Creative Queue (phase)

| Field name (label)            | Slug                           | Internal id |
| ----------------------------- | ------------------------------ | ----------- |
| Creative specialist(s) ⚠️     | `creative_specialist_s_1`      | 414009171   |
| DTP specialist(s) ⚠️          | `dtp_specialist_s_3`           | 416838333   |
| QA ⚠️                         | `qa_2`                         | 414009442   |
| Creative internal reviewer(s) | `creative_internal_reviewer_s` | 414009173   |

### Assignee fields — Concept Queue (phase)

| Field name (label)        | Slug               | Internal id |
| ------------------------- | ------------------ | ----------- |
| Creative specialist(s) ⚠️ | `concept_creative` | 398602233   |

### Assignee fields — Concept Internal Review (phase)

| Field name (label)        | Slug                   | Internal id |
| ------------------------- | ---------------------- | ----------- |
| Responsible - Reviewer ⚠️ | `responsible_reviewer` | 414271316   |

### Assignee fields — Proof of Concept Queue (phase)

| Field name (label)        | Slug                        | Internal id |
| ------------------------- | --------------------------- | ----------- |
| Creative specialist(s) ⚠️ | `proof_of_concept_creative` | 398603162   |

### Assignee fields — Proof of Concept Internal Review (phase)

| Field name (label)        | Slug                     | Internal id |
| ------------------------- | ------------------------ | ----------- |
| Responsible - Reviewer ⚠️ | `responsible_reviewer_1` | 414271318   |

### Assignee fields — Creative Rollout Queue (phase)

| Field name (label)             | Slug                               | Internal id |
| ------------------------------ | ---------------------------------- | ----------- |
| Creative specialist(s) ⚠️      | `creative_team_to_work_on_rollout` | 398630915   |
| DTP specialist(s) ⚠️           | `dtp_specialist_s_2`               | 416218765   |
| Creative internal reviewers ⚠️ | `creative_internal_reviewers_1`    | 409656009   |
| QA ⚠️                          | `qa_1`                             | 409656438   |

### Assignee fields — Creative Rollout Internal Review (phase)

| Field name (label)        | Slug                     | Internal id |
| ------------------------- | ------------------------ | ----------- |
| Responsible - Reviewer ⚠️ | `responsible_reviewer_2` | 414271320   |

### Assignee fields — Animation Queue (phase)

| Field name (label)         | Slug                       | Internal id |
| -------------------------- | -------------------------- | ----------- |
| Animation specialist(s) ⚠️ | `animation_specialist_s_1` | 419641328   |

### Assignee fields — DTP Queue (phase)

| Field name (label)   | Slug                 | Internal id |
| -------------------- | -------------------- | ----------- |
| DTP specialist(s) ⚠️ | `dtp_specialist_s_1` | 402538971   |

### Assignee fields — DTP Internal Review (phase)

| Field name (label)        | Slug                     | Internal id |
| ------------------------- | ------------------------ | ----------- |
| Responsible - Reviewer ⚠️ | `responsible_reviewer_3` | 414271321   |

### Assignee fields — DTP Final Artwork Queue (phase)

| Field name (label)   | Slug                 | Internal id |
| -------------------- | -------------------- | ----------- |
| DTP specialist(s) ⚠️ | `dtp_specialist_s_4` | 418582593   |

### Sample commands

**Replace someone as the Account Manager (start form)**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305709782 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field account_manager_1 --no-include-card-level
```

**Replace a Creative specialist on the Concept Queue only**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305709782 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field concept_creative --no-include-card-level
```

**Replace a Creative specialist EVERYWHERE they appear (start form + every phase)**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305709782 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field creative_specialist_s `
  --field creative_specialist_s_1 `
  --field concept_creative `
  --field proof_of_concept_creative `
  --field creative_team_to_work_on_rollout `
  --no-include-card-level
```

**Replace a DTP specialist across every DTP-related phase**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305709782 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field dtp_specialist_s `
  --field dtp_specialist_s_1 `
  --field dtp_specialist_s_2 `
  --field dtp_specialist_s_3 `
  --field dtp_specialist_s_4 `
  --no-include-card-level
```

**Replace a "Responsible - Reviewer" across every internal-review phase**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305709782 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field responsible_reviewer `
  --field responsible_reviewer_1 `
  --field responsible_reviewer_2 `
  --field responsible_reviewer_3 `
  --no-include-card-level
```

**Full pipe sweep across every assignee field + card-level avatars**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305709782 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --all-fields
```

---

## 5. Events and Activations — pipe id `306828440`

> ⚠️ **Several roles appear twice** (once on the start form, once on the
> "Events and Activations Queue" phase): **Events manager**, **Operations manager**,
> and **Events and activations reviewer(s)**. Always use the slug for these.

### Assignee fields — Start form

| Field name (label)                                   | Slug to use with `--field`                             | Internal id |
| ---------------------------------------------------- | ------------------------------------------------------ | ----------- |
| Account Manager                                      | `account_manager_1`                                    | 422262743   |
| Traffic Manager                                      | `trafficker`                                           | 422262747   |
| Business Unit Director and/or Group Account Director | `business_unit_director_and_or_group_account_director` | 424764160   |
| Events manager ⚠️                                    | `events_manager`                                       | 422263081   |
| Operations manager ⚠️                                | `operations_manager`                                   | 422263083   |
| Events and activations reviewer(s) ⚠️                | `events_and_activations_reviewer_s`                    | 422263085   |
| Reviewer                                             | `reviewer`                                             | 422262748   |

### Assignee fields — Events and Activations Queue (phase)

| Field name (label)                    | Slug                                  | Internal id |
| ------------------------------------- | ------------------------------------- | ----------- |
| Events manager ⚠️                     | `events_manager_1`                    | 422263082   |
| Operations manager ⚠️                 | `operations_manager_1`                | 422263084   |
| Events and activations reviewer(s) ⚠️ | `events_and_activations_reviewer_s_1` | 422263086   |

### Assignee fields — Activations Internal Review (phase)

| Field name (label)     | Slug                   | Internal id |
| ---------------------- | ---------------------- | ----------- |
| Responsible - Reviewer | `responsible_reviewer` | 422262889   |

### Sample commands

**Replace someone as the Account Manager (start form)**

```powershell
python -m swap_assignee.cli `
  --pipe-id 306828440 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field account_manager_1 --no-include-card-level
```

**Replace someone as the Traffic Manager**

```powershell
python -m swap_assignee.cli `
  --pipe-id 306828440 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field trafficker --no-include-card-level
```

**Replace an Events manager EVERYWHERE they appear (start form + queue)**

```powershell
python -m swap_assignee.cli `
  --pipe-id 306828440 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field events_manager `
  --field events_manager_1 `
  --no-include-card-level
```

**Replace an Operations manager EVERYWHERE they appear**

```powershell
python -m swap_assignee.cli `
  --pipe-id 306828440 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field operations_manager `
  --field operations_manager_1 `
  --no-include-card-level
```

**Replace an Events & Activations reviewer EVERYWHERE they appear**

```powershell
python -m swap_assignee.cli `
  --pipe-id 306828440 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field events_and_activations_reviewer_s `
  --field events_and_activations_reviewer_s_1 `
  --no-include-card-level
```

**Replace the Responsible reviewer on the internal-review phase**

```powershell
python -m swap_assignee.cli `
  --pipe-id 306828440 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field responsible_reviewer --no-include-card-level
```

**Full pipe sweep across every assignee field + card-level avatars**

```powershell
python -m swap_assignee.cli `
  --pipe-id 306828440 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --all-fields
```

---

## 6. Production — pipe id `305787071`

> ⚠️ **One duplicate label:** "Responsible - Reviewer" appears on two different
> internal-review phases. Use the slug for those.

### Assignee fields — Start form

| Field name (label)                                   | Slug to use with `--field`                             | Internal id |
| ---------------------------------------------------- | ------------------------------------------------------ | ----------- |
| Account Manager                                      | `account_manager_1`                                    | 400102023   |
| Traffic Manager                                      | `trafficker`                                           | 400102024   |
| Retail Account Manager                               | `retail_account_manager`                               | 410637436   |
| Production specialist(s)                             | `production_specialist_s`                              | 400102219   |
| Business Unit Director and/or Group Account Director | `business_unit_director_and_or_group_account_director` | 424764453   |
| Reviewer                                             | `reviewer`                                             | 415269120   |

### Assignee fields — Pre-Production Internal Review (phase)

| Field name (label)        | Slug                   | Internal id |
| ------------------------- | ---------------------- | ----------- |
| Responsible - Reviewer ⚠️ | `responsible_reviewer` | 414271344   |

### Assignee fields — Production Internal Review (phase)

| Field name (label)        | Slug                     | Internal id |
| ------------------------- | ------------------------ | ----------- |
| Responsible - Reviewer ⚠️ | `responsible_reviewer_1` | 414271346   |

### Sample commands

**Replace someone as the Account Manager**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305787071 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field account_manager_1 --no-include-card-level
```

**Replace someone as the Traffic Manager**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305787071 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field trafficker --no-include-card-level
```

**Replace a Production specialist**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305787071 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field production_specialist_s --no-include-card-level
```

**Replace the Responsible reviewer on BOTH internal-review phases**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305787071 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field responsible_reviewer `
  --field responsible_reviewer_1 `
  --no-include-card-level
```

**Full pipe sweep across every assignee field + card-level avatars**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305787071 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --all-fields
```

---

## 7. Media — pipe id `305743818`

> ⚠️ **Lots of duplicate labels** across the start form and various phases.
> Roles like **Media Strategist(s)**, **Media Planner**, **Digital Campaign Manager**,
> **Media deliverable reviewers**, and **Responsible - Reviewer** appear in
> multiple places. Always use the slug — the tool will refuse the label form for these.

### Assignee fields — Start form

| Field name (label)                                   | Slug to use with `--field`                             | Internal id |
| ---------------------------------------------------- | ------------------------------------------------------ | ----------- |
| Account Manager                                      | `account_manager_1`                                    | 399184948   |
| Media Business Unit Head / Digital Lead              | `media_business_unit_head_1`                           | 408595892   |
| Media specialist(s)                                  | `media_specialist_s_1`                                 | 399287685   |
| Media Strategist(s) ⚠️                               | `media_strategist_s_1`                                 | 408595866   |
| Media Planner ⚠️                                     | `media_planner_1`                                      | 411525062   |
| Media Buyer                                          | `media_buyer`                                          | 408595933   |
| Digital Campaign Manager ⚠️                          | `digital_campaign_manager_1`                           | 411560735   |
| Media deliverable reviewers ⚠️                       | `media_reviewers`                                      | 399187446   |
| Reviewer                                             | `reviewer`                                             | 415268269   |
| Business Unit Director and/or Group Account Director | `business_unit_director_and_or_group_account_director` | 424764524   |

### Assignee fields — Media Brief (phase)

| Field name (label)             | Slug                       | Internal id |
| ------------------------------ | -------------------------- | ----------- |
| Media Strategist(s) ⚠️         | `media_strategist_s`       | 408595852   |
| Media Planner(s)               | `media_planner_s`          | 411520797   |
| Media Buyer(s)                 | `media_buyer_1`            | 408595935   |
| Digital Campaign Manager ⚠️    | `digital_campaign_manager` | 411560596   |
| Media deliverable reviewers ⚠️ | `media_reviewers_1`        | 411176556   |

### Assignee fields — Kickstart (phase)

| Field name (label)  | Slug               | Internal id |
| ------------------- | ------------------ | ----------- |
| Media Strategist ⚠️ | `media_strategist` | 408595954   |

### Assignee fields — Media Strategy Internal Review (phase)

| Field name (label)  | Slug                 | Internal id |
| ------------------- | -------------------- | ----------- |
| Media Strategist ⚠️ | `media_strategist_1` | 411524810   |

### Assignee fields — Media Plan Internal Review (phase)

| Field name (label)        | Slug                     | Internal id |
| ------------------------- | ------------------------ | ----------- |
| Media Planner ⚠️          | `media_planner`          | 411524887   |
| Responsible - Reviewer ⚠️ | `responsible_reviewer_1` | 414271462   |

### Assignee fields — Media Buying (phase)

| Field name (label)        | Slug                      | Internal id |
| ------------------------- | ------------------------- | ----------- |
| Responsible - Media Buyer | `responsible_media_buyer` | 416058387   |

### Assignee fields — Digital Campaign Build (phase)

| Field name (label)          | Slug                                 | Internal id |
| --------------------------- | ------------------------------------ | ----------- |
| Digital Campaign Manager ⚠️ | `responsible_digital_campaign_build` | 411556759   |

### Assignee fields — Reporting Internal Review (phase)

| Field name (label)        | Slug                     | Internal id |
| ------------------------- | ------------------------ | ----------- |
| Responsible - Reviewer ⚠️ | `responsible_reviewer_2` | 414271488   |

### Sample commands

**Replace someone as the Account Manager**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305743818 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field account_manager_1 --no-include-card-level
```

**Replace a Media Strategist EVERYWHERE they appear**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305743818 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field media_strategist_s_1 `
  --field media_strategist_s `
  --field media_strategist `
  --field media_strategist_1 `
  --no-include-card-level
```

**Replace a Media Planner EVERYWHERE they appear**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305743818 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field media_planner_1 `
  --field media_planner_s `
  --field media_planner `
  --no-include-card-level
```

**Replace a Media Buyer EVERYWHERE they appear (start form + Media Brief + Media Buying)**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305743818 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field media_buyer `
  --field media_buyer_1 `
  --field responsible_media_buyer `
  --no-include-card-level
```

**Replace a Digital Campaign Manager EVERYWHERE they appear**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305743818 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field digital_campaign_manager_1 `
  --field digital_campaign_manager `
  --field responsible_digital_campaign_build `
  --no-include-card-level
```

**Replace a Media deliverable reviewer EVERYWHERE they appear**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305743818 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field media_reviewers `
  --field media_reviewers_1 `
  --no-include-card-level
```

**Replace the Responsible reviewer on every internal-review phase**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305743818 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field responsible_reviewer_1 `
  --field responsible_reviewer_2 `
  --no-include-card-level
```

**Full pipe sweep across every assignee field + card-level avatars**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305743818 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --all-fields
```

---

## 8. Retail — pipe id `305766460`

> ⚠️ **Lots of duplicate labels.** Roles like **Retail creative team**,
> **Retail internal reviewers**, **DTP specialist(s)**, and **QA** appear in
> multiple places. Always use the slug for those.
>
> Also note two label oddities on this pipe:
>
> - The **start-form "Retail Traffic Manager"** field has the slug `trafficker`
>   (the same slug used for "Traffic Manager" on other pipes).
> - The **start-form "Creative internal reviewers"** field has the slug
>   `creative_director` (Pipefy slugs were renamed at some point but the
>   underlying id stayed the same).

### Assignee fields — Start form

| Field name (label)           | Slug to use with `--field`  | Internal id |
| ---------------------------- | --------------------------- | ----------- |
| Campaign Account Manager     | `campaign_account_manager`  | 411177708   |
| Account Manager              | `account_manager_1`         | 399662837   |
| Retail Traffic Manager       | `trafficker`                | 399662838   |
| Retail creative team ⚠️      | `retail_specialist_s`       | 399663074   |
| Retail internal reviewers ⚠️ | `retail_internal_reviewers` | 401065832   |
| Creative internal reviewers  | `creative_director`         | 418395538   |
| QA ⚠️                        | `qa`                        | 418395637   |
| Reviewer                     | `reviewer`                  | 414484200   |
| DTP specialist(s) ⚠️         | `dtp_specialist_s_1`        | 402558748   |

### Assignee fields — Retail Brief (phase)

| Field name (label)           | Slug                          | Internal id |
| ---------------------------- | ----------------------------- | ----------- |
| Retail Account Manager       | `retail_account_manager`      | 403763942   |
| Retail internal reviewers ⚠️ | `retail_internal_reviewers_1` | 411105161   |
| Retail QA                    | `retail_qa_1`                 | 411106938   |

### Assignee fields — Creative Resource Allocation (phase)

| Field name (label)            | Slug                          | Internal id |
| ----------------------------- | ----------------------------- | ----------- |
| Retail Creative Team ⚠️       | `retail_specialist_s_1`       | 399664890   |
| DTP specialist(s) ⚠️          | `dtp_specialist_s_3`          | 414947353   |
| Creative internal reviewer(s) | `retail_internal_reviewers_2` | 411176879   |
| QA ⚠️                         | `qa_1`                        | 418582559   |

### Assignee fields — Creative Rollout Queue (phase)

| Field name (label)      | Slug                        | Internal id |
| ----------------------- | --------------------------- | ----------- |
| Retail creative team ⚠️ | `proof_of_concept_creative` | 399662973   |
| DTP specialist(s) ⚠️    | `dtp_specialist_s_2`        | 414525939   |

### Assignee fields — DTP Queue (phase)

| Field name (label)   | Slug               | Internal id |
| -------------------- | ------------------ | ----------- |
| DTP specialist(s) ⚠️ | `dtp_specialist_s` | 402558746   |

### Assignee fields — DTP Final Artwork Queue (phase)

| Field name (label)   | Slug                 | Internal id |
| -------------------- | -------------------- | ----------- |
| DTP specialist(s) ⚠️ | `dtp_specialist_s_4` | 418583608   |

### Sample commands

**Replace someone as the Account Manager**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305766460 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field account_manager_1 --no-include-card-level
```

**Replace the Retail Traffic Manager**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305766460 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field trafficker --no-include-card-level
```

**Replace someone on the Retail creative team EVERYWHERE they appear**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305766460 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field retail_specialist_s `
  --field retail_specialist_s_1 `
  --field proof_of_concept_creative `
  --no-include-card-level
```

**Replace a DTP specialist EVERYWHERE they appear (start form + every phase)**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305766460 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field dtp_specialist_s_1 `
  --field dtp_specialist_s_3 `
  --field dtp_specialist_s_2 `
  --field dtp_specialist_s `
  --field dtp_specialist_s_4 `
  --no-include-card-level
```

**Replace a Retail internal reviewer EVERYWHERE they appear**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305766460 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field retail_internal_reviewers `
  --field retail_internal_reviewers_1 `
  --no-include-card-level
```

**Replace a QA reviewer EVERYWHERE they appear**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305766460 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field qa `
  --field qa_1 `
  --field retail_qa_1 `
  --no-include-card-level
```

**Full pipe sweep across every assignee field + card-level avatars**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305766460 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --all-fields
```

---

## 9. Strategy — pipe id `305709783`

> ⚠️ **Strategy specialist(s)** appears on the start form and on three phases.
> Always use the slug for that role.

### Assignee fields — Start form

| Field name (label)                                   | Slug to use with `--field`                             | Internal id |
| ---------------------------------------------------- | ------------------------------------------------------ | ----------- |
| Account Manager                                      | `account_manager_1`                                    | 398478093   |
| Traffic Manager                                      | `trafficker`                                           | 398616031   |
| Strategy internal reviewers                          | `strategy_internal_reviewers`                          | 409584076   |
| Strategy specialist(s) ⚠️                            | `strategy_specialist_s`                                | 398731052   |
| Business Unit Director and/or Group Account Director | `business_unit_director_and_or_group_account_director` | 424764468   |
| Reviewer                                             | `reviewer`                                             | 414385760   |

### Assignee fields — Strategy Queue (phase)

| Field name (label)            | Slug                           | Internal id |
| ----------------------------- | ------------------------------ | ----------- |
| Strategy specialist(s) ⚠️     | `strategy_specialist_s_1`      | 411183865   |
| Strategy internal reviewer(s) | `strategy_internal_reviewer_s` | 414008323   |

### Assignee fields — Strategy Development Queue (phase)

| Field name (label)        | Slug           | Internal id |
| ------------------------- | -------------- | ----------- |
| Strategy specialist(s) ⚠️ | `strategist_s` | 398604326   |

### Assignee fields — Strategy Internal Review (phase)

| Field name (label)     | Slug                   | Internal id |
| ---------------------- | ---------------------- | ----------- |
| Responsible - Reviewer | `responsible_reviewer` | 414271324   |

### Assignee fields — Solutions Brief (phase)

| Field name (label)        | Slug                      | Internal id |
| ------------------------- | ------------------------- | ----------- |
| Strategy specialist(s) ⚠️ | `strategy_specialist_s_2` | 411847455   |

### Sample commands

**Replace someone as the Account Manager**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305709783 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field account_manager_1 --no-include-card-level
```

**Replace someone as the Traffic Manager**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305709783 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field trafficker --no-include-card-level
```

**Replace a Strategy specialist EVERYWHERE they appear (start form + every phase)**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305709783 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field strategy_specialist_s `
  --field strategy_specialist_s_1 `
  --field strategist_s `
  --field strategy_specialist_s_2 `
  --no-include-card-level
```

**Replace a Strategy internal reviewer (start form + Strategy Queue)**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305709783 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field strategy_internal_reviewers `
  --field strategy_internal_reviewer_s `
  --no-include-card-level
```

**Replace the Responsible reviewer on the internal-review phase**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305709783 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field responsible_reviewer --no-include-card-level
```

**Full pipe sweep across every assignee field + card-level avatars**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305709783 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --all-fields
```

---

## 10. Social Community Management — pipe id `305723961`

> ⚠️ **"Social Community Manager"** appears on the start form and on the
> Social Queue phase. Use the slug for that role.

### Assignee fields — Start form

| Field name (label)                                   | Slug to use with `--field`                             | Internal id |
| ---------------------------------------------------- | ------------------------------------------------------ | ----------- |
| Account Manager                                      | `account_manager_1`                                    | 398758590   |
| Traffic Manager                                      | `trafficker`                                           | 398758591   |
| Social Community Manager ⚠️                          | `social_specialist_s`                                  | 398838460   |
| Social community media management internal reviewers | `social_community_media_management_internal_reviewers` | 409584151   |
| Business Unit Director and/or Group Account Director | `business_unit_director_and_or_group_account_director` | 424764452   |
| Reviewer                                             | `reviewer`                                             | 416026588   |

### Assignee fields — Social Queue (phase)

| Field name (label)                             | Slug                                                     | Internal id |
| ---------------------------------------------- | -------------------------------------------------------- | ----------- |
| Social Community Manager ⚠️                    | `social_community_manager`                               | 405377990   |
| Social Community Management internal reviewers | `social_community_media_management_internal_reviewers_1` | 414378510   |

### Sample commands

**Replace someone as the Account Manager**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305723961 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field account_manager_1 --no-include-card-level
```

**Replace someone as the Traffic Manager**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305723961 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field trafficker --no-include-card-level
```

**Replace a Social Community Manager EVERYWHERE they appear**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305723961 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field social_specialist_s `
  --field social_community_manager `
  --no-include-card-level
```

**Replace a Social Community Management internal reviewer EVERYWHERE they appear**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305723961 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --field social_community_media_management_internal_reviewers `
  --field social_community_media_management_internal_reviewers_1 `
  --no-include-card-level
```

**Full pipe sweep across every assignee field + card-level avatars**

```powershell
python -m swap_assignee.cli `
  --pipe-id 305723961 `
  --old-user old.person@dydx.digital `
  --new-user new.person@dydx.digital `
  --all-fields
```

---

# Adding a new pipe to this document

1. Get the pipe id from the Pipefy URL: `app.pipefy.com/pipes/<PIPE_ID>`.
2. Run the discovery command:
   ```powershell
   python -m swap_assignee.cli --pipe-id <PIPE_ID> --list-fields
   ```
3. Copy the rows into a new section here, grouping by phase if there are many.
4. Mark any row whose label appears more than once with ⚠️ and add the
   "Always use the slug" note at the top of the section.
5. Add 2–4 sample commands that fit how your team typically uses that pipe.
