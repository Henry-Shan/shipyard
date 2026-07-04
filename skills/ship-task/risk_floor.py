#!/usr/bin/env python3
"""
risk_floor.py — deterministic ship-task tier FLOOR for the risk-override rule.

ship-task Step 1 and manager Phase 5 share one rule: a task that plausibly touches
a high-blast-radius area is MINIMUM Tier 2, no matter how small it looks textually.
That is a keyword/path check, not a judgment — so it lives here as code, a backstop
the model can't forget to apply.

This RAISES a floor, never lowers it: the model still picks the actual tier (0..3);
the floor only guarantees the safety-critical categories never get the fast path.
It is one of TWO independent checks (the model applies the same rule in prose), so
it is tuned for PRECISION — catch the unambiguous risk terms, and let the model's
judgment catch unusual phrasings. A false positive only costs extra rigor (safe); a
miss is covered by the model's own pass.

Universal categories (from the skill): auth/JWT/middleware · rbac/roles/permissions ·
DB migrations · multi-tenancy · .env/secrets · CORS · .github/workflows/deploy ·
payment/billing · deletion/destructive ops. Project-specific terms are appended
from config.json's `risk_keywords_extra` (each keyword matched literally,
case-insensitive).

Usage:
  python3 risk_floor.py "<task description>"             # arg
  echo "<task description>" | python3 risk_floor.py      # stdin
  python3 risk_floor.py --paths server/auth/x.go …       # also scan file paths
  python3 risk_floor.py --selftest                       # run the assert battery
Output (stdout, JSON):
  {"floor": 2, "matched": ["auth/JWT", "db/migrations"], "reason": "…"}
  {"floor": 0, "matched": [], "reason": "no risk-override category matched"}
Exit code = the floor (0 or 2), for easy shell gating.
"""
import json
import os
import re
import sys

# (category label, compiled regex). Patterns are deliberately specific: each token
# is one that reliably signals the risk category across projects — and common
# false-friends are excluded (e.g. bare "checkout" = a domain action, not
# payment; bare "remove"/"token"/"grant"/"price" are too generic, so excluded).
_CATEGORIES = [
    ("auth/JWT",
     r"\bauth\b|authenticat|authoriz|\bjwt\b|\boauth\b|\bsso\b|\bmiddleware\b|\blog ?in\b|\blogout\b|\bsign ?in\b"),
    ("rbac/roles/permissions",
     r"\brbac\b|\broles?\b|permission|access control|\bprivilege"),
    ("db/migrations",
     r"\bmigrations?\b|\bmigrate\b|\balter table\b|\bdrop table\b|\brls\b|row[- ]level security"),
    ("multi-tenancy",
     r"multi[- ]?tenan|\btenant\b|data isolation|cross[- ]tenant"),
    ("secrets/env",
     r"\.env\b|\bsecret\b|\bcredential|\bapi[_ ]?key\b|\baccess[_ ]?key\b|\bbearer\b|private key|service[_ ]?key"),
    ("CORS",
     r"\bcors\b|cross[- ]origin"),
    ("ci/deploy",
     r"\.github/workflows|github actions|\bci/cd\b|\bdeploy(ment|s)?\b|\bdockerfile\b"),
    ("payment/billing",
     r"\bbilling\b|\binvoice|\bpayment|\bstripe\b|\bcharge\b.*\b(card|account)\b|usage charge"),
    ("destructive/deletion",
     r"\bdelet|\bdestroy\b|\bpurge\b|\bwipe\b|\bhard[- ]delete|\bbulk[- ]delete|\birreversible\b|\btruncate\b"),
]


def _extra_category():
    """Project-specific keywords from config.json (`risk_keywords_extra`) as one
    extra category. Best-effort: standalone use without the manager skill (or
    with no config) just gets the universal set."""
    try:
        sys.path.insert(0, os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "manager")))
        import skill_config
        extras = skill_config.get("risk_keywords_extra") or []
    except Exception:
        return None
    extras = [str(k).strip() for k in extras if str(k).strip()]
    if not extras:
        return None
    return ("project-extra", "|".join(re.escape(k) for k in extras))


_ALL = _CATEGORIES + ([c] if (c := _extra_category()) else [])
_COMPILED = [(label, re.compile(pat, re.IGNORECASE)) for label, pat in _ALL]


def assess(text, paths=None):
    """Return (floor, matched_labels). floor is 2 if any category matches, else 0."""
    blob = text or ""
    if paths:
        blob = blob + "\n" + "\n".join(paths)
    matched = [label for label, rx in _COMPILED if rx.search(blob)]
    # De-dup preserving order.
    seen, ordered = set(), []
    for m in matched:
        if m not in seen:
            seen.add(m)
            ordered.append(m)
    return (2 if ordered else 0), ordered


def _result(text, paths=None):
    floor, matched = assess(text, paths)
    reason = (
        "risk-override category matched -> minimum Tier 2: " + ", ".join(matched)
        if matched else
        "no risk-override category matched (floor does not raise the tier)"
    )
    return {"floor": floor, "matched": matched, "reason": reason}


def _selftest():
    # (text, expected_floor, must_include_label_or_None)
    cases = [
        # --- must FLOOR to 2 ---
        ("Add a button to delete a workspace and cascade-remove its data", 2, "destructive/deletion"),
        ("Fix JWT refresh so the auth token rotates before expiry", 2, "auth/JWT"),
        ("Give team leads an rbac role with invoices:view permission", 2, "rbac/roles/permissions"),
        ("Add a DB migration adding users.deleted_at column", 2, "db/migrations"),
        ("Scope the new endpoint by tenant for multi-tenancy isolation", 2, "multi-tenancy"),
        ("Rotate the API service key and read it from .env", 2, "secrets/env"),
        ("Loosen CORS to allow the new preview origin", 2, "CORS"),
        ("Update .github/workflows/ci.yml to add a deploy step", 2, "ci/deploy"),
        ("Generate an invoice and charge the customer account for usage", 2, "payment/billing"),
        ("Bulk-delete archived order requests permanently", 2, "destructive/deletion"),
        ("Add login throttling middleware", 2, "auth/JWT"),
        # --- must NOT floor (no risk category) ---
        ("Change the dashboard headline copy from 'Apps' to 'App'", 0, None),
        ("Add a CSV export button to the inventory table", 0, None),
        ("Fix the sidebar hamburger spacing on mobile", 0, None),
        ("Let a member check out a tool from the tools page", 0, None),  # 'checkout' must NOT match
        ("Show unit price next to each order line item", 0, None),        # 'price' must NOT match
        ("Tag an order request with a grant for budget tracking", 0, None),  # 'grant' (funding) must NOT match
        ("Remove the extra padding under the section header", 0, None),   # bare 'remove' must NOT match
    ]
    failures = []
    for text, exp_floor, exp_label in cases:
        floor, matched = assess(text)
        if floor != exp_floor:
            failures.append(f"floor {floor}!={exp_floor} for: {text!r} (matched={matched})")
        elif exp_label and exp_label not in matched:
            failures.append(f"missing label {exp_label!r} for: {text!r} (matched={matched})")
    if failures:
        print("SELFTEST FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print(f"SELFTEST PASSED: {len(cases)} cases")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        sys.exit(_selftest())
    paths = []
    if "--paths" in args:
        i = args.index("--paths")
        paths = args[i + 1:]
        args = args[:i]
    text = " ".join(args).strip()
    if not text:
        text = sys.stdin.read()
    res = _result(text, paths)
    print(json.dumps(res))
    sys.exit(res["floor"])
