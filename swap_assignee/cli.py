"""argparse-based CLI entry point for the Pipefy assignee swap."""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Iterable, List, Optional, Sequence, Set

from .config import load_config, mask_secret
from .pipefy_client import PipefyClient
from .papertrail_logger import attach_papertrail
from .reports import ReportWriter
from .user_resolver import (
    User,
    fetch_org_users,
    fetch_pipe_context,
    resolve_user,
    scan_pipe_cards_for_user,
    UserResolutionError,
)
from .card_iterator import discover_assignee_fields, iter_cards
from .swap_engine import (
    DEFAULT_EXCLUDED_PHASES,
    execute_plan,
    plan_card,
)


log = logging.getLogger("swap_assignee")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swap-assignee",
        description="Swap a Pipefy user for another on every card in a pipe (card-level + assignee fields).",
    )
    p.add_argument("--pipe-id", required=False, default=None, help="Pipefy pipe ID to scan. Required for swap and --list-users; optional with --debug-card.")
    p.add_argument("--old-user", help="Email (preferred) or name of the user to remove. Required unless --list-users.")
    p.add_argument("--new-user", help="Email (preferred) or name of the user to add. Required unless --list-users.")
    p.add_argument(
        "--list-users",
        action="store_true",
        help="List every pipe member (with role) and org user the bot can see, then exit. Useful for diagnosing restricted-view members.",
    )
    p.add_argument(
        "--list-fields",
        action="store_true",
        help="List every assignee-typed field on the pipe (slug, internal_id, label, phase) and exit. Useful for picking values for --field.",
    )
    p.add_argument(
        "--debug-card",
        default=None,
        help="Fetch a single card by id and dump its raw assignee fields (JSON), then exit. For diagnosing field shape issues.",
    )
    p.add_argument(
        "--trace-card",
        default=None,
        help="During the swap pass, dump the raw payload for this card id as seen by the bulk iterator. For diagnosing why a card is being skipped.",
    )
    p.add_argument(
        "--exclude-phase",
        action="append",
        default=None,
        help='Phase name to skip (case-insensitive). Repeatable. Defaults to "Cancelled", "Completed", "Finance Recon" if omitted.',
    )
    p.add_argument("--field", action="append", default=None, help="Restrict to a specific assignee field (id or label, quote labels with spaces). Repeatable.")
    p.add_argument("--all-fields", action="store_true", help="Operate on every assignee field on the pipe.")
    p.add_argument(
        "--include-card-level",
        dest="include_card_level",
        action="store_true",
        default=True,
        help="Include card-level assignees (default).",
    )
    p.add_argument(
        "--no-include-card-level",
        dest="include_card_level",
        action="store_false",
        help="Skip card-level assignees.",
    )
    p.add_argument("--apply", action="store_true", help="Actually mutate. Default is dry-run.")
    p.add_argument("--yes", action="store_true", help="Skip confirmation prompt for bulk apply.")
    p.add_argument("--config", default="config.ini", help="Path to config.ini (default: ./config.ini).")
    p.add_argument("--limit", type=int, default=None, help="Cap cards considered (for testing).")
    p.add_argument("--throttle-ms", type=int, default=250, help="Sleep between mutations (ms).")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--report", default=None, help="Write CSV report to this path.")
    p.add_argument("--org-id", default=None, help="Override organization ID (otherwise auto-detected from pipe).")
    p.add_argument("--confirm-threshold", type=int, default=5, help="Prompt before applying when more than N cards would change.")
    p.add_argument(
        "--preview-limit",
        type=int,
        default=50,
        help="Max number of affected cards to print in the preview table (0 = unlimited).",
    )
    p.add_argument(
        "--no-preview",
        action="store_true",
        help="Suppress the per-card preview table (summary only).",
    )
    return p


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _print_preview(plans, *, old_user, new_user, limit: int, file=None) -> None:
    """Print a human-readable table of cards that would be affected."""
    if file is None:
        file = sys.stdout
    if not plans:
        file.write("\nNo cards would be affected.\n")
        return

    total = len(plans)
    shown = plans if (limit is None or limit <= 0) else plans[:limit]

    file.write("\n")
    file.write(f"Cards that would be affected ({total}):\n")
    file.write("-" * 78 + "\n")
    for i, plan in enumerate(shown, start=1):
        file.write(f"{i:>4}. {plan.title or '(untitled)'}  [phase: {plan.phase_name or '?'}]\n")
        file.write(f"      card_id={plan.card_id}")
        if plan.card_url:
            file.write(f"  url={plan.card_url}")
        file.write("\n")
        for action in plan.actions:
            scope = "card-level" if action.scope == "card" else f"field {action.field_id}"
            old_ids = ",".join(action.old_ids) or "(none)"
            new_ids = ",".join(action.new_ids) or "(none)"
            file.write(
                f"        - {scope}: [{old_ids}] -> [{new_ids}]"
                f"  (remove {old_user.id}, add {new_user.id})\n"
            )
    if len(shown) < total:
        file.write(f"... {total - len(shown)} more not shown (raise --preview-limit to see all).\n")
    file.write("-" * 78 + "\n")
    file.flush()


