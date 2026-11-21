"""python3 test_audit.py: the semantic contracts and the outbound scorer, on a fake decide.py (no network, no model)."""
import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import os, tempfile; os.environ.update(GREENLIGHT_HOME=tempfile.mkdtemp(), GREENLIGHT_ME="octo")  # before greenlight: never the real ~/.greenlight or glab
import contextlib, io, json, subprocess, time, types
from pathlib import Path
from unittest import mock
import audit
import greenlight

# ---- a fake decide.py speaking the shared contract; `ans` maps a substring of the question/rubric to its answer
calls = []
ans = {}
fake = types.ModuleType("decide")


def _pick(text, default):
    return next((v for k, v in ans.items() if k in text), default)


def _bool(state, question, *, context=""):
    calls.append(("bool", question, context))
    return dict(_pick(question, {"value": True, "prob": 0.95, "provider": "jev"}))


def _score(state, rubric, *, context=""):
    calls.append(("score", rubric, context))
    v = _pick(f"{rubric}\n{context}", 0.9)
    return v if isinstance(v, dict) else {"score": v, "provider": "jev"}


def _batch(state, questions):
    calls.append(("batch", sorted(questions)))
    return {k: (fake.bool(state, q["question"], context=q["context"]) if q["type"] == "bool"
                else fake.score(state, q["rubric"], context=q.get("question", "") + "\n\n" + q["context"]))
            for k, q in questions.items()}


fake.bool, fake.score, fake.batch = _bool, _score, _batch
sys.modules["decide"] = fake
private_state = {"cwd": "/home/alice/ghp_abcdefghijklmnopqrstuvwxyz0123456789", "nested": ["/Users/bob/private"]}
seen_state = []
with mock.patch.object(fake, "batch", side_effect=lambda state, qs: (seen_state.append(state), {k: {"value": True, "prob": .99, "provider": "remote"} for k in qs})[1]):
    audit.ask(private_state, {"safe": {"type": "bool", "question": "Safe?", "context": "ok"}})
