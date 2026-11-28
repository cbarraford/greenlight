"""Deterministic property-based & fuzz invariant tests across critical Greenlight parsers and guards."""
import json
import random
import string
import time

import ade_common
import audit
import auto
import diag
import dwim


def test_property_cron_due_invariants():
    rng = random.Random(20261122)
    for _ in range(300):
        ts = rng.randint(1_577_836_800, 1_893_456_000)  # 2020..2030
        tm = time.localtime(ts)
        # Wildcard always matches
        assert auto.due("* * * * *", tm) is True
        # Exact minute/hour/dom/mon matches itself
        exact = f"{tm.tm_min} {tm.tm_hour} {tm.tm_mday} {tm.tm_mon} *"
        assert auto.due(exact, tm) is True
        # Sunday equivalence: dow 0 and dow 7 always agree
        assert auto.due("* * * * 0", tm) == auto.due("* * * * 7", tm)
        # Step */1 always matches
        assert auto.due("*/1 */1 * * *", tm) is True


def test_property_ansi_plain_and_draft_never_crash_or_leak_escapes():
    rng = random.Random(20261123)
    escapes = ["\x1b[0m", "\x1b[1m", "\x1b[2m", "\x1b[32m", "\x1b[38;5;208m", "\x1b]0;title\x07", "\r"]
    alphabet = string.ascii_letters + string.digits + " ❯>─\t\xa0"
    for _ in range(250):
        parts = []
        for _ in range(rng.randint(1, 12)):
            if rng.random() < 0.4:
                parts.append(rng.choice(escapes))
            else:
                parts.append("".join(rng.choices(alphabet, k=rng.randint(0, 10))))
        raw = "".join(parts)
        cleaned = ade_common.plain(raw)
        assert "\x1b[" not in cleaned
        assert "\x1b]" not in cleaned
        # Dim-only ghost after prompt must never be classified as a user draft
        ghost = f"\x1b[1m❯\x1b[0m \x1b[2m{''.join(rng.choices(string.ascii_lowercase, k=8))}\x1b[0m"
        assert ade_common.draft([ade_common.plain(ghost, ghost=False)]) == ""


def test_property_diag_clean_never_leaks_free_text_or_nan():
    rng = random.Random(20261124)
    secrets = [
        "glpat" + "-1234567890abcdefghij",
        "sk-ant" + "-api03-secretvalue1234567890",
        "/U" + "sers/alice/private/secret-repo",
        "Merge my confidential branch",
    ]
    for _ in range(150):
        secret = rng.choice(secrets)
        payload = {
            "kind": "ping",
            "secs": rng.choice([0.1, 2.5, float("nan"), float("inf"), -float("inf")]),
            "ok": rng.choice([True, False]),
            "msg": secret,
            "title": secret,
            "path": secret,
            f"key_{secret}": 123,
            "ws_h": diag.h(secret),
        }
        cleaned = diag.clean(payload)
        serialized = json.dumps(cleaned)
        assert secret not in serialized
        assert "NaN" not in serialized and "Infinity" not in serialized


def test_property_audit_secrets_resists_line_separator_obfuscation():
    seps = ["\n", "\r", "\r\n", "\u2028", "\u2029"]
    tokens = [
        "glpat" + "-1234567890abcdefghij",
        "ghp" + "_1234567890abcdefghijklmnopqrstuvwxyz",
        "AKIA" + "1234567890ABCDEF",
    ]
    for sep in seps:
        for tok in tokens:
            diff = f"diff --git a/x.py b/x.py\n@@ -0,0 +1,2 @@\n+safe{sep}+{tok}\n"
            hits = audit.secrets(diff, diff=True)
            assert hits, f"audit.secrets missed token {tok!r} with separator {sep!r}"


def test_property_dwim_never_crashes_or_auto_merges_arbitrary_text():
    rng = random.Random(20261125)
    pucks = [
        {"name": "auth-fix", "path": "/w/auth-fix", "repo": "widgets", "state": "needs", "mr": {"ref": "!42", "title": "Auth fix", "pipeline": "failed"}, "agent": {"state": "idle"}},
        {"name": "billing", "path": "/w/billing", "repo": "widgets", "state": "working", "mr": {"ref": "#17", "title": "Billing", "pipeline": "success"}, "agent": {"state": "working"}},
    ]
    verbs = ["merge", "delete", "rm", "park", "hold", "unpark", "fix ci", "what is", "drop table", ""]
    targets = ["!42", "#17", "auth-fix", "billing", "all", "everything", "red ones", '"><img src=x>', ""]
    for _ in range(150):
        utterance = f"{rng.choice(verbs)} {rng.choice(targets)}".strip()
        res = dwim.plan({"text": utterance, "pucks": pucks})
        assert isinstance(res, dict)
        # Destructive merge/delete must NEVER be planned as a direct DWIM mutation
        if utterance.startswith(("merge ", "delete ", "rm ")) or utterance in ("merge", "delete", "rm"):
            assert res.get("kind") in ("fallback", "none", None, "ambiguous", "clarify") or not res.get("actions")