def _interactive_field_selection(fields_meta: dict, *, stdin=None, stdout=None) -> Optional[Set[str]]:
    """Prompt the user to pick fields. Returns a set of field IDs (empty set = all).

    A return of None means "abort".
    The special index 0 represents card-level (which is handled by --include-card-level).
    """
    if stdin is None:
        stdin = sys.stdin
    if stdout is None:
        stdout = sys.stdout
    items = sorted(fields_meta.values(), key=lambda f: (f["label"].lower(), f["id"]))
    stdout.write(f"Found {len(items)} user/assignee field(s) on pipe:\n")
    stdout.write("  [0] (card-level assignees)\n")
    for i, f in enumerate(items, start=1):
        stdout.write(f"  [{i}] {f['id']:<24} — \"{f['label']}\"  ({f['type']})\n")
    stdout.write(
        "\nWhich field(s) should the swap apply to?\n"
        '  - Enter numbers separated by commas (e.g. "1,3")\n'
        '  - Enter "all" to swap every field above\n'
        '  - Enter "q" to abort\n> '
    )
    stdout.flush()
    while True:
        try:
            raw = stdin.readline()
        except KeyboardInterrupt:
            return None
        if raw == "":
            return None
        choice = raw.strip().lower()
        if choice in ("q", "quit", "exit"):
            return None
        if choice == "all":
            return set(f["id"] for f in items)
        try:
            picks = [int(x.strip()) for x in choice.split(",") if x.strip()]
        except ValueError:
            stdout.write('Invalid input. Use comma-separated numbers, "all", or "q".\n> ')
            stdout.flush()
            continue
        if not picks or any(p < 0 or p > len(items) for p in picks):
            stdout.write('Invalid input. Use comma-separated numbers, "all", or "q".\n> ')
            stdout.flush()
            continue
        chosen: Set[str] = set()
        # Index 0 means card-level only — handled outside; skip it here.
        for p in picks:
            if p == 0:
                continue
            chosen.add(items[p - 1]["id"])
        # If they only selected 0, return empty set (no field-level).
        return chosen