assert "alice" not in repr(seen_state) and "bob" not in repr(seen_state) and "abcdefghijklmnopqrstuvwxyz" not in repr(seen_state)
Q = {k: v[1] for k, v in audit.CONTRACTS.items()}
DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
-x = 1
+x = 2
"""

# ---- p_true: decide's prob is P(yes), and yes means the contract holds
assert audit.p_true({"value": True, "prob": 0.9}) == 0.9 and audit.p_true({"value": False, "prob": 0.1}) == 0.1  # prob is P(yes)
assert audit.p_true({"value": True, "prob": 0.05}) == 0.05  # decide's contract: prob, not value, is the answer
assert audit.p_true({"value": True, "prob": 1.7}) == 1.0
try: audit.p_true({"prob": float("nan")})
except ValueError: pass
else: assert False, "NaN may not become a safe probability"
with mock.patch.object(fake, "batch", return_value={"safe": {"value": True, "prob": float("nan"), "provider": "jev"}}), \
     mock.patch.object(fake, "bool", return_value={"value": True, "prob": float("inf"), "provider": "jev"}):
    try: audit.ask({}, {"safe": {"type": "bool", "question": "Safe?", "context": "ok"}})
    except ValueError as e: assert "nonfinite" in str(e)
    else: assert False, "malformed batch and fallback must fail closed"

# ---- every contract holds: one batch, all four checks asked, ok
res = audit.contracts({}, audit.MERGE_CHECKS, goal="bump x", diff=DIFF)
assert res["ok"] and res["failed"] == [] and calls[0] == ("batch", ["diff_matches_goal", "no_secrets_or_pii", "tests_not_weakened"]), (res, calls)
assert "Goal:\nbump x" in calls[1][2] and "+x = 2" in calls[1][2]
# a broken contract above the threshold blocks, named exactly; below it doesn't; a guess (local fallback) never does
ans[Q["diff_matches_goal"]] = {"value": False, "prob": 0.08, "provider": "jev"}
res = audit.contracts({}, audit.MERGE_CHECKS, goal="bump x", diff=DIFF)
assert not res["ok"] and [(f["contract"], f["check"], f["p_broken"]) for f in res["failed"]] == [("NoScopeCreep", "diff_matches_goal", 0.92)], res
assert audit.lines(res) == [f"BLOCKED NoScopeCreep (diff_matches_goal, p=0.92, jev): {Q['diff_matches_goal']}"]
ans[Q["diff_matches_goal"]] = {"value": False, "prob": 0.3, "provider": "jev"}
assert audit.contracts({}, audit.MERGE_CHECKS, diff=DIFF)["ok"]  # 0.7 < 0.8
greenlight.CONFIG["audit"] = {"threshold": 0.6}
assert not audit.contracts({}, audit.MERGE_CHECKS, diff=DIFF)["ok"]  # config.json audit.threshold
del greenlight.CONFIG["audit"]
ans[Q["diff_matches_goal"]] = {"value": False, "prob": 0.01, "provider": "local"}
assert not audit.contracts({}, audit.MERGE_CHECKS, diff=DIFF)["ok"]  # a guess cannot authorize an unattended action
ans.clear()

# ---- a batch that doesn't take the question shape, or leaves answers out: the per-call API fills them, concurrently
calls.clear()
fake.batch = lambda s, q: (_ for _ in ()).throw(TypeError("unknown kind"))
assert audit.contracts({}, ("diff_matches_goal", "tests_not_weakened"), diff=DIFF)["ok"] and [c[0] for c in calls] == ["bool", "bool"]
calls.clear()
fake.batch = lambda s, q: {"diff_matches_goal": {"value": True, "prob": 0.9, "provider": "jev"}, "tests_not_weakened": {"oops": 1}}
assert audit.contracts({}, ("diff_matches_goal", "tests_not_weakened"), diff=DIFF)["ok"] and [c[1] for c in calls] == [Q["tests_not_weakened"]]
fake.batch = _batch
# decide itself failing is an error, not a pass
with mock.patch.object(fake, "batch", side_effect=RuntimeError("jev down")), mock.patch.object(fake, "bool", side_effect=RuntimeError("jev down")):
    try:
        audit.contracts({}, ("diff_matches_goal",), diff=DIFF)
        raise AssertionError("should raise")
    except RuntimeError:
        pass
real_import = __import__
with mock.patch("builtins.__import__", side_effect=lambda name, *a, **kw: (_ for _ in ()).throw(ModuleNotFoundError("missing dependency", name="broken_dependency")) if name == "decide" else real_import(name, *a, **kw)):
    try:
        audit._decide()
        raise AssertionError("a broken decide import must fail")
    except ModuleNotFoundError as e:
        assert e.name == "broken_dependency"
# no decide.py at all (before system-one-decide lands): the default holds the audit
with mock.patch.dict(sys.modules, {"decide": None}):
    r = audit.contracts({}, audit.MERGE_CHECKS, diff=DIFF)
    assert not r["ok"] and r["unverified"] and {c["provider"] for c in r["checks"].values()} == {"unavailable"}, r
print("contracts ok")

# ---- SafeToPublish: a credential shape in what goes out blocks whatever the model says; a removed one is fine; masked in the evidence
tok = "ghp_" + "a1B2c3D4" * 5
leak = DIFF + f"+TOKEN = '{tok}'\n"
ans[Q["no_secrets_or_pii"]] = {"value": True, "prob": 0.99, "provider": "local"}
r = audit.contracts({}, ("no_secrets_or_pii",), diff=leak)
assert not r["ok"] and r["failed"][0]["provider"] == "scan" and "a GitHub token" in r["failed"][0]["evidence"] and tok not in json.dumps(r), r
ans[Q["no_secrets_or_pii"]] = {"value": True, "prob": 0.99, "provider": "jev"}
assert audit.contracts({}, ("no_secrets_or_pii",), diff=DIFF + f"-TOKEN = '{tok}'\n")["ok"]  # deleting a secret publishes nothing new
assert not audit.contracts({}, ("no_secrets_or_pii",), outbound=f"<p>{tok}</p>")["ok"]  # report.html: every line counts
assert not audit.contracts({}, ("no_secrets_or_pii",), outbound="-----BEGIN " + "OPENSSH PRIVATE KEY-----")["ok"]  # split: this file is a diff the scan reads too
path_token = "ghp_" + "A" * 30
path_diff = f"diff --git a/{path_token} b/{path_token}\nnew file mode 100644\n--- /dev/null\n+++ b/{path_token}\n@@ -0,0 +1 @@\n+ordinary content\n"
assert not audit.contracts({}, ("no_secrets_or_pii",), diff=path_diff)["ok"]  # published filenames are outbound too
removed_path = f"diff --git a/{path_token} b/{path_token}\ndeleted file mode 100644\n--- a/{path_token}\n+++ /dev/null\n@@ -1 +0,0 @@\n-old content\n"
renamed_path = f"diff --git a/{path_token} b/clean.txt\nrename from {path_token}\nrename to clean.txt\n"
assert audit.contracts({}, ("no_secrets_or_pii",), diff=removed_path)["ok"]
assert audit.contracts({}, ("no_secrets_or_pii",), diff=renamed_path)["ok"]
empty_spaced_secret_path = "credential file " + tok + ".dat"
empty_spaced_diff = f"diff --git a/{empty_spaced_secret_path} b/{empty_spaced_secret_path}\nnew file mode 100644\nindex 0000000..e69de29\n"
assert not audit.contracts({}, ("no_secrets_or_pii",), diff=empty_spaced_diff)["ok"]
plus_diff = "diff --git a/count.cpp b/count.cpp\n--- a/count.cpp\n+++ b/count.cpp\n@@ -0,0 +1 @@\n+++counter; // " + path_token + "\n"
assert not audit.contracts({}, ("no_secrets_or_pii",), diff=plus_diff)["ok"]  # added content beginning ++ is not a +++ header
plus_space_diff = "diff --git a/count.cpp b/count.cpp\n--- a/count.cpp\n+++ b/count.cpp\n@@ -0,0 +1 @@\n+++ " + path_token + "\n"
assert not audit.contracts({}, ("no_secrets_or_pii",), diff=plus_space_diff)["ok"]  # hunk state distinguishes +++ text from headers
assert audit.contracts({}, ("no_secrets_or_pii",), outbound='api_key = os.environ["X"]  # ghp_ short')["ok"]  # no false hit on names
# masked on the whole line: two tokens, and one past the 120th char, never show
two = "x" * 130 + f" {tok} and glpat-" + "Z9" * 12
h = audit.secrets(two)
assert [w for w, _ in h] == ["a GitHub token", "a GitLab token"] and all(tok[4:12] not in l and "Z9Z9Z9" not in l for _, l in h), h
two = tok + " glpat-" + "Z9" * 12
assert all(tok not in line and "Z9" * 12 not in line for _, line in audit.secrets(two))  # neither credential leaks in the other's evidence
embedded = "glpat-" + "A" * 10 + "test" + "B" * 12
assert audit.secrets(embedded)  # 'test' inside a token does not make it an example
# a comment that quotes a hunk is still plain text: every line of it is read
assert audit.secrets(f"Quoting:\n@@ -1 +1 @@\n token {tok}") and not audit.secrets(f"@@ -1 +1 @@\n token {tok}", diff=True)
# docs' and fixtures' example values aren't credentials (they'd stall every unattended run on a repo that has them)
assert not audit.secrets('+key = "AKIAIOSFODNN7EXAMPLE"\n+password = "test-password-123"', diff=True)
assert audit.secrets('password = "' + 'hunter2' * 3 + '"')
url = "postgres://user:password" + "@host/db"
with mock.patch.dict(sys.modules, {"decide": None}):
    assert not audit.contracts({}, ("no_secrets_or_pii",), diff="diff --git a/env b/env\n@@ -0,0 +1 @@\n+DATABASE_URL=" + url + "\n")["ok"]
assert not audit.secrets("postgres://host/db") and not audit.secrets("DATABASE_URL = os.environ['DATABASE_URL']")
skip_token = "glpat-" + "A" * 24
skip_diff = "diff --git a/tests/test_x.py b/tests/test_x.py\n--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@ -0,0 +1 @@\n+" + "@pytest.mark.skip(reason=\"" + skip_token + "\")\n"
ans[Q["tests_not_weakened"]] = {"value": False, "prob": 0.05, "provider": "jev"}
redacted = audit.contracts({}, ("tests_not_weakened", "no_secrets_or_pii"), diff=skip_diff)
assert skip_token not in json.dumps(redacted) and skip_token not in "\n".join(audit.lines(redacted)), redacted
del ans[Q["tests_not_weakened"]]
calls.clear()
assert audit.contracts({}, audit.MERGE_CHECKS, diff=DIFF + f"-old = '{tok}'\n")["ok"]
assert tok not in repr(calls)  # removed secrets must not be sent to the model either
calls.clear()
pem_body = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="
pem_open, pem_close = "-----BEGIN " + "PRIVATE KEY-----", "-----END " + "PRIVATE KEY-----"
pem_diff = "diff --git a/key.pem b/key.pem\n@@ -0,0 +1,3 @@\n+" + pem_open + "\n+" + pem_body + "\n+" + pem_close + "\n"
r = audit.contracts({}, ("no_secrets_or_pii",), diff=pem_diff)
assert not r["ok"] and pem_body not in repr(calls) and pem_body not in json.dumps(r), r
calls.clear()
large_pem = "diff --git a/key.pem b/key.pem\n@@ -0,0 +1,3 @@\n+" + pem_open + "\n+" + (pem_body * 1500) + "\n+" + pem_close + "\n"
r = audit.contracts({}, audit.MERGE_CHECKS, diff=large_pem)
assert not r["ok"] and pem_body not in repr(calls) and len(large_pem) > audit.MAXC, r
calls.clear()
unclosed_removed = "diff --git a/key.pem b/key.pem\n@@ -1 +0,0 @@\n-" + pem_open + "\n" + \
    "diff --git a/tests/test_x.py b/tests/test_x.py\n@@ -1 +1 @@\n-assert result == expected\n+" + "@pytest.mark.skip(reason='broken')\n"
r = audit.contracts({}, audit.MERGE_CHECKS, diff=unclosed_removed)
assert r["ok"] and "@pytest.mark.skip" in repr(calls) and pem_open not in repr(calls), r
calls.clear()
encrypted_removed = ("diff --git a/key.pem b/key.pem\n@@ -5 +0,0 @@\n-" + pem_open + "\n-Proc-Type: 4,ENCRYPTED\n"
                     "-DEK-Info: AES-256-CBC,0123456789ABCDEF\n-" + pem_body + "\n-" + pem_close + "\n"
                     "diff --git a/tests/test_x.py b/tests/test_x.py\n@@ -1 +1 @@\n-assert result == expected\n+" + "@pytest.mark.skip(reason='broken')\n")
r = audit.contracts({}, audit.MERGE_CHECKS, diff=encrypted_removed)
assert r["ok"] and pem_body not in repr(calls) and "@pytest.mark.skip" in repr(calls), r
calls.clear()
added_unclosed = "diff --git a/key.pem b/key.pem\n@@ -0,0 +1 @@\n+" + pem_open + "\n"
r = audit.contracts({}, audit.MERGE_CHECKS, diff=added_unclosed)
assert not r["ok"] and "unclosed private-key marker" in r["error"] and not calls
calls.clear()
audit.score_outbound({}, [{"body": f"Look at {tok}"}], diff=DIFF)
assert tok not in repr(calls)  # blocked comments are redacted before the scorer calls decide
calls.clear()
removed_email = "jane.private@" + "private-mail.test"
removed_pii = DIFF + f"-{removed_email}\n+anonymous\n"
assert audit.contracts({}, audit.MERGE_CHECKS, diff=removed_pii)["ok"]
audit.score_outbound({}, [{"body": "A concrete review point"}], diff=removed_pii)
assert removed_email not in repr(calls)  # full-diff model contexts omit removed PII
r = audit.contracts({}, ("no_secrets_or_pii",), diff=DIFF + "+path = '" + r"C:\Users" + r"\alice\Documents\report.txt" + "'\n")
assert not r["ok"] and r["failed"][0]["contract"] == "SafeToPublish"
assert "alice" not in repr(calls)  # recognizable Windows home usernames are masked before model calls
# the model's own verdict blocks too when it's sure (an email, say, that no regex knows)
ans[Q["no_secrets_or_pii"]] = {"value": False, "prob": 0.1, "provider": "jev"}
r = audit.contracts({}, ("no_secrets_or_pii",), outbound="mail jane.doe@" + "private-mail.test")
assert [f["contract"] for f in r["failed"]] == ["SafeToPublish"] and r["failed"][0]["provider"] == "scan"
ans.clear()
assert audit.contracts({}, ("no_secrets_or_pii",), outbound="mail alice@" + "example.com")["ok"]
assert audit.secrets("alice@" + "example.com and bob@" + "private-mail.test") == [("an email address", "alice@example.com and [email redacted]")]
ans.clear()
print("secrets ok")

# ---- GenuineFix gets the test files' facts as evidence
weak = """diff --git a/tests/test_x.py b/tests/test_x.py
--- a/tests/test_x.py
+++ b/tests/test_x.py
@@ -1,3 +1,3 @@
-    assert f(2) == 4
-    assert f(3) == 9
+    @unittest.skip("flaky")
+    assert f(2)
"""
sig = audit.test_signals(weak)
assert sig.startswith("Test files: 2 assertion lines removed, 1 added.") and 'tests/test_x.py: @unittest.skip("flaky")' in sig, sig
assert audit.test_signals(DIFF) == ""
gone = "diff --git a/tests/test_y.py b/tests/test_y.py\ndeleted file mode 100644\n--- a/tests/test_y.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def test_y():\n-    assert y()\n"
assert audit.test_signals(gone) == "Test files: 1 assertion lines removed, 0 added."  # deleting the whole test file counts
sql = "diff --git a/tests/q.sql b/tests/q.sql\n--- a/tests/q.sql\n+++ b/tests/q.sql\n@@ -1,2 +1,1 @@\n--- assert rows\n-select 1;\n"
assert audit.test_signals(sql) == "Test files: 1 assertion lines removed, 0 added."  # a removed "-- ..." line isn't a header
calls.clear()
ans[Q["tests_not_weakened"]] = {"value": False, "prob": 0.05, "provider": "jev"}
r = audit.contracts({}, ("tests_not_weakened",), diff=weak)
assert r["failed"][0]["contract"] == "GenuineFix" and r["failed"][0]["evidence"] == sig and sig in calls[1][2]
assert "Evidence: Test files: 2 assertion" in audit.lines(r)[0]
ans.clear()
# NoBrushOff: one question per reply, each failing on its own
ans["Reply:\nThanks, will do later"] = {"value": False, "prob": 0.1, "provider": "jev"}
fake.bool = lambda s, q, *, context="": _bool(s, context if "Reply:" in context else q, context=context)
r = audit.contracts({}, ("thread_truly_addressed",), diff=DIFF, replies=[{"thread": "x is wrong", "body": "Fixed in abc: x = 2"},
                                                                       {"thread": "y?", "body": "Thanks, will do later"}])
assert [f["check"] for f in r["failed"]] == ["thread_truly_addressed#1"] and r["failed"][0]["contract"] == "NoBrushOff", r
fake.bool = _bool
ans.clear()
# a diff longer than one part is asked in parts, and a broken part blocks; past MAXPARTS it's an error, never a pass on its head
big = "".join(f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n@@ -1 +1 @@\n" + "+y = 1\n" * 30 for i in range(40))
with mock.patch.object(audit, "MAXC", 2000):
    ps = audit.parts(big)
    assert len(ps) > 1 and "".join(ps) == big and all(len(p) <= 2000 and p.startswith("diff --git") for p in ps), [len(p) for p in ps]
    one = "diff --git a/g b/g\n" + "+z\n" * 1500  # one file bigger than a part: cut at lines
    assert "".join(audit.parts(one)) == one and all(p.endswith("\n") for p in audit.parts(one))
    calls.clear()
    fake.bool = lambda s, q, *, context="": _bool(s, q + context, context=context)
    ans["part 2 of"] = {"value": False, "prob": 0.05, "provider": "jev"}
    r = audit.contracts({}, ("diff_matches_goal",), diff=big)
    assert [f["check"] for f in r["failed"]] == ["diff_matches_goal@2"] and r["failed"][0]["contract"] == "NoScopeCreep", r
    assert len(r["checks"]) == len(ps)
    ans.clear(); fake.bool = _bool
    with mock.patch.object(audit, "MAXPARTS", 2):
        try:
            audit.contracts({}, ("diff_matches_goal",), diff=big)
            raise AssertionError("should raise")
        except ValueError as e:
            assert "too large to audit" in str(e)
print("tests/threads ok")

# ---- the outbound scorer: post only high precision, keep medium as drafts, drop weak nits
cm = [{"body": "STRONG: off-by-one at a.py:3 drops the last row when n=0", "path": "a.py", "line": 3},
      {"body": "MEDIUM: this might race", "path": "a.py"},
      {"body": "WEAK: rename x to count"},
      {"body": f"STRONG but leaks {tok}"}]
ans.update({"STRONG": 0.9, "MEDIUM": 0.55, "WEAK": 0.1})
o = audit.score_outbound({}, cm, diff=DIFF)
assert [c["body"][:6] for c in o["post"]] == ["STRONG"] and o["post"][0]["path"] == "a.py" and o["post"][0]["mean"] == 0.9, o
assert [c["body"][:6] for c in o["draft"]] == ["MEDIUM", "[redac"] and o["draft"][1]["why"] == "broke SafeToPublish" and tok not in json.dumps(o), o
assert [c["body"][:4] for c in o["drop"]] == ["WEAK"] and o["failed"] == [{"comment": 3, "contract": "SafeToPublish"}]
path_comment = {"body": "A concrete failing case", "path": "src/" + tok + ".py", "line": 1}
held = audit.score_outbound({}, [path_comment], diff=DIFF)
assert not held["post"] and held["failed"] == [{"comment": 0, "contract": "SafeToPublish"}] and tok not in json.dumps(held), held
calls.clear()
private_mail = "jane.private@" + "private-mail.test"
held = audit.score_outbound({}, [{"body": "Reply to " + private_mail, "thread": "Also see " + private_mail}], kind="reply", diff=DIFF)
assert not held["post"] and held["failed"] == [{"comment": 0, "contract": "SafeToPublish"}]
assert private_mail not in repr(calls) and private_mail not in json.dumps(held)
late = "diff --git a/early.py b/early.py\n@@ -0,0 +1 @@\n+" + "x" * 22000 + "\n"
late += "diff --git a/late.py b/late.py\n@@ -0,0 +1 @@\n+LATE_SENTINEL = 1\n"
assert "LATE_SENTINEL = 1" in audit.comment_diff(late, "late.py", 1)
big_file = "diff --git a/big.py b/big.py\n@@ -0,0 +1,3000 @@\n" + "\n".join(f"+row_{i} = {i}" for i in range(1, 3000)) + "\n+TAIL_SENTINEL = 3000\n"
assert "TAIL_SENTINEL = 3000" in audit.comment_diff(big_file, "big.py", 3000)
calls.clear()
audit.score_outbound({}, [{"body": "Late file has wrong sentinel", "path": "late.py", "line": 1}], diff=late)
assert any("LATE_SENTINEL = 1" in c[2] for c in calls if c[0] == "score"), calls
assert set(o["post"][0]["scores"]) == {"severity", "evidence", "non_nitpick"}
ans.clear()
ans.update({audit.RUBRICS["severity"][1][0]: 0.3, audit.RUBRICS["evidence"][1][0]: 1.0,
            audit.RUBRICS["non_nitpick"][1][0]: 1.0})
o = audit.score_outbound({}, [{"body": "LOW: speculative concern", "path": "a.py"}], diff=DIFF)
assert not o["post"] and o["draft"][0]["mean"] > 0.75, o  # a high mean cannot hide low severity
ans.clear()
sc = [c for c in calls if c[0] == "score"]
assert all(isinstance(c[1], list) and len(c[1]) == 5 for c in sc) and any(c[2].startswith(audit.RUBRICS["severity"][0]) for c in sc)  # levels, worst to best, and what's rated
# strong but unshown (evidence low) waits as a draft; a guess from the fallback is never posted
ans.clear()
ans.update({audit.RUBRICS["evidence"][1][0]: 0.3, "STRONG": 1.0})
o = audit.score_outbound({}, cm[:1])
assert not o["post"], o  # mean (1+0.3+1)/3 = 0.77 but evidence 0.3 < draft 0.45
ans.clear()
ans["STRONG"] = {"score": 0.95, "provider": "local"}
o = audit.score_outbound({}, cm[:1])
assert not o["post"] and o["draft"][0]["why"] == "a guess (no model answered)", o
ans.clear()
# a reply: NoBrushOff and evidence; a brush-off is held as a draft (not posted, not resolved)
ans[Q["thread_truly_addressed"]] = {"value": False, "prob": 0.1, "provider": "jev"}
o = audit.score_outbound({}, [{"thread": "why?", "body": "Good point, thanks!"}], kind="reply")
assert o["draft"][0]["why"] == "broke NoBrushOff" and set(o["draft"][0]["scores"]) == {"evidence"} and not o["post"], o
ans.clear()
o = audit.score_outbound({}, [{"thread": "why?", "body": "Fixed in 1a2b: the guard now covers n=0, test_n0 added"}], kind="reply")
assert len(o["post"]) == 1, o
ans[Q["thread_truly_addressed"]] = {"value": True, "prob": 0.5, "provider": "jev"}
o = audit.score_outbound({}, [{"thread": "why?", "body": "Fixed in 1a2b: the guard now covers n=0, test_n0 added"}], kind="reply")
assert not o["post"] and o["draft"], o  # an uncertain real-model NoBrushOff answer cannot resolve a thread
ans[Q["thread_truly_addressed"]] = {"value": True, "prob": 0.5, "provider": "local"}
o = audit.score_outbound({}, [{"thread": "why?", "body": "Fixed in 1a2b: the guard now covers n=0, test_n0 added"}], kind="reply")
assert not o["post"] and o["draft"][0]["why"] == "a guess (no model answered)", o
ans.clear()
o = audit.score_outbound({}, [{"body": "Fixed in 1a2b: the guard now covers n=0, test_n0 added"}], kind="reply")
assert not o["post"] and o["draft"][0]["why"] == "broke NoBrushOff", o  # no thread given: nothing to check it against
print("outbound ok")

# ---- the merge audit: the branch's diff at that head, cached per head; an error blocks and is retried later
wt = tempfile.mkdtemp()
g = lambda *a: subprocess.run(["git", "-C", wt, *a], check=True, capture_output=True, text=True).stdout.strip()
g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
Path(wt, "app.py").write_text("x = 1\n"); g("add", "."); g("commit", "-qm", "base")
g("update-ref", "refs/remotes/origin/main", "HEAD")
g("checkout", "-qb", "feat"); Path(wt, "app.py").write_text("x = 2\n"); g("commit", "-qam", "bump x")
sha = g("rev-parse", "HEAD")
g("checkout", "-qb", "harmless", "main"); Path(wt, "app.py").write_text("x = 99\n"); g("commit", "-qam", "harmless replacement")
replacement = g("rev-parse", "HEAD"); g("checkout", "-q", "feat"); g("replace", sha, replacement)
assert "+x = 2" in audit.branch_diff(wt, "main", sha) and "+x = 99" not in audit.branch_diff(wt, "main", sha)
g("replace", "-d", sha)
calls.clear()
v = audit.merge_audit(wt, sha=sha, target="main", goal="Merge #1: bump x")
assert v["ok"] and v["sha"] == sha and "+x = 2" in calls[1][2] and "-x = 1" in calls[1][2], (v, calls[:2])
calls.clear()
assert audit.merge_audit(wt, sha=sha, target="main")["ok"] and not calls  # cached: once per head
assert audit.cached(wt, sha)["ok"] and audit.cached(wt, "f" * 40) is None
Path(wt, "artifact.bin").write_bytes(b"\0ghp_" + b"A" * 30)
g("add", "artifact.bin"); g("commit", "-qm", "binary fixture"); binary_sha = g("rev-parse", "HEAD")
with mock.patch.object(audit, "forge_refs", return_value=(binary_sha, "main", g("rev-parse", "main"))), \
     mock.patch.object(audit, "forge_diff", side_effect=AssertionError("do not use binary-omitting forge diff")):
    binary_verdict = audit.merge_audit(wt, sha=binary_sha, target="main", goal="Merge #1", project="octo/x", iid=3, forge="github", tag="binary")
assert not binary_verdict["ok"] and binary_verdict["failed"][0]["contract"] == "SafeToPublish"
g("reset", "--hard", sha)
g("commit", "--allow-empty", "-qm", f"rotate {tok}")
message_sha = g("rev-parse", "HEAD")
with mock.patch.object(audit, "forge_refs", return_value=(message_sha, "main", g("rev-parse", "main"))):
    message_verdict = audit.merge_audit(wt, sha=message_sha, target="main", goal="Merge #1", project="octo/x", iid=3, forge="github", tag="message")
assert not message_verdict["ok"] and message_verdict["failed"][0]["contract"] == "SafeToPublish" and tok not in json.dumps(message_verdict)
g("reset", "--hard", sha)
Path(wt, "temporary.txt").write_text(tok + "\n")
g("add", "temporary.txt"); g("commit", "-qm", "temporary value")
Path(wt, "temporary.txt").unlink(); Path(wt, "app.py").write_text("x = 3\n")
g("add", "-A"); g("commit", "-qm", "remove temporary value and update app")
history_sha = g("rev-parse", "HEAD")
assert tok not in audit.branch_diff(wt, "main", history_sha) and tok in audit.branch_history(wt, "main", history_sha)
with mock.patch.object(audit, "forge_refs", return_value=(history_sha, "main", g("rev-parse", "main"))):
    history_verdict = audit.merge_audit(wt, sha=history_sha, target="main", goal="Merge #1", project="octo/x", iid=3, forge="github", tag="history")
assert not history_verdict["ok"] and history_verdict["failed"][0]["contract"] == "SafeToPublish" and tok not in json.dumps(history_verdict)
g("reset", "--hard", sha)
with mock.patch.object(audit, "forge_diff", return_value=None), mock.patch.object(audit, "forge_refs", return_value=(sha, "main", "stale-target")):
    assert "cannot refresh" in audit.merge_audit(wt, sha=sha, target="main", goal="Merge #1", project="octo/x", iid=3, forge="github", tag="stale-local")["error"]
old_target = g("rev-parse", "refs/remotes/origin/main")
def refresh_target(path, *args, **kwargs):
    if args[:1] == ("fetch",):
        g("update-ref", "refs/remotes/origin/main", replacement)
        return ""
    return real_git(path, *args, **kwargs)
real_git = audit.git
with mock.patch.object(audit, "git", side_effect=refresh_target), \
     mock.patch.object(audit, "forge_refs", return_value=(sha, "main", replacement)):
    assert audit.merge_audit(wt, sha=sha, target="main", goal="Merge #1", project="octo/x", iid=3, forge="github", tag="refresh-target")["ok"]
g("update-ref", "refs/remotes/origin/main", old_target)
with mock.patch.object(audit, "git", side_effect=refresh_target), \
     mock.patch.object(audit, "forge_refs", side_effect=[(sha, "main", replacement), (sha, "main", old_target)]):
    assert "target changed" in audit.merge_audit(wt, sha=sha, target="main", goal="Merge #1", project="octo/x", iid=3, forge="github", tag="moving-target")["error"]
g("update-ref", "refs/remotes/origin/main", old_target)
with mock.patch.object(audit, "forge_diff", return_value=None) as fd:
    e = audit.merge_audit(wt, sha="0" * 40, target="main", goal="Merge #1", project="octo/x", iid=3)
assert not e["ok"] and "no diff" in e["error"] and fd.call_args.args == ("octo/x", 3, None, "0" * 40) and "couldn't run" in audit.summary(e), e
calls.clear()
assert audit.merge_audit(wt, sha="0" * 40)["error"] and not calls  # a fresh error is cached too (no hammering)...
f = audit.state_dir() / f"{audit.key(wt, '0' * 40)}.json"
f.write_text(json.dumps({**json.loads(f.read_text()), "ts": 1}))
with mock.patch.object(audit, "forge_diff", return_value=DIFF):
    assert audit.merge_audit(wt, sha="0" * 40, goal="Merge #1", project="octo/x", iid=3)["ok"]  # ...and retried once it's 10 minutes old
# the forge's diff comes first (the agent can't shape it); an empty diff is an error, not a pass
calls.clear()
with mock.patch.object(audit, "forge_diff", return_value=DIFF.replace("x = 2", "x = 7")) as fd:
    assert audit.merge_audit(wt, sha="1" * 40, goal="Merge #1", project="octo/x", iid=3, forge="github")["ok"] and "+x = 7" in calls[1][2]
with mock.patch.object(audit, "forge_diff", return_value="\n"):
    assert "empty diff" in audit.merge_audit(wt, sha="2" * 40, goal="Merge #1", project="octo/x", iid=3)["error"]
assert "no principal goal" in audit.merge_audit(wt, sha="4" * 40, goal=audit.goal_of({}, {"title": "agent-authored scope"}))["error"]
# the worktree's own diff can't be bent by its config: an external diff driver, a `-diff` attribute
g("config", "diff.external", "/bin/true")
Path(wt, ".git/info").mkdir(exist_ok=True); Path(wt, ".git/info/attributes").write_text("* -diff\n")
d = audit.branch_diff(wt, "main", sha)
assert "+x = 2" in d and "Binary" not in d, d
g("config", "--unset", "diff.external"); Path(wt, ".git/info/attributes").unlink()
# A named target must never silently fall back to another branch's diff.
assert audit.base_ref(wt, "nope") is None and audit.branch_diff(wt, "nope", sha) is None
g("update-ref", "refs/remotes/origin/master", "refs/remotes/origin/main"); g("update-ref", "-d", "refs/remotes/origin/main")
assert audit.base_ref(wt) == "origin/master" and "+x = 2" in audit.branch_diff(wt, None, sha)
g("update-ref", "-d", "refs/remotes/origin/master")
assert audit.base_ref(wt) is None and audit.branch_diff(wt) is None
g("update-ref", "refs/remotes/origin/main", "main")
# non-UTF-8 output doesn't crash the audit
Path(wt, "latin.txt").write_bytes(b"caf\xe9\n"); g("add", "latin.txt"); g("commit", "-qm", "latin")
assert "caf" in audit.branch_diff(wt, "main")
g("reset", "-q", "--hard", sha)
# an unverified result is retried after RETRY; require_model defaults to holding it
with mock.patch.dict(sys.modules, {"decide": None}):
    u = audit.contracts({}, audit.MERGE_CHECKS, diff=DIFF)
    assert not u["ok"] and u["unverified"]
    greenlight.CONFIG["audit"] = {"require_model": False}
    u = audit.contracts({}, audit.MERGE_CHECKS, diff=DIFF)
    assert u["ok"] and u["unverified"]  # deliberate opt-out remains available
    del greenlight.CONFIG["audit"]
greenlight.CONFIG["audit"] = {"require_model": True}
with mock.patch.object(audit, "ask", return_value={"diff_matches_goal": {"value": True, "prob": 0.9, "provider": "jev"},
                                                     "tests_not_weakened": {"value": True, "prob": 0.5, "provider": "local"}}):
    mixed = audit.contracts({}, ("diff_matches_goal", "tests_not_weakened"), diff=DIFF)
assert not mixed["ok"] and mixed["unverified"] and "a contract has no model answer" in mixed["error"], mixed
del greenlight.CONFIG["audit"]
assert audit.fresh({"ok": True, "unverified": True, "ts": time.time()}) and not audit.fresh({"ok": True, "unverified": True, "ts": 1})
assert audit.fresh({"ok": True, "ts": 1}) and not audit.fresh(None)  # a model's verdict on a head stands
# merge_verdict: greenlight.py's automerge, from the scan's workspace + forge MR: the cache, else started in the background
calls.clear()
_ws, _mr = {"path": wt, "project": "octo/x", "goal": "Merge #1"}, {"sha": sha, "target_branch": "main", "iid": 1}
audit.save(wt, sha, {"ok": True, "failed": []}, audit.ident(_ws, _mr))
assert audit.merge_verdict(_ws, _mr)["ok"] and not calls
_scoped_ws, _scoped_mr = {**_ws, "forge": "github"}, {**_mr, "sha": "6" * 40}
audit.save(wt, _scoped_mr["sha"], {"ok": True, "failed": [], "target_oid": "base1"}, audit.ident(_scoped_ws, _scoped_mr))
with mock.patch.object(audit, "forge_refs", return_value=("6" * 40, "main", "base1")):
    assert audit.merge_verdict(_scoped_ws, _scoped_mr)["ok"]
blocked_ws, blocked_mr = {**_ws, "name": "retry-ping"}, {**_mr, "sha": "7" * 40, "ref": "#7"}
audit.save(wt, blocked_mr["sha"], {"ok": False, "failed": [{"check": "diff_matches_goal", "contract": "NoScopeCreep", "p_broken": .9}]}, audit.ident(blocked_ws, blocked_mr))
with mock.patch.object(greenlight, "ping", side_effect=OSError("outbox")) as pg:
    assert not audit.merge_verdict(blocked_ws, blocked_mr)["ok"] and pg.call_count == 1
with mock.patch.object(greenlight, "ping") as pg:
    assert not audit.merge_verdict(blocked_ws, blocked_mr)["ok"] and pg.call_count == 1
    assert not audit.merge_verdict(blocked_ws, blocked_mr)["ok"] and pg.call_count == 1
with mock.patch.object(audit, "forge_refs", return_value=("6" * 40, "main", "base2")), mock.patch.object(audit.subprocess, "Popen") as po:
    assert audit.merge_verdict(_scoped_ws, _scoped_mr)["pending"] and po.call_count == 1
# A stricter semantic policy invalidates a pass even when the commit and goal are unchanged.
with mock.patch.object(audit.subprocess, "Popen") as po:
    greenlight.CONFIG["audit"] = {"threshold": 0.7}
    assert audit.merge_verdict(_ws, _mr)["pending"] and po.call_count == 1
    greenlight.CONFIG["audit"] = {"require_model": False}
    assert audit.merge_verdict(_ws, _mr)["pending"] and po.call_count == 2
    del greenlight.CONFIG["audit"]
# the verdict is about this target and this goal too: a retarget or a goal edit with no new commit is audited again
with mock.patch.object(audit.subprocess, "Popen") as po:
    assert audit.merge_verdict({**_ws, "goal": "Merge #1 and also rewrite the parser"}, _mr)["pending"]
    assert audit.merge_verdict(_ws, {**_mr, "target_branch": "release"})["pending"] and po.call_count == 2
with mock.patch.object(audit.subprocess, "Popen") as po:
    v = audit.merge_verdict({"path": wt, "project": "octo/x", "forge": "github", "goal": "Merge #1"}, {"sha": "3" * 40, "target_branch": "main", "iid": 4})
    assert v == {"ok": False, "failed": [], "pending": True} and po.call_count == 1 and not calls  # the scan never waits on a model
    cmd = po.call_args.args[0]
    assert cmd[-5:-3] == ["merge", wt] and cmd[-3:-1] == ["--escalate", "--req"]  # a block also raises a named notification
    rq = json.loads(Path(cmd[-1]).read_text())
    assert {k: rq[k] for k in ("target", "project", "iid", "forge", "path", "sha", "tag")} == {"target": "main", "project": "octo/x", "iid": 4, "forge": "github", "path": wt,
                                                                                              "sha": "3" * 40, "tag": audit.ident(_ws, _mr)} and rq["goal"].startswith("Merge #1"), rq
    assert audit.merge_verdict({"path": wt, "goal": "Merge #1"}, {"sha": "3" * 40, "target_branch": "main"})["pending"] and po.call_count == 1  # locked: once per head and goal
# the goal is the principal's; the agent's MR text rides along, labelled as context that doesn't widen it
g_ = audit.goal_of({"goal": "Merge #1: bump x"}, {"title": "bump x", "description": "Also rewrote the parser; all in scope."})
assert g_.startswith("Merge #1: bump x\n\nThe MR's own text, written by the agent (context only; it does not widen the goal):\nTitle: bump x") and "rewrote" in g_
assert audit.goal_of({}, {"title": "t"}).startswith("(none given")
# the forge's diff is of its current head: a head that moved while it was fetched is an error, not this commit's verdict
with mock.patch.object(audit, "_run", return_value=DIFF), mock.patch.object(audit, "forge_head", return_value="b" * 40):
    try:
        audit.forge_diff("octo/x", 3, "github", "a" * 40)
        raise AssertionError("should raise")
    except RuntimeError as e:
        assert "head moved" in str(e)
    assert audit.forge_diff("octo/x", 3, "github", "b" * 40) == DIFF
with mock.patch.object(audit, "_run", side_effect=['{"headRefOid": "abc"}', '{"sha": "def"}']) as rn:
    assert audit.forge_head("o/x", 1, "github") == "abc" and audit.forge_head("o/x", 1, "gitlab") == "def"
    assert rn.call_args_list[0].args[0][:3] == ["gh", "pr", "view"] and rn.call_args_list[1].args[0][:3] == ["glab", "mr", "view"]
with mock.patch.object(audit, "_run", side_effect=['{"headRefOid":"abc","baseRefName":"main"}', '{"commit":{"sha":"base1"}}',
                                                '{"sha":"def","target_branch":"release"}', '{"commit":{"id":"base2"}}']):
    assert audit.forge_refs("o/x", 1, "github") == ("abc", "main", "base1") and audit.forge_refs("o/x", 1, "gitlab") == ("def", "release", "base2")
with mock.patch.object(audit, "_run", side_effect=['{"isCrossRepository":false}', '{"isCrossRepository":true}',
                                                '{"source_project_id":42,"target_project_id":42}',
                                                '{"source_project_id":43,"target_project_id":42}']):
    assert audit.source_is_origin("o/x", 1, "github") and not audit.source_is_origin("o/x", 1, "github")
    assert audit.source_is_origin("o/x", 1, "gitlab") and not audit.source_is_origin("o/x", 1, "gitlab")
for url, project, forge, good in [("https://github.com/o/x.git", "o/x", "github", True),
                                  ("git@" + "github.com:o/x.git", "o/x", "github", True),
                                  ("ssh://git@" + "gitlab.com/group/x.git", "group/x", "gitlab", True),
                                  ("https://github.com/other/x.git", "o/x", "github", False),
                                  ("https://evil.example/o/x.git", "o/x", "github", False),
                                  ("https://github.com/o/x.git\nhttps://github.com/other/x.git", "o/x", "github", False)]:
    with mock.patch.object(audit, "git", return_value=url):
        assert bool(audit.origin_push_url(wt, project, forge)) == good, url
with mock.patch.object(audit, "forge_refs", return_value=("abc", "main", "base1")), mock.patch.object(audit.subprocess, "run") as merged:
    merged.return_value.returncode = 0
    cmd = ["gh", "pr", "merge", "23", "-R", "o/x", "--merge"]
    assert audit.checked_merge("github", "o/x", "23", "main", "abc", "base1", cmd) == 0
    assert merged.call_args.args[0] == cmd + ["--match-head-commit", "abc"]
    assert audit.checked_merge("github", "o/x", "23", "release", "abc", "base1", cmd) == 4 and merged.call_count == 1
    assert audit.checked_merge("github", "o/x", "23", "main", "abc", "base2", cmd) == 4 and merged.call_count == 1
    assert audit.same_target({"target_oid": "base1"}, "o/x", 23, "github", "abc", "main")
    assert not audit.same_target({"target_oid": "base2"}, "o/x", 23, "github", "abc", "main")
print("merge audit ok")

# ---- kick: one background audit per head; escalate: the user pinged once per head with the exact contracts
with mock.patch.object(audit.subprocess, "Popen") as po:
    assert audit.kick(wt, sha, escalate=True) and not audit.kick(wt, sha) and po.call_count == 1
    assert po.call_args.args[0][-3:] == ["merge", wt, "--escalate"]
    lk = audit.state_dir() / f"{audit.key(wt, sha)}.lock"
    os.utime(lk, (1, 1))
    assert audit.kick(wt, sha) and po.call_count == 2  # a stale lock (a crashed audit) doesn't hold it forever
row = {"name": "feat", "path": wt, "mr": {"sha": sha, "ref": "#1", "web_url": "https://gh/1"}}
blk = {"ok": False, "failed": [{"check": "diff_matches_goal", "contract": "NoScopeCreep", "p_broken": 0.9}]}
with mock.patch.object(greenlight, "ping") as pg:
    assert audit.escalate(row, blk) and not audit.escalate(row, blk) and pg.call_count == 1
    t = pg.call_args.args[0]
    assert "NoScopeCreep (diff_matches_goal, p=0.90)" in t and "https://gh/1" in t and "\nOptions: " in t and pg.call_args.kwargs == {"key": "feat", "kind": "audit-blocked"}
row2 = {**row, "mr": {**row["mr"], "sha": "9" * 40}}
with mock.patch.object(greenlight, "ping", side_effect=OSError("outbox")):
    try:
        audit.escalate(row2, blk)
    except OSError:
        pass
with mock.patch.object(greenlight, "ping") as pg:
    assert audit.escalate(row2, blk) and pg.call_count == 1  # a ping that failed isn't counted as told
print("kick/escalate ok")

# ---- what an unattended run is told, per Auto-run gate
assert audit.brief("ci", 0) == audit.brief("conflicts", 0) == audit.brief("threads", 0) == audit.brief("review", 0) == ""  # stop-before-push / draft-only gates
b = audit.brief("ci", 1, audit="A")
assert "`python3 A gate push --execute`" in b and "audit and push exactly this MR branch" in b and "gate post" not in b
b = audit.brief("threads", 1, audit="A")
assert "gate push" in b and "gate post" not in b  # push, draft replies: nothing posted
b = audit.brief("threads", 2, audit="A")
assert "gate push" in b and "`python3 A gate post <file> --kind reply --execute`" in b and "gate posts and resolves" in b and '"thread_id"' in b
b = audit.brief("review", 1, audit="A")
assert "gate push" not in b and "--kind review --execute" in b and '"thread"' not in b
print("brief ok")


# ---- the CLI an agent runs, in its worktree
g("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
audit.origin_push_url = lambda *_: "https://github.com/o/x.git"  # fake forge destination for this scratch repo
real_remote_branch_sha = audit.remote_branch_sha
audit.remote_branch_sha = lambda path, destination, branch: (audit.git(path, "rev-parse", f"origin/{branch}") or "").strip()
def cli(*a):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = audit.main(list(a))
    return code, out.getvalue().strip()


code, out = cli("gate", "push", "--path", wt)
assert code == 4 and "no principal goal" in out, out
with mock.patch.object(audit, "row_for", return_value={"path": wt, "goal": "Merge #1", "branch": "feat", "project": "o/x", "forge": "github"}), \
     mock.patch.object(audit, "push_audited", return_value=0) as pushed:
    code, out = cli("gate", "push", "--path", wt, "--execute")
assert code == 0 and pushed.call_args.args == (wt, g("rev-parse", "HEAD"), "feat", False, None), (code, out)
assert pushed.call_args.kwargs == {"destination": "https://github.com/o/x.git"}
with mock.patch.object(audit, "row_for", return_value={"path": wt, "goal": "Merge #1", "branch": "feat", "project": "o/x", "forge": "github"}), \
     mock.patch.object(audit, "origin_push_url", return_value=None), \
     mock.patch.object(audit, "push_audited") as pushed:
    code, out = cli("gate", "push", "--path", wt, "--execute")
assert code == 4 and "push destination" in out and not pushed.called, (code, out)
with mock.patch.object(audit, "row_for", return_value={"path": wt, "goal": "Merge #1", "branch": "feat", "project": "o/x", "forge": "github"}), \
     mock.patch.object(audit, "origin_push_url", side_effect=["https://github.com/o/x.git", "https://github.com/other/x.git"]), \
     mock.patch.object(audit, "push_audited") as pushed:
    code, out = cli("gate", "push", "--path", wt, "--execute")
assert code == 4 and "destination changed" in out and not pushed.called, (code, out)
with mock.patch.object(audit, "row_for", return_value={"path": wt, "goal": "Merge #1", "branch": "other", "project": "o/x", "forge": "github"}), \
     mock.patch.object(audit, "push_audited") as pushed:
    code, out = cli("gate", "push", "--path", wt, "--execute")
assert code == 4 and "scanned source branch" in out and not pushed.called, (code, out)
(greenlight.STATE / "last.json").write_text(json.dumps({"rows": [{"path": wt, "goal": "Merge #1", "project": "o/x", "forge": "github", "mr": {"target_branch": "main", "source_branch": "feat"}}]}))
code, out = cli("gate", "push", "--path", wt)
assert code == 0 and out == "audit passed: push", out
with mock.patch.object(audit, "push_audited", return_value=0) as pushed:
    code, out = cli("gate", "push", "--path", wt, "--execute", "--force-with-lease")
assert code == 0 and pushed.call_args.args == (wt, g("rev-parse", "HEAD"), "feat", True, None), (code, out, pushed.call_args)
initial_receipt = audit.cached(wt, g("rev-parse", "HEAD"), action="push")
assert initial_receipt["new_sha"] == g("rev-parse", "HEAD") and initial_receipt["source"] == "feat"
secret_branch = "feature/ghp_" + "A" * 35
g("checkout", "-qb", secret_branch)
with mock.patch.object(audit, "row_for", return_value={"path": wt, "goal": "Merge #1", "project": "o/x", "forge": "github", "branch": secret_branch}), \
     mock.patch.object(audit, "push_audited") as pushed:
    code, out = cli("gate", "push", "--path", wt, "--execute")
assert code == 3 and "SafeToPublish" in out and secret_branch not in out and not pushed.called, (code, out)
g("checkout", "-q", "feat")
g("update-ref", "refs/remotes/origin/release", "refs/remotes/origin/main")
with mock.patch.object(audit, "row_for", return_value={"path": wt, "goal": "Merge #1", "project": "o/x", "forge": "github", "branch": "feat"}), \
     mock.patch.object(audit, "push_audited", return_value=0):
    code, out = cli("gate", "push", "--path", wt, "--target", "release", "--execute")
assert code == 0 and audit.cached(wt, g("rev-parse", "HEAD"), action="push")["target"] == "release", (code, out)
with mock.patch.object(audit, "git", return_value=initial_receipt["new_sha"] + "\trefs/heads/feat\n"):
    assert real_remote_branch_sha(wt, "https://github.com/o/x.git", "feat") == initial_receipt["new_sha"]
with mock.patch.object(audit, "git", return_value=""):
    assert real_remote_branch_sha(wt, "https://github.com/o/x.git", "feat") is None
with mock.patch.object(audit.subprocess, "run") as ran:
    ran.return_value.returncode, ran.return_value.stdout = 0, "pushed"
    assert audit.push_audited(wt, sha, "feat", force=True, expected="a" * 40, destination="https://github.com/o/x.git") == 0
    assert ran.call_args.args[0] == ["git", "-C", wt, "push", f"--force-with-lease=refs/heads/feat:{'a' * 40}", "https://github.com/o/x.git", f"{sha}:refs/heads/feat"]
assert audit.push_audited(wt, sha, "feat", force=True) == 4
with mock.patch.object(audit, "row_for", return_value={"path": wt, "goal": "Merge #1", "project": "o/x", "forge": "github",
                                                    "mr": {"iid": 23, "target_branch": "main"}}), \
     mock.patch.object(audit, "forge_refs", return_value=("abc", "main", "stale-target")):
    code, out = cli("gate", "push", "--path", wt)
assert code == 4 and "scanned MR head or local target ref is stale" in out, out
scanned_source = g("rev-parse", "HEAD")
target_source = g("rev-parse", "refs/remotes/origin/main")
g("update-ref", "refs/remotes/origin/feat", scanned_source)
scanned_row = {"path": wt, "goal": "Merge #1", "project": "o/x", "forge": "github",
               "mr": {"iid": 23, "sha": scanned_source, "target_branch": "main", "source_branch": "feat"}}
with mock.patch.object(audit, "row_for", return_value=scanned_row), \
     mock.patch.object(audit, "forge_refs", return_value=(scanned_source, "main", target_source)), \
     mock.patch.object(audit, "source_is_origin", return_value=True), \
     mock.patch.object(audit, "push_audited", return_value=0) as pushed:
    code, out = cli("gate", "push", "--path", wt, "--execute", "--force-with-lease")
    assert code == 0 and pushed.call_args.args[-1] == scanned_source, (code, out, pushed.call_args)
g("checkout", "-qb", "alias")
g("config", "branch.alias.remote", "origin")
g("config", "branch.alias.merge", "refs/heads/feat")
alias_row = {**scanned_row, "branch": "alias"}
with mock.patch.object(audit, "row_for", return_value=alias_row), \
     mock.patch.object(audit, "forge_refs", return_value=(scanned_source, "main", target_source)), \
     mock.patch.object(audit, "source_is_origin", return_value=True), \
     mock.patch.object(audit, "push_audited", return_value=0) as pushed:
    code, out = cli("gate", "push", "--path", wt, "--execute")
    assert code == 0 and pushed.call_args.args[2] == "feat", (code, out, pushed.call_args)
g("config", "branch.alias.merge", "refs/heads/unrelated")
with mock.patch.object(audit, "row_for", return_value=alias_row), \
     mock.patch.object(audit, "forge_refs", return_value=(scanned_source, "main", target_source)), \
     mock.patch.object(audit, "source_is_origin", return_value=True):
    code, out = cli("gate", "push", "--path", wt, "--execute")
assert code == 4 and "does not track" in out, out
g("checkout", "-q", "feat")
with mock.patch.object(audit, "row_for", return_value=scanned_row), \
     mock.patch.object(audit, "forge_refs", return_value=("b" * 40, "main", target_source)):
    code, out = cli("gate", "push", "--path", wt, "--execute", "--force-with-lease")
assert code == 4 and "scanned MR head" in out, out
g("update-ref", "refs/remotes/origin/feat", replacement)
with mock.patch.object(audit, "row_for", return_value=scanned_row), \
     mock.patch.object(audit, "forge_refs", return_value=(scanned_source, "main", target_source)):
    code, out = cli("gate", "push", "--path", wt, "--execute", "--force-with-lease")
assert code == 4 and "local source ref is stale" in out, out
g("update-ref", "refs/remotes/origin/feat", scanned_source)
old_source = sha
g("commit", "--allow-empty", "-qm", "reply fix")
scanned_source = g("rev-parse", "HEAD")
g("update-ref", "refs/remotes/origin/feat", old_source)
previous_scan = {**scanned_row, "mr": {**scanned_row["mr"], "sha": old_source}}
with mock.patch.object(audit, "row_for", return_value=previous_scan), \
     mock.patch.object(audit, "forge_refs", return_value=(old_source, "main", target_source)), \
     mock.patch.object(audit, "source_is_origin", return_value=False), \
     mock.patch.object(audit, "push_audited") as pushed:
    code, out = cli("gate", "push", "--path", wt, "--execute")
assert code == 4 and "source is a fork" in out and not pushed.called, (code, out)
with mock.patch.object(audit, "row_for", return_value=previous_scan), \
     mock.patch.object(audit, "forge_refs", return_value=(old_source, "main", target_source)), \
     mock.patch.object(audit, "source_is_origin", return_value=True), \
     mock.patch.object(audit, "push_audited", return_value=0):
    code, out = cli("gate", "push", "--path", wt, "--execute")
assert code == 0 and audit.cached(wt, old_source, action="push")["new_sha"] == scanned_source, (code, out)
assert g("rev-parse", "refs/remotes/origin/feat") == scanned_source  # explicit URL push may not update this ref itself
reply_file = Path(wt, "reply.json")
reply_file.write_text(json.dumps([{"body": "Fixed the boundary in commit " + scanned_source, "thread_id": "thread-1", "thread": "fabricated mild text"}]))
with mock.patch.object(audit, "row_for", return_value=previous_scan), \
     mock.patch.object(audit, "forge_refs", return_value=(scanned_source, "main", target_source)), \
     mock.patch.object(audit, "live_threads", return_value={"thread-1": "Boundary is wrong"}), \
     mock.patch.object(audit, "forge_diff", return_value=DIFF):
    code, out = cli("gate", "post", str(reply_file), "--path", wt, "--kind", "reply")
    assert code == 0 and len(json.loads(out)["post"]) == 1 and json.loads(out)["post"][0]["thread"] == "Boundary is wrong", (code, out)
    with mock.patch.object(audit, "post_reply") as posted:
        code, out = cli("gate", "post", str(reply_file), "--path", wt, "--kind", "reply", "--execute")
    assert code == 0 and json.loads(out)["posted"] == ["thread-1"] and not json.loads(out)["post"], (code, out)
    assert posted.call_args.args == ("o/x", 23, "github", "thread-1", "Fixed the boundary in commit " + scanned_source, "Boundary is wrong")
    code, out = cli("gate", "post", str(reply_file), "--path", wt, "--kind", "review")
    assert code == 4 and "rescan before posting" in json.loads(out)["error"], (code, out, old_source, scanned_source, previous_scan)
with mock.patch.object(audit, "row_for", return_value=previous_scan), mock.patch.object(audit, "live_threads", return_value={}):
    code, out = cli("gate", "post", str(reply_file), "--path", wt, "--kind", "reply")
assert code == 4 and "unresolved live thread_id" in json.loads(out)["error"] and not json.loads(out)["post"], out
with mock.patch.object(audit, "row_for", return_value=previous_scan), \
     mock.patch.object(audit, "forge_refs", return_value=(scanned_source, "main", target_source)), \
     mock.patch.object(audit, "forge_diff", return_value=DIFF), \
     mock.patch.object(audit, "live_threads", side_effect=[{"thread-1": "Boundary is wrong"},
                                                        {"thread-1": "Boundary is wrong"}, {"thread-1": "Changed request"}]), \
     mock.patch.object(audit, "post_reply") as posted:
    code, out = cli("gate", "post", str(reply_file), "--path", wt, "--kind", "reply", "--execute")
assert code == 4 and "changed before posting" in json.loads(out)["error"] and not posted.called, out
reply_file.write_text(json.dumps([{"body": "First", "thread_id": "thread-1"}, {"body": "Second", "thread_id": "thread-1"}]))
with mock.patch.object(audit, "row_for", return_value=previous_scan), mock.patch.object(audit, "post_reply") as posted:
    code, out = cli("gate", "post", str(reply_file), "--path", wt, "--kind", "reply", "--execute")
assert code == 4 and "duplicate thread_id" in json.loads(out)["error"] and not posted.called, out
reply_file.write_text(json.dumps([{"body": "First", "thread_id": "thread-1"}, {"body": "Second", "thread_id": "thread-2"}]))
with mock.patch.object(audit, "row_for", return_value=previous_scan), \
     mock.patch.object(audit, "forge_refs", return_value=(scanned_source, "main", target_source)), \
     mock.patch.object(audit, "forge_diff", return_value=DIFF), \
     mock.patch.object(audit, "live_threads", side_effect=[{"thread-1": "First issue", "thread-2": "Second issue"},
                                                        {"thread-1": "First issue", "thread-2": "Second issue"},
                                                        {"thread-1": "First issue"}, {"thread-2": "Changed issue"}]), \
     mock.patch.object(audit, "post_reply") as posted:
    code, out = cli("gate", "post", str(reply_file), "--path", wt, "--kind", "reply", "--execute")
assert code == 4 and json.loads(out)["posted"] == ["thread-1"] and posted.call_count == 1, out
reply_file.unlink()
g("reset", "--hard", sha)
g("update-ref", "refs/remotes/origin/feat", sha)
g("commit", "--allow-empty", "-qm", f"rotate {tok}")
code, out = cli("gate", "push", "--path", wt)
assert code == 3 and "SafeToPublish" in out and tok not in out, out
g("reset", "--hard", "HEAD~1")
Path(wt, "temporary.txt").write_text(tok + "\n")
g("add", "temporary.txt"); g("commit", "-qm", "add temporary value")
Path(wt, "temporary.txt").unlink(); Path(wt, "app.py").write_text("x = 4\n")
g("add", "-A"); g("commit", "-qm", "remove value and update app")
code, out = cli("gate", "push", "--path", wt)
assert code == 3 and "SafeToPublish" in out and tok not in out, out  # clean net diff still publishes the earlier commit
g("reset", "--hard", sha)
Path(wt, "app.py").write_text("x = 9\n"); g("add", "app.py")
code, out = cli("gate", "push", "--path", wt)
assert code == 4 and "worktree is not clean" in out, out  # staged work must be committed before the audited HEAD is pushed
g("reset", "--hard", "HEAD")
ans[Q["tests_not_weakened"]] = {"value": False, "prob": 0.03, "provider": "haiku"}
code, out = cli("gate", "push", "--path", wt)
assert code == 3 and out.startswith("BLOCKED GenuineFix (tests_not_weakened, p=0.97, haiku): "), out
ans.clear()
with mock.patch.object(audit, "branch_diff", return_value=None):
    code, out = cli("gate", "push", "--path", wt)
assert code == 4 and out.startswith("AUDIT ERROR") and "don't push" in out
with mock.patch.object(audit, "branch_diff", return_value=""):
    code, out = cli("gate", "push", "--path", wt)
assert code == 4 and out.startswith("AUDIT ERROR") and "don't push" in out
greenlight.CONFIG["audit"] = {"require_model": True}
with mock.patch.dict(sys.modules, {"decide": None}):
    code, out = cli("gate", "push", "--path", wt)
del greenlight.CONFIG["audit"]
assert code == 4 and out.startswith("AUDIT ERROR a contract has no model answer") and "passed" not in out, out  # never "passed" on a nonzero exit
assert "gate push --execute" in audit.brief("ci", 1)
cf = Path(wt, "c.json")
cf.write_text(json.dumps([{"body": "STRONG: crash on empty input", "path": "app.py", "line": 1}]))
ans["STRONG"] = 0.9
code, out = cli("gate", "post", str(cf), "--path", wt)
assert code == 4 and "no fully scanned MR" in json.loads(out)["error"] and not json.loads(out)["post"], out
code, out = cli("gate", "post", str(cf), "--path", wt, "--kind", "repply")
assert code == 4 and "--kind must be review or reply" in json.loads(out)["error"], out
with mock.patch.object(audit, "row_for", return_value=None):
    code, out = cli("gate", "post", str(cf), "--path", wt)
assert code == 4 and "no fully scanned MR" in json.loads(out)["error"] and not json.loads(out)["post"], out
scanned = {"project": "o/x", "forge": "github", "mr": {"iid": 23, "sha": "abc", "target_branch": "main"}}
with mock.patch.object(audit, "row_for", return_value=scanned), mock.patch.object(audit, "forge_refs", return_value=("abc", "main", "base1")), \
     mock.patch.object(audit, "forge_diff", return_value=DIFF) as fd, mock.patch.object(audit, "branch_diff", side_effect=AssertionError("wrong checkout")):
    code, out = cli("gate", "post", str(cf), "--path", wt, "--kind", "review")
assert code == 0 and len(json.loads(out)["post"]) == 1 and fd.call_args.args == ("o/x", 23, "github", "abc"), out
with mock.patch.object(audit, "row_for", return_value=scanned), mock.patch.object(audit, "forge_refs", return_value=("abc", "main", "base1")), \
     mock.patch.object(audit, "forge_diff", return_value=DIFF), mock.patch.object(audit, "post_review_comment", return_value="C1") as posted:
    code, out = cli("gate", "post", str(cf), "--path", wt, "--kind", "review", "--execute")
assert code == 0 and json.loads(out)["posted"] == ["C1"] and not json.loads(out)["post"], out
assert posted.call_args.args[:4] == ("o/x", 23, "github", "abc") and posted.call_args.args[4]["body"] == "STRONG: crash on empty input"
with mock.patch.object(audit, "row_for", return_value=scanned), \
     mock.patch.object(audit, "forge_refs", side_effect=[("abc", "main", "base1")] * 3 + [("changed", "main", "base1")]), \
     mock.patch.object(audit, "forge_diff", return_value=DIFF), mock.patch.object(audit, "post_review_comment") as posted:
    code, out = cli("gate", "post", str(cf), "--path", wt, "--kind", "review", "--execute")
assert code == 4 and "MR changed before posting" in json.loads(out)["error"] and not posted.called, out
with mock.patch.object(audit, "row_for", return_value=scanned), mock.patch.object(audit, "forge_refs", side_effect=[("abc", "main", "base1"),
                                                                                                             ("abc", "main", "base1"), ("new-head", "main", "base1")]), \
     mock.patch.object(audit, "forge_diff", return_value=DIFF):
    code, out = cli("gate", "post", str(cf), "--path", wt, "--kind", "review")
assert code == 4 and "changed while comments were scored" in json.loads(out)["error"] and not json.loads(out)["post"], out
with mock.patch.object(audit, "row_for", return_value=scanned), mock.patch.object(audit, "forge_refs", return_value=("new-head", "main", "base1")):
    code, out = cli("gate", "post", str(cf), "--path", wt, "--kind", "review")
assert code == 4 and "rescan before posting" in json.loads(out)["error"], out
with mock.patch.object(audit, "branch_diff", return_value=None):
    code, out = cli("gate", "post", str(cf), "--path", wt)
assert code == 4 and "no fully scanned MR" in json.loads(out)["error"] and not json.loads(out)["post"], out
cf.write_text('{"not": "a list"}')
code, out = cli("gate", "post", str(cf), "--path", wt)
assert code == 4 and "want a JSON list" in json.loads(out)["error"] and json.loads(out)["post"] == []
ans.clear()
rep = Path(wt, "report.html")
rep.write_text("<html>3 workspaces</html>")
assert cli("publish", str(rep)) == (0, "audit passed: publish")
named = Path(wt, tok + ".html")
named.write_text("<html>ordinary report</html>")
code, out = cli("publish", str(named))
assert code == 3 and tok not in out and "BLOCKED SafeToPublish" in out, out
rep.write_text(f"<html>{tok}</html>")
code, out = cli("publish", str(rep))
assert code == 3 and out.startswith("BLOCKED SafeToPublish (no_secrets_or_pii, p=1.00, scan)") and tok not in out, out
# merge <path>: the scanned row's head; --escalate pings a block
(greenlight.STATE / "last.json").write_text(json.dumps({"rows": [{"name": "feat", "path": wt, "project": "octo/x", "goal": "Merge #1",
                                                                  "mr": {"sha": sha, "target_branch": "main", "iid": 1}}]}))
assert cli("merge", wt)[0] == 0 and cli("merge", "/nope")[0] == 4
Path(wt, "app.py").write_text("x = 3\n"); g("commit", "-qam", "more"); sha2 = g("rev-parse", "HEAD")
(greenlight.STATE / "last.json").write_text(json.dumps({"rows": [{"name": "feat2", "path": wt, "project": "octo/x", "goal": "Merge #2", "mr": {"sha": sha2, "target_branch": "main"}}]}))
ans[Q["diff_matches_goal"]] = {"value": False, "prob": 0.05, "provider": "jev"}
with mock.patch.object(greenlight, "ping") as pg:
    code, out = cli("merge", wt, "--escalate")
assert code == 3 and json.loads(out)["failed"][0]["contract"] == "NoScopeCreep" and pg.call_count == 1
ans.clear()
# merge --req: the scan's spec, before last.json has the row; the spec file and the lock are cleaned up
rq = audit.state_dir() / "r.req.json"
rq.write_text(json.dumps({"path": wt, "sha": sha2, "target": "main", "goal": "Merge #2: more", "tag": "main|Merge #2"}))
calls.clear()
code, out = cli("merge", wt, "--req", str(rq))
assert code == 0 and json.loads(out)["sha"] == sha2 and "Goal:\nMerge #2: more" in calls[1][2] and not rq.exists(), out
assert not (audit.state_dir() / f"{audit.key(wt, sha2, 'main|Merge #2')}.lock").exists() and audit.cached(wt, sha2, "main|Merge #2")["ok"]
rq.write_text(json.dumps({"path": wt, "sha": sha2, "target": "main", "goal": "Merge #2: more", "tag": "notification",
                          "name": "semantic-auditors", "ref": "#64", "web_url": "https://github.com/o/x/pull/64"}))
ans[Q["diff_matches_goal"]] = {"value": False, "prob": 0.05, "provider": "jev"}
with mock.patch.object(greenlight, "ping") as pg:
    code, out = cli("merge", wt, "--req", str(rq), "--escalate")
assert code == 3 and pg.call_args.kwargs["key"] == "semantic-auditors" and "#64" in pg.call_args.args[0] and "https://github.com/o/x/pull/64" in pg.call_args.args[0]
ans.clear()
assert cli()[0] == 2
# Draft PR metadata is checked before publication, and the create command receives the audited bytes.
meta = Path(tempfile.mkdtemp()) / "pr.json"
draft_row = {"path": wt, "goal": "Merge #1", "project": "o/x", "forge": "github", "branch": "feat"}
real_git = audit.git
def clean_git(path, *args, **kw):
    return "" if args[:1] == ("status",) else real_git(path, *args, **kw)
receipt = {"new_sha": real_git(wt, "rev-parse", "HEAD").strip(), "project": "o/x", "forge": "github", "source": "feat", "target": "main",
           "target_oid": "a" * 40, "goal_digest": audit.hashlib.sha256(audit.goal_of(draft_row).encode()).hexdigest()}
with mock.patch.object(audit, "row_for", return_value=draft_row), mock.patch.object(audit, "git", side_effect=clean_git), \
     mock.patch.object(audit, "cached", return_value=None), mock.patch.object(audit, "create_draft_pr") as created:
    meta.write_text(json.dumps({"title": "Fix the bug", "body": "Closes #1"}))
    code, out = cli("gate", "pr", str(meta), "--path", wt, "--execute")
assert code == 4 and "no audited push receipt" in out and not created.called, out
audit.cached = lambda *args, **kwargs: receipt
audit.remote_branch_sha = lambda path, destination, branch: receipt["new_sha"] if branch == "feat" else receipt["target_oid"]
meta.write_text(json.dumps({"title": "Fix the bug", "body": "Closes #1\n" + tok}))
with mock.patch.object(audit, "row_for", return_value=draft_row), mock.patch.object(audit, "git", side_effect=clean_git), mock.patch.object(audit, "create_draft_pr") as created:
    code, out = cli("gate", "pr", str(meta), "--path", wt, "--execute")
assert code == 3 and "SafeToPublish" in out and tok not in out and not created.called, out
meta.write_text(json.dumps({"title": "Fix " + tok, "body": "Closes #1"}))
with mock.patch.object(audit, "row_for", return_value=draft_row), mock.patch.object(audit, "git", side_effect=clean_git), mock.patch.object(audit, "create_draft_pr") as created:
    code, out = cli("gate", "pr", str(meta), "--path", wt, "--execute")
assert code == 3 and "SafeToPublish" in out and tok not in out and not created.called, out
meta.write_text(json.dumps({"title": "Fix the bug", "body": "Closes #1\nSafe description"}))
with mock.patch.object(audit, "row_for", return_value=draft_row), mock.patch.object(audit, "git", side_effect=clean_git), mock.patch.object(audit, "create_draft_pr", return_value=0) as created:
    code, out = cli("gate", "pr", str(meta), "--path", wt, "--execute")
assert code == 0 and created.call_args.args == (wt, "o/x", "github", "feat", "main", "Fix the bug", "Closes #1\nSafe description"), out
with mock.patch.object(audit, "row_for", return_value=draft_row), mock.patch.object(audit, "git", side_effect=clean_git), \
     mock.patch.object(audit, "remote_branch_sha", return_value="b" * 40), mock.patch.object(audit, "create_draft_pr") as created:
    code, out = cli("gate", "pr", str(meta), "--path", wt, "--execute")
assert code == 4 and "remote branch changed" in out and not created.called, out
with mock.patch.object(audit, "row_for", return_value={**draft_row, "goal": "Different principal goal"}), \
     mock.patch.object(audit, "git", side_effect=clean_git), mock.patch.object(audit, "create_draft_pr") as created:
    code, out = cli("gate", "pr", str(meta), "--path", wt, "--execute")
assert code == 4 and "no audited push receipt" in out and not created.called, out
with mock.patch.object(audit, "row_for", return_value=draft_row), mock.patch.object(audit, "git", side_effect=clean_git), \
     mock.patch.object(audit, "remote_branch_sha", side_effect=lambda p, u, b: receipt["new_sha"] if b == "feat" else "b" * 40), \
     mock.patch.object(audit, "create_draft_pr") as created:
    code, out = cli("gate", "pr", str(meta), "--path", wt, "--execute")
assert code == 4 and "remote target changed" in out and not created.called, out
meta.write_text(json.dumps({"title": "Fix the bug", "body": "Closes #1", "target": "release"}))
with mock.patch.object(audit, "row_for", return_value=draft_row), mock.patch.object(audit, "git", side_effect=clean_git), \
     mock.patch.object(audit, "create_draft_pr") as created:
    code, out = cli("gate", "pr", str(meta), "--path", wt, "--execute")
assert code == 4 and "target differs" in out and not created.called, out
meta.write_text(json.dumps({"title": "Fix the bug", "body": "Closes #1\nSafe description"}))
with mock.patch.object(audit, "row_for", return_value=draft_row), mock.patch.object(audit, "git", side_effect=clean_git), mock.patch.dict(sys.modules, {"decide": None}), \
     mock.patch.object(audit, "create_draft_pr") as created:
    code, out = cli("gate", "pr", str(meta), "--path", wt, "--execute")
assert code == 4 and "no model answer" in out and not created.called, out
seen = []
def fake_create(cmd, **kw):
    seen.append((cmd, Path(cmd[cmd.index("--body-file") + 1]).read_text()))
    return subprocess.CompletedProcess(cmd, 0, "https://github.com/o/x/pull/1\n", "")
with mock.patch.object(audit.subprocess, "run", side_effect=fake_create):
    assert audit.create_draft_pr(wt, "o/x", "github", "feat", "main", "Fix the bug", "Closes #1\nSafe description") == 0
cmd, body = seen[0]
assert cmd[:4] == ["gh", "pr", "create", "--repo"] and "--draft" in cmd and cmd[cmd.index("--title") + 1] == "Fix the bug"
assert cmd[cmd.index("--base") + 1] == "main"
assert body == "Closes #1\nSafe description"
# Discussion binding reads only the requested forge discussion, including all notes.
gh_threads = {"data": {"node": {"id": "T1", "isResolved": False,
                               "pullRequest": {"number": 23, "repository": {"nameWithOwner": "o/x"}},
                               "comments": {"pageInfo": {"hasNextPage": False},
                                            "nodes": [{"id": "N0", "body": "Fix null case"}, {"id": "N1", "body": "Still failing"}]}}}}
with mock.patch.object(audit, "_run", return_value=json.dumps(gh_threads)):
    assert audit.live_threads("o/x", 23, "github", ["T1"]) == {"T1": "Fix null case\n\nStill failing"}
gh_threads["data"]["node"]["comments"]["pageInfo"]["hasNextPage"] = True
with mock.patch.object(audit, "_run", return_value=json.dumps(gh_threads)):
    try: audit.live_threads("o/x", 23, "github", ["T1"])
    except RuntimeError: pass
    else: assert False, "partial note page must block"
gl_threads = {"id": "abc", "notes": [{"id": 1, "body": "Fix null case", "resolvable": True, "resolved": False}, {"id": 2, "body": "Still failing"}]}
with mock.patch.object(audit, "_run", return_value=json.dumps(gl_threads)):
    assert audit.live_threads("o/x", 23, "gitlab", ["abc"]) == {"abc": "Fix null case\n\nStill failing"}
with mock.patch.object(audit, "live_thread_snapshot", side_effect=[{"text": "Original", "ids": ["N0"]}, {"text": "Original\n\nExact audited reply", "ids": ["N0", "N1"]}]), \
     mock.patch.object(audit, "_run", side_effect=[json.dumps({"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "N1"}}}}),
                                                  json.dumps({"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})]) as ran:
    audit.post_reply("o/x", 23, "github", "T1", "Exact audited reply", "Original")
    assert "body=Exact audited reply" in ran.call_args_list[0].args[0]
with mock.patch.object(audit, "live_thread_snapshot", side_effect=[{"text": "Original", "ids": ["1"]}, {"text": "Original\n\nExact audited reply", "ids": ["1", "42"]}]), \
     mock.patch.object(audit, "_run", side_effect=[json.dumps({"id": 42, "body": "Exact audited reply"}),
                                                  json.dumps({"id": "abc", "notes": [{"resolved": True}]})]) as ran:
    audit.post_reply("o/x", 23, "gitlab", "abc", "Exact audited reply", "Original")
    assert "body=Exact audited reply" in ran.call_args_list[0].args[0]
with mock.patch.object(audit, "live_thread_snapshot", return_value={"text": "Original", "ids": ["N0"]}), mock.patch.object(audit, "_run", return_value=None) as ran:
    try: audit.post_reply("o/x", 23, "github", "uncertain-T", "Exact audited reply", "Original")
    except (ValueError, RuntimeError): pass
    else: assert False, "lost POST response must fail closed"
    try: audit.post_reply("o/x", 23, "github", "uncertain-T", "Exact audited reply", "Original")
    except RuntimeError as e: assert "may have posted" in str(e) and ".pending" in str(e) and "if no reply exists" in str(e)
    else: assert False, "uncertain reply must not be sent twice"
    assert ran.call_count == 1
with mock.patch.object(audit, "live_thread_snapshot", side_effect=[{"text": "Original", "ids": ["N0"]}, {"text": "Original\n\nNew reviewer note\n\nExact audited reply", "ids": ["N0", "N2", "N3"]}]), \
     mock.patch.object(audit, "_run", return_value=json.dumps({"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "N3"}}}})) as ran:
    try: audit.post_reply("o/x", 23, "github", "changed-T", "Exact audited reply", "Original")
    except RuntimeError as e: assert "new reviewer feedback" in str(e)
    else: assert False, "new feedback must remain unresolved"
    assert ran.call_count == 1
review_cm = {"body": "Exact review point", "path": "app.py", "line": 1}
with mock.patch.object(audit, "forge_diff", return_value=DIFF), mock.patch.object(audit, "_run", return_value=json.dumps({"id": 99, "body": "Exact review point", "path": "app.py", "commit_id": "abc"})) as ran:
    assert audit.post_review_comment("o/x", 23, "github", "abc", review_cm) == "99"
    args = ran.call_args.args[0]
    assert "body=Exact review point" in args and "commit_id=abc" in args and "line=1" in args
with mock.patch.object(audit, "forge_diff", return_value=DIFF), mock.patch.object(audit, "_run") as ran:
    try: audit.post_review_comment("o/x", 23, "github", "abc", review_cm)
    except RuntimeError as e: assert "already posted" in str(e)
    else: assert False, "confirmed review comment must not post twice"
    assert not ran.called
with mock.patch.object(audit, "forge_diff", return_value=DIFF), mock.patch.object(audit, "_run", side_effect=[json.dumps({"sha": "abc", "diff_refs": {"base_sha": "base", "start_sha": "start", "head_sha": "abc"}}),
                                                  json.dumps({"id": "D1", "notes": [{"body": "Exact review point"}]})]) as ran:
    assert audit.post_review_comment("o/x", 23, "gitlab", "abc", review_cm) == "D1"
    assert "position[head_sha]=abc" in ran.call_args_list[1].args[0] and "body=Exact review point" in ran.call_args_list[1].args[0]
rename_diff = "diff --git a/old.py b/new.py\nrename from old.py\nrename to new.py\n--- a/old.py\n+++ b/new.py\n@@ -1,2 +1,2 @@\n-old\n+new\n context\n"
assert audit.review_position(rename_diff, "new.py", 1) == {"old_path": "old.py", "new_path": "new.py", "old_line": None, "new_line": 1}
assert audit.review_position(rename_diff, "old.py", 1, "LEFT") == {"old_path": "old.py", "new_path": "new.py", "old_line": 1, "new_line": None}
assert audit.review_position(rename_diff, "new.py", 2) == {"old_path": "old.py", "new_path": "new.py", "old_line": 2, "new_line": 2}
try: audit.review_position(rename_diff, "new.py", 50)
except ValueError: pass
else: assert False, "an unbound line must be rejected before posting"
with mock.patch.object(audit, "forge_diff", return_value=rename_diff), mock.patch.object(audit, "_run", side_effect=[json.dumps({"sha": "abc", "diff_refs": {"base_sha": "base", "start_sha": "start", "head_sha": "abc"}}),
                                                  json.dumps({"id": "D2", "notes": [{"body": "Exact review point"}]})]) as ran:
    assert audit.post_review_comment("o/x", 23, "gitlab", "abc", {"body": "Exact review point", "path": "old.py", "line": 1, "side": "LEFT"}) == "D2"
    args = ran.call_args_list[1].args[0]
    assert "position[old_path]=old.py" in args and "position[new_path]=new.py" in args
    assert "position[old_line]=1" in args and not any("position[new_line]" in x for x in args)
print("cli ok")
g("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
assert audit.default_target(wt) == "main"
with mock.patch.object(audit, "git", return_value="ref: refs/heads/develop\tHEAD\n"):
    assert audit.default_target(wt) == "develop"  # remote default wins over local main
with mock.patch.object(audit, "git", return_value=""):
    assert audit.default_target(wt) is None  # unknown remote default cannot silently become main
print("ok")