def _debug_card_mode(client, card_id: str) -> int:
    """Fetch a single card and dump its assignee fields raw, to diagnose parser mismatches."""
    import json as _json
    query = """
    query DebugCard($id: ID!) {
      card(id: $id) {
        id title url
        current_phase { id name }
        assignees { id name email }
        fields {
          name value array_value
          field { id label type }
        }
      }
    }
    """
    try:
        data = client.execute(query, {"id": str(card_id)})
    except Exception as exc:
        print(f"ERROR: could not fetch card {card_id}: {exc}", file=sys.stderr)
        return 2
    card = (data or {}).get("card")
    if not card:
        print(f"ERROR: card {card_id} not found.", file=sys.stderr)
        return 2

    print(f"Card {card['id']}  '{card.get('title')}'")
    print(f"Phase:  {(card.get('current_phase') or {}).get('name')}")
    print(f"URL:    {card.get('url')}")
    print()
    print("Card-level assignees:")
    for a in card.get("assignees") or []:
        print(f"  id={a.get('id')}  {a.get('name')} <{a.get('email')}>")
    print()
    print("Assignee-typed fields:")
    found = False
    for f in card.get("fields") or []:
        meta = (f or {}).get("field") or {}
        if meta.get("type") not in {"assignee_select", "user", "member"}:
            continue
        found = True
        print(f"  field id={meta.get('id')}  label={meta.get('label')!r}  type={meta.get('type')}")
        print(f"    value       = {_json.dumps(f.get('value'))}")
        print(f"    array_value = {_json.dumps(f.get('array_value'))}")
    if not found:
        print("  (none with type assignee_select/user/member)")
    print()
    print("All other fields (name -> value):")
    for f in card.get("fields") or []:
        meta = (f or {}).get("field") or {}
        if meta.get("type") in {"assignee_select", "user", "member"}:
            continue
        print(f"  {meta.get('label')!r} (type={meta.get('type')})  value={_json.dumps(f.get('value'))[:120]}")
    return 0


def _list_fields_mode(client, pipe_id: str) -> int:
    """Print every assignee-typed field on the pipe with its slug, internal_id, label, and location."""
    query = """
    query ListFields($id: ID!) {
      pipe(id: $id) {
        id name
        start_form_fields { id internal_id label type }
        phases { id name fields { id internal_id label type } }
      }
    }
    """
    try:
        data = client.execute(query, {"id": str(pipe_id)})
    except Exception as exc:
        print(f"ERROR: could not fetch pipe {pipe_id}: {exc}", file=sys.stderr)
        return 2
    pipe = (data or {}).get("pipe")
    if not pipe:
        print(f"ERROR: pipe {pipe_id} not found.", file=sys.stderr)
        return 2

    assignee_types = {"assignee_select", "user", "member"}
    rows = []  # (slug, internal_id, type, label, location)
    for f in pipe.get("start_form_fields") or []:
        if f and f.get("type") in assignee_types:
            rows.append((f.get("id") or "", f.get("internal_id") or "", f.get("type") or "", f.get("label") or "", "Start form"))
    for ph in pipe.get("phases") or []:
        ph_name = (ph or {}).get("name") or "?"
        for f in (ph or {}).get("fields") or []:
            if f and f.get("type") in assignee_types:
                rows.append((f.get("id") or "", f.get("internal_id") or "", f.get("type") or "", f.get("label") or "", f"Phase: {ph_name}"))

    print(f"Pipe: {pipe.get('name')} (id {pipe.get('id')})")
    print()
    if not rows:
        print("No assignee-typed fields found on this pipe.")
        return 0

    # Detect ambiguous labels.
    label_counts: Dict[str, int] = {}
    for _, _, _, lbl, _ in rows:
        norm = " ".join(lbl.split()).strip().lower()
        if norm:
            label_counts[norm] = label_counts.get(norm, 0) + 1

    slug_w = max(len(r[0]) for r in rows + [("slug", "", "", "", "")])
    iid_w = max(len(r[1]) for r in rows + [("", "internal_id", "", "", "")])
    type_w = max(len(r[2]) for r in rows + [("", "", "type", "", "")])
    label_w = max(len(r[3]) for r in rows + [("", "", "", "label", "")])
    print(f"{'slug'.ljust(slug_w)}  {'internal_id'.ljust(iid_w)}  {'type'.ljust(type_w)}  {'label'.ljust(label_w)}  location")
    print(f"{'-'*slug_w}  {'-'*iid_w}  {'-'*type_w}  {'-'*label_w}  {'-'*8}")
    for slug, iid, ftype, lbl, loc in rows:
        flag = "  *AMBIGUOUS*" if label_counts.get(" ".join(lbl.split()).strip().lower(), 0) > 1 else ""
        print(f"{slug.ljust(slug_w)}  {iid.ljust(iid_w)}  {ftype.ljust(type_w)}  {lbl.ljust(label_w)}  {loc}{flag}")

    ambiguous = [lbl for lbl, n in label_counts.items() if n > 1]
    if ambiguous:
        print()
        print("NOTE: labels marked *AMBIGUOUS* match multiple fields. Use the slug (first column) with --field, not the label.")
    print()
    print(f"Tip: pass --field <slug> (or --field <internal_id>) to target a specific field. Use --all-fields to swap across every assignee field.")
    return 0


def _list_users_mode(client, pipe_id: str, org_id_override: Optional[str]) -> int:
    """Print every pipe member (with role) and every org user the bot can see, then return."""
    try:
        ctx = fetch_pipe_context(client, pipe_id)
    except Exception as exc:
        print(f"ERROR: could not fetch pipe context: {exc}", file=sys.stderr)
        return 2
    members = ctx["members"]
    pipe_name = ctx.get("pipe_name") or "?"
    org_id = org_id_override or ctx.get("org_id")
    org_name = ctx.get("org_name") or "?"

    print(f"Pipe:  {pipe_name} (id {pipe_id})")
    print(f"Org:   {org_name} (id {org_id or '?'})")
    print()
    print(f"Pipe members ({len(members)}):")
    if not members:
        print("  (none — bot may lack visibility)")
    else:
        for u in sorted(members, key=lambda m: (m.email or m.name).lower()):
            role = u.role or "?"
            print(f"  {u.id:<12} {role:<22} {u.name:<30} <{u.email}>")

    print()
    if org_id:
        try:
            org_users = fetch_org_users(client, org_id)
        except Exception as exc:
            print(f"Org users: (could not fetch — {exc})")
            org_users = []
    else:
        org_users = []
        print("Org users: (no org id — pass --org-id to fetch)")

    if org_users:
        member_ids = {m.id for m in members}
        extras = [u for u in org_users if u.id not in member_ids]
        print(f"Org users not already listed as pipe members ({len(extras)}):")
        for u in sorted(extras, key=lambda m: (m.email or m.name).lower()):
            print(f"  {u.id:<12} {'(org-only)':<22} {u.name:<30} <{u.email}>")
    return 0


def _resolve_users(client, pipe_id: str, old_input: str, new_input: str, org_id_override: Optional[str]):
    ctx = fetch_pipe_context(client, pipe_id)
    org_id = org_id_override or ctx.get("org_id")
    org_users: List[User] = []
    if org_id:
        try:
            org_users = fetch_org_users(client, org_id)
        except Exception as exc:
            log.warning("Could not fetch org users for org %s: %s", org_id, exc)
    members = ctx["members"]
    log.info(
        "Resolving users against %d pipe members and %d org users (org_id=%s)",
        len(members), len(org_users), org_id or "?",
    )

    def _resolve_with_fallback(value: str) -> User:
        try:
            return resolve_user(value, members, org_users)
        except UserResolutionError as primary_exc:
            log.warning(
                "User '%s' not found in pipe members or org users; "
                "scanning pipe cards for an existing assignee match...",
                value,
            )
            found = scan_pipe_cards_for_user(client, pipe_id, value)
            if found:
                log.info("Resolved '%s' via card scan: id=%s name=%s", value, found.id, found.name)
                return found
            raise primary_exc

    old_user = _resolve_with_fallback(old_input)
    new_user = _resolve_with_fallback(new_input)
    return ctx, old_user, new_user


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)

    cfg = load_config(args.config)
    if not cfg.pipefy_api_token:
        print("ERROR: Pipefy API token missing. Set [Pipefy] pipefy_api_token in config.ini or PIPEFY_API_TOKEN env var.", file=sys.stderr)
        return 2

    attach_papertrail(cfg.papertrail_token, cfg.papertrail_host, cfg.papertrail_port)
    log.debug("Using Pipefy URL %s with token %s", cfg.pipefy_api_url, mask_secret(cfg.pipefy_api_token))

    client = PipefyClient(cfg.pipefy_api_url, cfg.pipefy_api_token)

    # --list-users mode: enumerate pipe members + org users, then exit.
    if args.list_users:
        return _list_users_mode(client, args.pipe_id, args.org_id or cfg.org_id)

    # --list-fields mode: enumerate every assignee-typed field then exit.
    if args.list_fields:
        if not args.pipe_id:
            print("ERROR: --pipe-id is required with --list-fields.", file=sys.stderr)
            return 2
        return _list_fields_mode(client, args.pipe_id)

    # --debug-card mode: dump raw card payload then exit.
    if args.debug_card:
        return _debug_card_mode(client, args.debug_card)

    if not args.pipe_id:
        print("ERROR: --pipe-id is required (unless using --debug-card).", file=sys.stderr)
        return 2

    if not args.old_user or not args.new_user:
        print("ERROR: --old-user and --new-user are required (unless --list-users).", file=sys.stderr)
        return 2

    if args.old_user.strip().lower() == args.new_user.strip().lower():
        print("ERROR: --old-user and --new-user are the same value.", file=sys.stderr)
        return 2

    # Resolve users (fail-fast).
    try:
        pipe_ctx, old_user, new_user = _resolve_users(
            client, args.pipe_id, args.old_user, args.new_user, args.org_id or cfg.org_id
        )
    except UserResolutionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("Hint: re-run with --list-users to see every pipe member and org user the bot can see.", file=sys.stderr)
        return 2

    if old_user.id == new_user.id:
        print("ERROR: --old-user and --new-user resolved to the same Pipefy user id.", file=sys.stderr)
        return 2

    # Warn if the new user isn't a formal pipe member — Pipefy may reject the assignment.
    member_ids = {m.id for m in pipe_ctx.get("members", [])}
    if new_user.id not in member_ids:
        print(
            f"WARNING: new user {new_user.name} <{new_user.email}> (id {new_user.id}) "
            f"is NOT listed as a pipe member of pipe {args.pipe_id}.\n"
            f"         Pipefy may reject the assignment. Add them to the pipe (any role, "
            f"including restricted) before running with --apply.",
            file=sys.stderr,
        )

    # Discover assignee fields.
    try:
        fields_meta = discover_assignee_fields(client, args.pipe_id)
    except Exception as exc:
        print(f"ERROR: could not discover pipe fields: {exc}", file=sys.stderr)
        return 2

    # Resolve --field / --all-fields / interactive selection.
    field_filter: Optional[Set[str]] = None
    if args.field:
        # Accept slug id, numeric internal_id, or label (case-insensitive, whitespace-collapsed).
        # Labels can collide (Pipefy lets multiple fields share a label); refuse ambiguous matches.
        def _norm(s: str) -> str:
            return " ".join(s.split()).strip().lower()

        label_to_ids: Dict[str, list] = {}
        internal_to_id: Dict[str, str] = {}
        for fid, meta in fields_meta.items():
            lbl = _norm(meta.get("label", ""))
            if lbl:
                label_to_ids.setdefault(lbl, []).append(fid)
            iid = str(meta.get("internal_id") or "")
            if iid:
                internal_to_id[iid] = fid

        resolved: Set[str] = set()
        unknown: list[str] = []
        ambiguous: list[tuple[str, list]] = []
        for f in args.field:
            if f in fields_meta:
                resolved.add(f)
            elif f in internal_to_id:
                resolved.add(internal_to_id[f])
            elif _norm(f) in label_to_ids:
                matches = label_to_ids[_norm(f)]
                if len(matches) == 1:
                    resolved.add(matches[0])
                else:
                    ambiguous.append((f, matches))
            else:
                unknown.append(f)

        if ambiguous:
            for raw, matches in ambiguous:
                detail = ", ".join(
                    f'{mid} (internal_id={fields_meta[mid].get("internal_id")})' for mid in matches
                )
                print(
                    f'ERROR: --field "{raw}" is ambiguous — multiple assignee fields share that label: {detail}.\n'
                    f"       Re-run using the slug id (first column above).",
                    file=sys.stderr,
                )
            return 2
        if unknown:
            available = "\n  ".join(
                f'{fid:<48} internal_id={meta.get("internal_id"):<12} label={meta.get("label")!r}'
                for fid, meta in sorted(fields_meta.items())
            ) or "(none)"
            print(
                f"ERROR: --field value(s) not found on pipe or not assignee-typed: {', '.join(unknown)}.\n"
                f"Discovered assignee fields:\n  {available}\n"
                f"Tip: pass the slug (first column), the internal_id (numeric), or quote the label.",
                file=sys.stderr,
            )
            return 2
        field_filter = resolved
    elif args.all_fields:
        field_filter = set(fields_meta.keys())
        if not field_filter and not args.include_card_level:
            print("ERROR: pipe has no assignee fields and --no-include-card-level was set; nothing to do.", file=sys.stderr)
            return 2
        if not field_filter:
            log.warning("Pipe has no assignee fields; falling back to card-level only.")
    else:
        if not sys.stdin.isatty():
            print(
                "ERROR: Field selection required: pass --field <ID> (repeatable) or --all-fields. "
                f"Discovered assignee fields: {', '.join(sorted(fields_meta)) or '(none)'}",
                file=sys.stderr,
            )
            return 2
        if not fields_meta:
            log.warning("Pipe has no assignee fields; using card-level only.")
            field_filter = set()
        else:
            chosen = _interactive_field_selection(fields_meta)
            if chosen is None:
                print("Aborted.")
                return 0
            field_filter = chosen

    excluded_phases = args.exclude_phase if args.exclude_phase else list(DEFAULT_EXCLUDED_PHASES)

    # Banner.
    print(f"Pipe:           {pipe_ctx.get('pipe_name') or '?'} (id {args.pipe_id})")
    print(f"Old user:       {old_user.name} <{old_user.email}> (id {old_user.id})")
    print(f"New user:       {new_user.name} <{new_user.email}> (id {new_user.id})")
    print(f"Excluded:       {', '.join(excluded_phases)}")
    print(f"Mode:           {'APPLY' if args.apply else 'DRY-RUN  (re-run with --apply to mutate)'}")
    print()

    # First pass: plan everything.
    plans = []
    cards_scanned = 0
    cards_in_excluded = 0
    cards_already_swapped = 0
    log.info("Scan started: paging cards from pipe %s ...", args.pipe_id)
    HEARTBEAT_EVERY = 200
    for card in iter_cards(client, args.pipe_id):
        cards_scanned += 1
        if args.limit is not None and cards_scanned > args.limit:
            cards_scanned -= 1
            break
        if args.trace_card and str(card.get("id")) == str(args.trace_card):
            import json as _json
            print(f"--- TRACE card {card.get('id')} (as yielded by iter_cards) ---", file=sys.stderr)
            print(_json.dumps(card, indent=2, default=str), file=sys.stderr)
            print(f"--- END TRACE ---", file=sys.stderr)
        plan = plan_card(
            card,
            old_id=old_user.id,
            new_id=new_user.id,
            excluded_phases=excluded_phases,
            include_card_level=args.include_card_level,
            field_filter=field_filter,
        )
        if plan.skipped_reason:
            cards_in_excluded += 1
            # Per-card skip noise — only useful at DEBUG. Big pipes can have 1000+ skips.
            log.debug("SKIP card %s (%s): %s", plan.card_id, plan.title, plan.skipped_reason)
        elif not plan.actions:
            # Could be already-swapped, or old user simply not present.
            log.debug("No-op for card %s (%s)", plan.card_id, plan.title)
            cards_already_swapped += 1
        else:
            log.debug(
                "PLAN card %s '%s' [%s] -> %d action(s)",
                plan.card_id, plan.title, plan.phase_name, len(plan.actions),
            )
            plans.append(plan)
        if cards_scanned % HEARTBEAT_EVERY == 0:
            log.info(
                "Progress: %d scanned (excluded=%d, no-op=%d, to-swap=%d)",
                cards_scanned, cards_in_excluded, cards_already_swapped, len(plans),
            )
    log.info(
        "Scan complete: %d cards scanned (excluded=%d, no-op=%d, to-swap=%d)",
        cards_scanned, cards_in_excluded, cards_already_swapped, len(plans),
    )

    card_level_changes = sum(1 for p in plans for a in p.actions if a.scope == "card")
    field_level_changes = sum(1 for p in plans for a in p.actions if a.scope.startswith("field:"))
    fields_touched = {a.field_id for p in plans for a in p.actions if a.field_id}

    print(f"Cards scanned:        {cards_scanned}")
    print(f"Cards in excluded:    {cards_in_excluded}")
    print(f"Cards already swapped:{cards_already_swapped}")
    print(f"Cards to swap:        {len(plans)}")
    print(f"   ├─ card-level:     {card_level_changes}")
    print(f"   └─ field-level:    {field_level_changes}  (across {len(fields_touched)} field(s))")

    # Preview the affected cards (always, unless explicitly suppressed).
    if not args.no_preview:
        _print_preview(
            plans,
            old_user=old_user,
            new_user=new_user,
            limit=args.preview_limit,
        )

    cards_changed = 0
    errors: List[str] = []

    report = ReportWriter(args.report)
    try:
        # Write planned rows for dry-run / unchanged ones for visibility.
        for plan in plans:
            for action in plan.actions:
                report.write(
                    card_id=plan.card_id,
                    card_url=plan.card_url,
                    title=plan.title,
                    current_phase=plan.phase_name,
                    scope=action.scope,
                    action="would_swap",
                    old_assignees=action.old_ids,
                    new_assignees=action.new_ids,
                )

        if args.apply and plans:
            needs_prompt = (not args.yes) and (
                len(plans) > args.confirm_threshold or sys.stdin.isatty()
            )
            if needs_prompt:
                if not sys.stdin.isatty():
                    print(
                        f"ERROR: {len(plans)} cards would be modified (>{args.confirm_threshold}); "
                        "pass --yes to proceed in non-interactive mode.",
                        file=sys.stderr,
                    )
                    return 2
                resp = input(f"Modify {len(plans)} cards? [y/N] ").strip().lower()
                if resp not in ("y", "yes"):
                    print("Aborted.")
                    return 0

            for plan in plans:
                results = execute_plan(client, plan, throttle_ms=args.throttle_ms)
                any_ok = False
                for action, err in results:
                    if err:
                        errors.append(f"{plan.card_url or plan.card_id}: {err}")
                        report.write(
                            card_id=plan.card_id,
                            card_url=plan.card_url,
                            title=plan.title,
                            current_phase=plan.phase_name,
                            scope=action.scope,
                            action="error",
                            old_assignees=action.old_ids,
                            new_assignees=action.new_ids,
                            error_message=err,
                        )
                    else:
                        any_ok = True
                        report.write(
                            card_id=plan.card_id,
                            card_url=plan.card_url,
                            title=plan.title,
                            current_phase=plan.phase_name,
                            scope=action.scope,
                            action="swapped",
                            old_assignees=action.old_ids,
                            new_assignees=action.new_ids,
                        )
                if any_ok:
                    cards_changed += 1
    finally:
        report.close()

    print()
    print(f"Cards changed:        {cards_changed}{'  (dry-run)' if not args.apply else ''}")
    print(f"Errors:               {len(errors)}")
    for e in errors:
        print(f"  - {e}")

    return 1 if errors else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
