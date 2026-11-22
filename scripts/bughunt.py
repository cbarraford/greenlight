#!/usr/bin/env python3
"""bughunt: Nightly autonomous bug hunter, regression catcher, and Draft PR patcher for Greenlight.

Runs an isolated copy of Greenlight (--uitest) across stress/chaos fixtures + recent commits,
collects and ranks ALL discovered issues by priority, and opens Draft PRs strictly for the top
N (default: 2) highest-priority confirmed, deduplicated bugs — each gated on a failing regression
test first. Designed to run unattended on a Mac mini (via launchd + caffeinate) or locally.

CLI usage:
    python3 scripts/bughunt.py run [--max-prs 2] [--dry-run] [--since-hours 72] [--soak-secs 90]
                                   [--fuzz-steps 60] [--fixtures default,empty,adversarial,scale,chaos,corrupt]
                                   [--skip-baseline] [--skip-llm] [--with-vm] [--base origin/main]
    python3 scripts/bughunt.py fuzz [--fixture default] [--seed 42] [--steps 60] [--soak-secs 0]
    python3 scripts/bughunt.py replay <trace.jsonl> [--shrink] [--out minimal.jsonl]
    python3 scripts/bughunt.py install [--host SSH_HOST] [--repo PATH] [--hour 2] [--minute 30]
                                       [--max-prs 2] [--with-vm] [--uninstall]
    python3 scripts/bughunt.py run-remote --host SSH_HOST [--repo PATH] [--dry-run] [--max-prs 2]
    python3 scripts/bughunt.py status [--host SSH_HOST]
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import random
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import diag  # noqa: E402
import uitest  # noqa: E402

try:
    import harness  # noqa: E402
    from greenlight import CONFIG, LIMIT_RE  # noqa: E402
except Exception:  # pragma: no cover - allows standalone import in minimal envs
    harness = None
    CONFIG = {"agents": ["claude", "codex"]}
    LIMIT_RE = re.compile(r"rate.?limit|429|usage limit|limit reached", re.I)

PR_LABEL = "nightly-bug-hunter"
LAUNCHD_LABEL = "greenlight-bughunt"
DEFAULT_FIXTURES = ("default", "empty", "adversarial", "scale", "chaos", "corrupt")
DEFAULT_VIEWPORTS = ((760, 520), (1300, 900), (1680, 1020), (1100, 480))
FUZZ_INPUTS = (
    "status",
    "park the auth ones",
    "x" * 240,
    '"><img src=x onerror="window.__pwned=1">',
    "unicode-\u00e9-\u4e2d\u6587-\U0001f680-\u202e-rtl",
    "quote's \"double\" \\backslash\\ `tick` $(echo nope)",
    "/clear",
    "",
)

# Priority base scores: higher = more important. Hard oracles are eligible for autonomous Draft PRs.
ORACLE_SCORES = {
    "security": 100,
    "crash": 95,
    "baseline_regression": 90,
    "hang": 85,
    "python_exception": 80,
    "js_error": 75,
    "commit_regression": 72,
    "process_leak": 65,
    "perf_regression": 60,
    "console_error": 50,
    "layout_issue": 30,
    "visual_issue": 25,
}
HARD_ORACLES = frozenset({
    "security",
    "crash",
    "baseline_regression",
    "hang",
    "python_exception",
    "js_error",
    "commit_regression",
    "process_leak",
    "perf_regression",
    "console_error",
})

# Normalization patterns for stable cross-run fingerprinting.
_NORM_PATTERNS = (
    (re.compile(r"gl-uitest-[0-9a-f]{6,16}"), "gl-uitest-<id>"),
    (re.compile(r"/var/folders/[^\s'\")]+"), "<tmp>"),
    (re.compile(r"/tmp/[^\s'\")]+"), "<tmp>"),  # noqa: S108 - regex matching temp paths in error messages
    (re.compile(r"0x[0-9a-fA-F]{4,}"), "0x<hex>"),
    (re.compile(r"\bpid[=:\s]+\d+", re.I), "pid=<n>"),
    (re.compile(r"\b\d{10,13}\b"), "<ts>"),
    (re.compile(r"\s+"), " "),
)


def bughunt_home() -> Path:
    base = Path(os.environ.get("GREENLIGHT_HOME") or (Path.home() / ".greenlight")).expanduser()
    return base / "bughunt"


def normalize_error(text: str) -> str:
    s = str(text or "").strip()
    for rx, repl in _NORM_PATTERNS:
        s = rx.sub(repl, s)
    return s[:220].strip()


def make_fingerprint(oracle: str, summary: str, where: str = "") -> str:
    key = f"{oracle}|{normalize_error(summary)}|{normalize_error(where)}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def score_finding(f: dict) -> int:
    """Compute deterministic priority score so the top-2 Draft PR selection picks highest-impact bugs."""
    base = ORACLE_SCORES.get(f.get("oracle", ""), 20)
    bonus = 0
    if f.get("reproducible"):
        bonus += 10
    if f.get("recent_commit_overlap"):
        bonus += 8
    fixtures = f.get("fixtures") or ([f["fixture"]] if f.get("fixture") else [])
    if any(fx in ("default", "empty") for fx in fixtures):
        bonus += 5
    if len(set(fixtures)) > 1:
        bonus += min(9, 3 * (len(set(fixtures)) - 1))
    return base + bonus


def make_finding(
    *,
    oracle: str,
    track: str,
    title: str,
    summary: str,
    fixture: str = "default",
    details: str = "",
    where: str = "",
    reproducible: bool = False,
    files: list | None = None,
    trace_path: str | None = None,
    screenshot_path: str | None = None,
    recent_commit_overlap: bool = False,
) -> dict:
    fp = make_fingerprint(oracle, summary, where)
    auto_pr = (oracle in HARD_ORACLES) and bool(reproducible)
    f = {
        "fingerprint": fp,
        "oracle": oracle,
        "track": track,
        "fixture": fixture,
        "fixtures": [fixture] if fixture else [],
        "title": title[:140],
        "summary": summary[:400],
        "details": details[:4000],
        "where": where[:160],
        "reproducible": bool(reproducible),
        "auto_pr": auto_pr,
        "files": list(files or []),
        "trace_path": str(trace_path) if trace_path else None,
        "screenshot_path": str(screenshot_path) if screenshot_path else None,
        "recent_commit_overlap": bool(recent_commit_overlap),
    }
    f["priority"] = score_finding(f)
    return f


def merge_and_rank_findings(findings: list[dict], recent_files: set[str] | None = None) -> list[dict]:
    """Merge duplicate findings discovered across tracks/fixtures and sort strictly by priority desc."""
    recent_files = recent_files or set()
    by_fp: dict[str, dict] = {}
    for raw in findings:
        f = dict(raw)
        fp = f.get("fingerprint") or make_fingerprint(f.get("oracle", ""), f.get("summary", ""), f.get("where", ""))
        f["fingerprint"] = fp
        if not f.get("recent_commit_overlap") and recent_files:
            hit_files = set(f.get("files") or [])
            where = f.get("where") or ""
            if (hit_files & recent_files) or any(Path(rf).name in where for rf in recent_files):
                f["recent_commit_overlap"] = True
        if fp not in by_fp:
            f["fixtures"] = sorted(set(f.get("fixtures") or ([f["fixture"]] if f.get("fixture") else [])))
            f["tracks"] = sorted({f.get("track", "unknown")})
            f["auto_pr"] = (f.get("oracle") in HARD_ORACLES) and bool(f.get("reproducible"))
            f["priority"] = score_finding(f)
            by_fp[fp] = f
        else:
            cur = by_fp[fp]
            cur["fixtures"] = sorted(set(cur.get("fixtures", [])) | set(f.get("fixtures") or ([f["fixture"]] if f.get("fixture") else [])))
            cur["tracks"] = sorted(set(cur.get("tracks", [])) | {f.get("track", "unknown")})
            cur["files"] = sorted(set(cur.get("files", [])) | set(f.get("files") or []))
            cur["reproducible"] = bool(cur.get("reproducible") or f.get("reproducible"))
            cur["recent_commit_overlap"] = bool(cur.get("recent_commit_overlap") or f.get("recent_commit_overlap"))
            if not cur.get("trace_path") and f.get("trace_path"):
                cur["trace_path"] = f["trace_path"]
            if not cur.get("screenshot_path") and f.get("screenshot_path"):
                cur["screenshot_path"] = f["screenshot_path"]
            if len(f.get("details", "")) > len(cur.get("details", "")):
                cur["details"] = f["details"]
            cur["auto_pr"] = (cur.get("oracle") in HARD_ORACLES) and bool(cur.get("reproducible"))
            cur["priority"] = score_finding(cur)
    ranked = sorted(by_fp.values(), key=lambda x: (-x["priority"], x["fingerprint"]))
    return ranked


def select_pr_candidates(
    ranked_findings: list[dict],
    existing_fps: set[str] | None = None,
    known_fps: set[str] | None = None,
) -> list[dict]:
    """Return eligible, deduplicated findings in strict descending priority order."""
    skip = set(existing_fps or ()) | set(known_fps or ())
    return [
        f for f in ranked_findings
        if f.get("auto_pr") and f.get("fingerprint") not in skip
    ]


# ---------------------------------------------------------------------------
# Deduplication helpers (open PRs + known-issues.json)
# ---------------------------------------------------------------------------

def load_known_fingerprints(home_dir: Path | None = None) -> set[str]:
    p = (home_dir or bughunt_home()) / "known-issues.json"
    if not p.is_file():
        return set()
    try:
        data = json.loads(p.read_text())
        if isinstance(data, list):
            return {str(x) for x in data}
        if isinstance(data, dict):
            return {str(x) for x in data.get("fingerprints", [])}
    except (OSError, ValueError):
        pass
    return set()


def fetch_open_pr_fingerprints(repo_dir: Path = ROOT) -> set[str]:
    """Inspect open nightly-bug-hunter PRs so we never open duplicate PRs for the same fingerprint."""
    if not shutil.which("gh"):
        return set()
    try:
        p = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--label", PR_LABEL, "--json", "number,body,headRefName"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=20,
        )
        if p.returncode != 0 or not p.stdout.strip():
            return set()
        prs = json.loads(p.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()
    fps = set()
    fp_re = re.compile(r"bughunt-fingerprint:\s*([0-9a-f]{8,16})", re.I)
    branch_re = re.compile(r"bughunt/\d{4}-\d{2}-\d{2}-([0-9a-f]{8,16})")
    for pr in prs:
        m = fp_re.search(pr.get("body") or "")
        if m:
            fps.add(m.group(1).lower())
        bm = branch_re.search(pr.get("headRefName") or "")
        if bm:
            fps.add(bm.group(1).lower())
    return fps


# ---------------------------------------------------------------------------
# Recent commits helper (bridges Track B & priority scoring)
# ---------------------------------------------------------------------------

def get_recent_commits(repo_dir: Path = ROOT, since_hours: int = 72) -> tuple[list[dict], set[str]]:
    """Return (commits, changed_files) over the last `since_hours` hours."""
    try:
        p = subprocess.run(
            ["git", "-C", str(repo_dir), "log", f"--since={since_hours} hours ago",
             "--name-only", "--pretty=format:===%h\t%s"],
            capture_output=True, text=True, timeout=15,
        )
        if p.returncode != 0:
            return [], set()
    except (OSError, subprocess.SubprocessError):
        return [], set()

    commits, files = [], set()
    cur = None
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("==="):
            sha, _, subj = line[3:].partition("\t")
            cur = {"sha": sha, "subject": subj, "files": []}
            commits.append(cur)
        elif cur is not None:
            cur["files"].append(line)
            files.add(line)
    return commits, files


# ---------------------------------------------------------------------------
# Track 0: Baseline test/lint/typecheck gate
# ---------------------------------------------------------------------------

def _python_cmd() -> list[str]:
    return ["uv", "run", "python"] if shutil.which("uv") else [sys.executable]


def _has_playwright() -> bool:
    if not shutil.which("node"):
        return False
    p = subprocess.run(["node", "-e", "require.resolve('playwright-core')"], capture_output=True)
    return p.returncode == 0


def run_baseline(repo_dir: Path = ROOT) -> list[dict]:
    """Run existing lint, safety, unit, JS, typecheck, and UI tests to catch direct regressions."""
    findings = []
    checks = []
    if shutil.which("ruff"):
        checks.append(("ruff-lint", ["ruff", "check", "scripts/", "tests/"], 120))
    checks.append(("safety-check", [*_python_cmd(), "scripts/check_safety.py"], 60))
    checks.append(("test-py", ["make", "test-py"], 300))
    if _has_playwright():
        checks.append(("test-js", ["make", "test-js"], 300))
    if sys.platform == "darwin":
        checks.append(("swift-typecheck", ["make", "typecheck"], 180))
        checks.append(("test-ui", ["make", "test-ui"], 240))

    with diag.span("bughunt", phase="baseline"):
        for name, cmd, timeout in checks:
            try:
                p = subprocess.run(cmd, cwd=str(repo_dir), capture_output=True, text=True, timeout=timeout)
                rc, out = p.returncode, (p.stdout + "\n" + p.stderr).strip()
            except subprocess.TimeoutExpired as e:
                rc = 124
                out = f"Timed out after {timeout}s\n{(e.stdout or '')}"
            except OSError as e:
                rc, out = 127, str(e)
            if rc != 0:
                tail = "\n".join(out.splitlines()[-40:])
                findings.append(make_finding(
                    oracle="baseline_regression",
                    track="baseline",
                    fixture="baseline",
                    title=f"Baseline check `{name}` failed on main (exit {rc})",
                    summary=f"Command `{' '.join(cmd)}` failed with exit {rc}",
                    details=tail,
                    where=name,
                    reproducible=True,
                ))
    return findings


# ---------------------------------------------------------------------------
# Track A: Isolated --uitest Chaos Fuzzer, Perf Soak, Replay & Trace Shrinker
# ---------------------------------------------------------------------------

_DISCOVER_CLICKABLES_JS = """(() => {
  const sels = [];
  const els = document.querySelectorAll('button, [role="button"], .cell, .tab, .pill, [data-action]');
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.pointerEvents === 'none') continue;
    if (el.disabled) continue;
    if (el.id) { sels.push('#' + CSS.escape(el.id)); continue; }
    const act = el.getAttribute('data-action');
    if (act) { sels.push(`[data-action="${act.replace(/"/g, '')}"]`); continue; }
    const name = el.getAttribute('data-name');
    if (name && el.classList.contains('cell')) { sels.push(`.cell[data-name="${name.replace(/"/g, '')}"]`); continue; }
    if (el.classList.contains('tab') && el.dataset.id) { sels.push(`.tab[data-id="${el.dataset.id.replace(/"/g, '')}"]`); continue; }
  }
  return [...new Set(sels)].slice(0, 40);
})()"""

_DISCOVER_INPUTS_JS = """(() => {
  const sels = [];
  for (const el of document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea')) {
    const r = el.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || el.disabled || el.readOnly) continue;
    if (el.id) sels.push('#' + CSS.escape(el.id));
    else if (el.className && typeof el.className === 'string') {
      const cls = el.className.trim().split(/\\s+/)[0];
      if (cls) sels.push(el.tagName.toLowerCase() + '.' + CSS.escape(cls));
    }
  }
  return [...new Set(sels)].slice(0, 15);
})()"""

_LAYOUT_CHECK_JS = """(() => {
  const issues = [];
  const vw = window.innerWidth, vh = window.innerHeight;
  if (vw > 0 && document.documentElement.scrollWidth > vw + 48) {
    issues.push(`horizontal overflow: scrollWidth=${document.documentElement.scrollWidth} > innerWidth=${vw}`);
  }
  const main = document.querySelector('#main, #term, body');
  if (main) {
    const r = main.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) issues.push(`collapsed root container ${main.id || 'body'}`);
  }
  return issues;
})()"""


def _read_isolated_diag_errors(home: Path) -> list[dict]:
    """Read isolated $GREENLIGHT_HOME/diag/{events,app}.jsonl for hangs, uncaught errors, dash_errors."""
    hits = []
    for fname in ("events.jsonl", "app.jsonl"):
        p = home / "diag" / fname
        if not p.is_file():
            continue
        try:
            lines = p.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("kind")
            if kind == "hang":
                hits.append({
                    "oracle": "hang",
                    "summary": f"Main-thread hang ({ev.get('secs')}s) in {ev.get('comp', 'app')}",
                    "where": str(ev.get("where") or ev.get("comp") or "app"),
                    "details": json.dumps(ev),
                })
            elif kind in ("uncaught", "dash_error"):
                where = ev.get("where")
                where_s = where[-1] if isinstance(where, list) and where else str(where or ev.get("comp") or "")
                hits.append({
                    "oracle": "python_exception",
                    "summary": f"{kind}: {ev.get('err', 'error')} ({ev.get('msg_class', 'other')}) in {ev.get('comp', '?')}",
                    "where": where_s,
                    "details": json.dumps(ev),
                })
            elif kind == "js_error":
                hits.append({
                    "oracle": "js_error",
                    "summary": f"App-logged js_error on {ev.get('page', 'unknown')}",
                    "where": str(ev.get("page") or "webview"),
                    "details": json.dumps(ev),
                })
    return hits


def _check_session_oracles(sess: uitest.UITestSession, fixture: str, track: str, seen_keys: set[str],
                           shot_dir: Path | None = None) -> tuple[list[dict], dict]:
    """Poll a live --uitest session for crashes, JS/console errors, XSS, diag errors, and metrics."""
    new_findings = []
    metrics = {}
    if not sess.is_alive():
        rc = sess.proc.returncode if sess.proc else -1
        key = f"crash:{rc}"
        if key not in seen_keys:
            seen_keys.add(key)
            log_tail = ""
            log_p = sess.root / "app.log"
            if log_p.is_file():
                log_tail = log_p.read_text(errors="replace")[-1500:]
            new_findings.append(make_finding(
                oracle="crash",
                track=track,
                fixture=fixture,
                title=f"Greenlight exited unexpectedly under `{fixture}` fixture (rc={rc})",
                summary=f"Greenlight --uitest process exited unexpectedly with code {rc}",
                details=log_tail,
                where="app",
                reproducible=False,  # upgraded to True after replay confirms
            ))
        return new_findings, metrics

    t0 = time.monotonic()
    try:
        st = sess.state()
    except OSError as e:
        key = f"hang:rpc:{type(e).__name__}"
        if key not in seen_keys:
            seen_keys.add(key)
            new_findings.append(make_finding(
                oracle="hang",
                track=track,
                fixture=fixture,
                title=f"Greenlight UI RPC stalled/disconnected under `{fixture}` fixture",
                summary=f"RPC state() failed: {type(e).__name__}: {e}",
                where="UITestServer",
                reproducible=False,
            ))
        return new_findings, metrics
    rpc_ms = round((time.monotonic() - t0) * 1000.0, 1)

    if st.get("ok"):
        metrics = dict(st.get("metrics") or {})
        metrics["rpcMs"] = rpc_ms
        if rpc_ms > 2500.0:
            key = "hang:slow_rpc"
            if key not in seen_keys:
                seen_keys.add(key)
                new_findings.append(make_finding(
                    oracle="hang",
                    track=track,
                    fixture=fixture,
                    title=f"Main-thread stall: state() RPC took {rpc_ms}ms on `{fixture}`",
                    summary=f"UITest state() RPC blocked main thread for {rpc_ms}ms (>2500ms)",
                    where="main-thread",
                    reproducible=False,
                ))

        for page, errs in (st.get("webErrors") or {}).items():
            for err in errs:
                norm = normalize_error(err)
                key = f"js:{page}:{norm}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    shot = _maybe_snap(sess, shot_dir, f"{fixture}-{page}-jserr.png", page)
                    new_findings.append(make_finding(
                        oracle="js_error",
                        track=track,
                        fixture=fixture,
                        title=f"Uncaught JS error in `{page}` ({fixture}): {norm[:80]}",
                        summary=f"[{page}] {err}",
                        where=f"app/{page}.html",
                        files=[f"app/{page}.html"],
                        screenshot_path=shot,
                        reproducible=False,
                    ))

        for page, errs in (st.get("consoleErrors") or {}).items():
            for err in errs:
                norm = normalize_error(err)
                key = f"console:{page}:{norm}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    new_findings.append(make_finding(
                        oracle="console_error",
                        track=track,
                        fixture=fixture,
                        title=f"console.error in `{page}` ({fixture}): {norm[:80]}",
                        summary=f"[{page}] console.error: {err}",
                        where=f"app/{page}.html",
                        files=[f"app/{page}.html"],
                        reproducible=False,
                    ))

        # Security invariant: XSS payload must never execute.
        for page in ("dashboard", "terminal"):
            try:
                pwn = sess.eval(page, "window.__pwned || null", record=False)
                if pwn.get("ok") and pwn.get("result"):
                    key = f"xss:{page}"
                    if key not in seen_keys:
                        seen_keys.add(key)
                        new_findings.append(make_finding(
                            oracle="security",
                            track=track,
                            fixture=fixture,
                            title=f"XSS executed in `{page}` WKWebView ({fixture})",
                            summary=f"window.__pwned became truthy ({pwn['result']}) in {page}",
                            where=f"app/{page}.html",
                            files=[f"app/{page}.html"],
                            reproducible=True,
                        ))
            except OSError:
                break

    if sess.home:
        for ev in _read_isolated_diag_errors(sess.home):
            key = f"diag:{ev['oracle']}:{normalize_error(ev['summary'])}"
            if key not in seen_keys:
                seen_keys.add(key)
                new_findings.append(make_finding(
                    oracle=ev["oracle"],
                    track=track,
                    fixture=fixture,
                    title=f"{ev['oracle']} under `{fixture}`: {ev['summary'][:80]}",
                    summary=ev["summary"],
                    details=ev["details"],
                    where=ev["where"],
                    reproducible=False,
                ))

    return new_findings, metrics


def _maybe_snap(sess: uitest.UITestSession, shot_dir: Path | None, name: str, window: str = "active") -> str | None:
    if shot_dir is None:
        return None
    try:
        shot_dir.mkdir(parents=True, exist_ok=True)
        out = shot_dir / name
        r = sess.screenshot(out, window=window)
        return str(out) if r.get("ok") else None
    except OSError:
        return None


def fuzz_session(
    fixture: str = "default",
    seed: int = 42,
    steps: int = 60,
    binary: Path | None = None,
    run_dir: Path | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """Run a seeded deterministic monkey session on one isolated --uitest instance.

    Returns (findings, trace_steps, summary_metrics).
    """
    rng = random.Random(seed)
    seen_keys: set[str] = set()
    findings: list[dict] = []
    metrics_samples: list[dict] = []
    shot_dir = (run_dir / "screenshots") if run_dir else None
    tmux_sock = None

    with uitest.UITestSession(fixture=fixture, binary=binary, record_trace=True) as sess:
        tmux_sock = sess.tmux_socket
        # Let terminal & dashboard settle initially
        try:
            sess.wait_for("terminal", "Boolean(window.term)", timeout=12)
        except Exception:
            pass
        sess.action("floor", to="center")
        try:
            sess.wait_for("dashboard", "Boolean(document.querySelector('#main'))", timeout=12)
        except Exception:
            pass

        # Initial oracle check
        f0, m0 = _check_session_oracles(sess, fixture, "fuzz", seen_keys, shot_dir)
        findings.extend(f0)
        if m0:
            metrics_samples.append(m0)

        active_page = "dashboard"
        for i in range(steps):
            if not sess.is_alive():
                f_dead, _ = _check_session_oracles(sess, fixture, "fuzz", seen_keys, shot_dir)
                findings.extend(f_dead)
                break

            roll = rng.random()
            try:
                if roll < 0.22:
                    fl = rng.choice(("pipe", "basement", "center"))
                    sess.action("floor", to=fl)
                    active_page = "dashboard" if fl == "center" else "terminal"
                elif roll < 0.34:
                    w, h = rng.choice(DEFAULT_VIEWPORTS)
                    win = "dashboard" if active_page == "dashboard" else "terminal"
                    sess.action("resize", window=win, width=w, height=h)
                elif roll < 0.44:
                    act = rng.choice(("toggleChat", "openSettings", "closeSettings", "rescan", "openPalette", "closePalette", "chatClear"))
                    sess.action(act)
                elif roll < 0.50:
                    sess.action("sendChat", text=rng.choice(("what should I work on next?", "highlight auth", "status")))
                elif roll < 0.78:
                    r = sess.eval(active_page, _DISCOVER_CLICKABLES_JS, record=False)
                    sels = r.get("result") if r.get("ok") and isinstance(r.get("result"), list) else []
                    if sels:
                        sess.click(active_page, rng.choice(sels))
                elif roll < 0.92:
                    r = sess.eval(active_page, _DISCOVER_INPUTS_JS, record=False)
                    sels = r.get("result") if r.get("ok") and isinstance(r.get("result"), list) else []
                    if sels:
                        sess.type_text(active_page, rng.choice(sels), rng.choice(FUZZ_INPUTS))
                else:
                    # Layout check on current page (soft oracle)
                    r = sess.eval(active_page, _LAYOUT_CHECK_JS, record=False)
                    issues = r.get("result") if r.get("ok") and isinstance(r.get("result"), list) else []
                    for iss in issues:
                        key = f"layout:{active_page}:{iss}"
                        if key not in seen_keys:
                            seen_keys.add(key)
                            shot = _maybe_snap(sess, shot_dir, f"{fixture}-{active_page}-layout-{i}.png", active_page)
                            findings.append(make_finding(
                                oracle="layout_issue",
                                track="fuzz",
                                fixture=fixture,
                                title=f"Layout anomaly in `{active_page}` ({fixture}): {iss[:80]}",
                                summary=f"[{active_page}] {iss}",
                                where=f"app/{active_page}.html",
                                files=[f"app/{active_page}.html"],
                                screenshot_path=shot,
                                reproducible=False,
                            ))
            except OSError as e:
                key = f"hang:step:{type(e).__name__}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    findings.append(make_finding(
                        oracle="hang",
                        track="fuzz",
                        fixture=fixture,
                        title=f"RPC failure during fuzz step {i} on `{fixture}`",
                        summary=f"Step {i} raised {type(e).__name__}: {e}",
                        where="UITestServer",
                        reproducible=False,
                    ))
                break

            if (i + 1) % 6 == 0 or i == steps - 1:
                time.sleep(0.08)
                fi, mi = _check_session_oracles(sess, fixture, "fuzz", seen_keys, shot_dir)
                findings.extend(fi)
                if mi:
                    metrics_samples.append(mi)

        trace_steps = list(sess.trace)
        trace_path = None
        if run_dir and findings:
            trace_path = sess.save_trace(run_dir / "traces" / f"fuzz-{fixture}-seed{seed}.jsonl")
            for f in findings:
                f["trace_path"] = str(trace_path)

    # Post-stop process leak oracle: isolated tmux server must be gone
    if tmux_sock:
        cp = subprocess.run(["tmux", "-L", tmux_sock, "list-sessions"], capture_output=True, text=True)
        if cp.returncode == 0 and cp.stdout.strip():
            subprocess.run(["tmux", "-L", tmux_sock, "kill-server"], capture_output=True, timeout=5)
            findings.append(make_finding(
                oracle="process_leak",
                track="fuzz",
                fixture=fixture,
                title=f"Isolated tmux server survived quit() on `{fixture}`",
                summary=f"tmux -L {tmux_sock} still had live sessions after clean quit",
                details=cp.stdout,
                where="scripts/uitest.py",
                reproducible=True,
            ))

    summary = _summarize_metrics(metrics_samples)
    return findings, trace_steps, summary


def _summarize_metrics(samples: list[dict]) -> dict:
    if not samples:
        return {}
    rss = [s["rssMb"] for s in samples if isinstance(s.get("rssMb"), (int, float))]
    fds = [s["openFds"] for s in samples if isinstance(s.get("openFds"), (int, float)) and s["openFds"] >= 0]
    rpcs = [s["rpcMs"] for s in samples if isinstance(s.get("rpcMs"), (int, float))]
    dom_d = [s["domNodes"].get("dashboard", 0) for s in samples if isinstance(s.get("domNodes"), dict)]
    out = {"samples": len(samples)}
    if rss:
        out["rss_start_mb"] = rss[0]
        out["rss_end_mb"] = rss[-1]
        out["rss_max_mb"] = max(rss)
        out["rss_delta_mb"] = round(rss[-1] - rss[0], 1)
    if fds:
        out["fds_start"] = fds[0]
        out["fds_end"] = fds[-1]
        out["fds_delta"] = fds[-1] - fds[0]
    if rpcs:
        rpcs_sorted = sorted(rpcs)
        p95_idx = min(len(rpcs_sorted) - 1, int(len(rpcs_sorted) * 0.95))
        out["rpc_p50_ms"] = round(statistics.median(rpcs_sorted), 1)
        out["rpc_p95_ms"] = round(rpcs_sorted[p95_idx], 1)
    if dom_d:
        out["dom_dashboard_max"] = max(dom_d)
    return out


def load_perf_history(home_dir: Path | None = None, limit: int = 14) -> list[dict]:
    p = (home_dir or bughunt_home()) / "history.jsonl"
    if not p.is_file():
        return []
    rows = []
    for line in p.read_text(errors="replace").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def append_perf_history(record: dict, home_dir: Path | None = None):
    d = home_dir or bughunt_home()
    d.mkdir(parents=True, exist_ok=True)
    with (d / "history.jsonl").open("a") as f:
        f.write(json.dumps(record) + "\n")


def evaluate_perf_oracles(soak_metrics: dict, history: list[dict], fixture: str = "scale") -> list[dict]:
    """Flag clear memory leaks, FD leaks, or rolling-baseline perf regressions."""
    findings = []
    if not soak_metrics:
        return findings

    rss_delta = soak_metrics.get("rss_delta_mb", 0)
    if rss_delta >= 90.0:
        findings.append(make_finding(
            oracle="perf_regression",
            track="soak",
            fixture=fixture,
            title=f"Memory leak during `{fixture}` soak (+{rss_delta} MB RSS)",
            summary=f"RSS grew by +{rss_delta} MB during soak ({soak_metrics.get('rss_start_mb')} -> {soak_metrics.get('rss_end_mb')} MB)",
            details=json.dumps(soak_metrics, indent=2),
            where="app/Sources/App.swift",
            reproducible=True,
        ))

    fds_delta = soak_metrics.get("fds_delta", 0)
    if fds_delta >= 40:
        findings.append(make_finding(
            oracle="perf_regression",
            track="soak",
            fixture=fixture,
            title=f"File descriptor leak during `{fixture}` soak (+{fds_delta} FDs)",
            summary=f"Open FDs grew by +{fds_delta} during soak ({soak_metrics.get('fds_start')} -> {soak_metrics.get('fds_end')})",
            details=json.dumps(soak_metrics, indent=2),
            where="app/Sources/App.swift",
            reproducible=True,
        ))

    prior_rss = [h["soak"]["rss_max_mb"] for h in history if isinstance(h.get("soak"), dict) and "rss_max_mb" in h["soak"]]
    if len(prior_rss) >= 3 and "rss_max_mb" in soak_metrics:
        med = statistics.median(prior_rss)
        if med > 0 and soak_metrics["rss_max_mb"] > med * 1.40 and (soak_metrics["rss_max_mb"] - med) > 40:
            findings.append(make_finding(
                oracle="perf_regression",
                track="soak",
                fixture=fixture,
                title=f"Peak RSS jumped {round((soak_metrics['rss_max_mb'] / med - 1) * 100)}% vs baseline",
                summary=f"Soak peak RSS {soak_metrics['rss_max_mb']} MB exceeds rolling median {round(med, 1)} MB by >40%",
                details=json.dumps({"current": soak_metrics, "baseline_median_rss_mb": med}, indent=2),
                where="app/Sources/App.swift",
                reproducible=True,
            ))

    prior_p95 = [h["soak"]["rpc_p95_ms"] for h in history if isinstance(h.get("soak"), dict) and "rpc_p95_ms" in h["soak"]]
    if len(prior_p95) >= 3 and "rpc_p95_ms" in soak_metrics:
        med_p95 = statistics.median(prior_p95)
        if med_p95 > 0 and soak_metrics["rpc_p95_ms"] > max(med_p95 * 2.0, 400.0):
            findings.append(make_finding(
                oracle="perf_regression",
                track="soak",
                fixture=fixture,
                title=f"UI responsiveness regression: RPC p95 {soak_metrics['rpc_p95_ms']}ms vs {round(med_p95, 1)}ms baseline",
                summary=f"Soak RPC p95 latency {soak_metrics['rpc_p95_ms']}ms is >2x rolling median {round(med_p95, 1)}ms",
                details=json.dumps({"current": soak_metrics, "baseline_median_p95_ms": med_p95}, indent=2),
                where="app/Sources/App.swift",
                reproducible=True,
            ))
    return findings


def run_soak(soak_secs: int = 90, fixture: str = "scale", binary: Path | None = None,
             history: list[dict] | None = None) -> tuple[list[dict], dict]:
    """Sustained churn soak on the scale fixture to catch RSS/FD/DOM leaks and latency regressions."""
    if soak_secs <= 0:
        return [], {}
    seen_keys: set[str] = set()
    findings: list[dict] = []
    samples: list[dict] = []
    deadline = time.monotonic() + soak_secs
    cycle = 0
    with uitest.UITestSession(fixture=fixture, binary=binary, record_trace=False) as sess:
        sess.action("floor", to="center")
        try:
            sess.wait_for("dashboard", "document.querySelectorAll('#main .cell').length >= 10", timeout=15)
        except Exception:
            pass
        while time.monotonic() < deadline and sess.is_alive():
            cycle += 1
            try:
                sess.action("floor", to=("basement" if cycle % 2 else "center"))
                if cycle % 3 == 0:
                    sess.action("rescan")
                if cycle % 4 == 0:
                    w, h = DEFAULT_VIEWPORTS[cycle % len(DEFAULT_VIEWPORTS)]
                    sess.action("resize", window="active", width=w, height=h)
                if cycle % 5 == 0:
                    sess.action("toggleChat")
            except OSError:
                pass
            time.sleep(min(2.0, max(0.2, deadline - time.monotonic())))
            f_i, m_i = _check_session_oracles(sess, fixture, "soak", seen_keys)
            findings.extend(f_i)
            if m_i:
                samples.append(m_i)
    summary = _summarize_metrics(samples)
    findings.extend(evaluate_perf_oracles(summary, history or [], fixture=fixture))
    return findings, summary


def replay_trace(
    trace_steps: list[dict],
    fixture: str = "default",
    binary: Path | None = None,
    target_oracle: str | None = None,
    target_fingerprint: str | None = None,
) -> tuple[bool, list[dict]]:
    """Replay a recorded trace on a fresh isolated --uitest instance and check if the bug reproduces."""
    seen: set[str] = set()
    findings: list[dict] = []
    with uitest.UITestSession(fixture=fixture, binary=binary, record_trace=False) as sess:
        for step in trace_steps:
            if not sess.is_alive():
                break
            op = step.get("op")
            args = step.get("args") or {}
            try:
                if op == "action":
                    name = args.get("name")
                    extra = {k: v for k, v in args.items() if k != "name"}
                    sess.action(name, **extra)
                elif op == "click":
                    sess.click(args.get("target", "dashboard"), args.get("selector", ""))
                elif op == "type":
                    sess.type_text(args.get("target", "dashboard"), args.get("selector", ""), args.get("text", ""))
                elif op == "eval":
                    sess.eval(args.get("target", "dashboard"), args.get("js", "null"), record=False)
            except OSError:
                break
            time.sleep(0.05)
        f_end, _ = _check_session_oracles(sess, fixture, "replay", seen)
        findings.extend(f_end)

    matched = False
    for f in findings:
        if target_fingerprint and f["fingerprint"] == target_fingerprint:
            matched = True
            break
        if target_oracle and f["oracle"] == target_oracle:
            matched = True
            break
        if not target_oracle and not target_fingerprint and f["oracle"] in HARD_ORACLES:
            matched = True
            break
    return matched, findings


def shrink_trace(steps: list[dict], check_fn, max_attempts: int = 24) -> list[dict]:
    """Delta-debugging (ddmin) trace minimizer: shrinks a failing trace to the smallest reproducer."""
    if len(steps) <= 2:
        return list(steps)
    cur = list(steps)
    n = 2
    attempts = 0
    while len(cur) >= 2 and attempts < max_attempts:
        # Partition cur into n balanced slices
        bounds = [(i * len(cur)) // n for i in range(n + 1)]
        slices = [(bounds[i], bounds[i + 1]) for i in range(n) if bounds[i] < bounds[i + 1]]
        reduced = False
        # At n == 2, test each half first (fast binary search when bug lives in one half)
        if n == 2:
            for lo, hi in slices:
                if attempts >= max_attempts:
                    break
                cand = cur[lo:hi]
                if not cand or len(cand) == len(cur):
                    continue
                attempts += 1
                if check_fn(cand):
                    cur = cand
                    n = 2
                    reduced = True
                    break
        if reduced:
            continue
        # Remove one slice at a time (complement) — converges fast for multi-step bugs
        for lo, hi in slices:
            if attempts >= max_attempts:
                break
            cand = cur[:lo] + cur[hi:]
            if not cand or len(cand) == len(cur):
                continue
            attempts += 1
            if check_fn(cand):
                cur = cand
                n = max(n - 1, 2)
                reduced = True
                break
        if not reduced:
            if n >= len(cur):
                break
            n = min(len(cur), n * 2)
    return cur


def load_trace_file(path: Path) -> tuple[str, list[dict]]:
    fixture = "default"
    steps = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if obj.get("meta"):
            fixture = obj.get("fixture", "default")
        else:
            steps.append(obj)
    return fixture, steps


def save_trace_file(path: Path, fixture: str, steps: list[dict]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        f.write(json.dumps({"meta": True, "fixture": fixture}) + "\n")
        for idx, s in enumerate(steps):
            rec = dict(s)
            rec["seq"] = idx
            f.write(json.dumps(rec) + "\n")
    return p


def run_fuzz_matrix(
    fixtures: tuple[str, ...] = DEFAULT_FIXTURES,
    seed: int = 42,
    steps: int = 60,
    soak_secs: int = 90,
    workers: int = 2,
    binary: Path | None = None,
    run_dir: Path | None = None,
    history: list[dict] | None = None,
) -> tuple[list[dict], dict]:
    """Run parallel fuzz sessions across the fixture matrix + scale soak, replaying & shrinking hard hits."""
    if sys.platform != "darwin":
        return [], {}

    all_findings: list[dict] = []
    traces_by_fixture: dict[str, list[dict]] = {}
    fuzz_metrics: dict[str, dict] = {}
    binary = binary or uitest.build_test_binary()

    with diag.span("bughunt", phase="fuzz"):
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(fixtures)))) as pool:
            futs = {
                pool.submit(fuzz_session, fx, seed + idx * 17, steps, binary, run_dir): fx
                for idx, fx in enumerate(fixtures)
            }
            for fut in as_completed(futs):
                fx = futs[fut]
                try:
                    f_list, tr, m = fut.result()
                    all_findings.extend(f_list)
                    traces_by_fixture[fx] = tr
                    fuzz_metrics[fx] = m
                except Exception as e:
                    all_findings.append(make_finding(
                        oracle="crash",
                        track="fuzz",
                        fixture=fx,
                        title=f"UITest harness failed to launch/run `{fx}` fixture",
                        summary=f"{type(e).__name__}: {e}",
                        where="scripts/uitest.py",
                        reproducible=True,
                    ))

        # Verify & shrink hard-oracle hits from fuzzing so only deterministic bugs become PR candidates.
        # Track confirmed_fps and (fp, fx) attempts separately so a flaky hit on chaos still replays on default/scale.
        confirmed_fps: set[str] = set()
        attempted_pairs: set[tuple[str, str]] = set()
        for f in all_findings:
            if f["oracle"] not in HARD_ORACLES or f["reproducible"]:
                continue
            fp = f["fingerprint"]
            fx = f["fixture"]
            if fp in confirmed_fps or (fp, fx) in attempted_pairs:
                continue
            attempted_pairs.add((fp, fx))
            tr = traces_by_fixture.get(fx)
            if not tr:
                continue
            ok, _ = replay_trace(tr, fixture=fx, binary=binary, target_fingerprint=fp)
            if ok:
                confirmed_fps.add(fp)
                f["reproducible"] = True
                f["auto_pr"] = True
                f["priority"] = score_finding(f)
                minimal = shrink_trace(
                    tr,
                    lambda cand, _fx=fx, _fp=fp: replay_trace(cand, fixture=_fx, binary=binary, target_fingerprint=_fp)[0],
                    max_attempts=14,
                )
                if run_dir:
                    min_path = save_trace_file(run_dir / "traces" / f"minimal-{fx}-{fp}.jsonl", fx, minimal)
                    f["trace_path"] = str(min_path)

    soak_findings, soak_metrics = [], {}
    if soak_secs > 0:
        with diag.span("bughunt", phase="soak"):
            soak_findings, soak_metrics = run_soak(
                soak_secs=soak_secs, fixture="scale", binary=binary, history=history,
            )
            all_findings.extend(soak_findings)

    return all_findings, {"fuzz": fuzz_metrics, "soak": soak_metrics}


# ---------------------------------------------------------------------------
# Headless agent runner (uses Greenlight's harness.py with limit fallback)
# ---------------------------------------------------------------------------

def run_agent_with_fallback(prompt: str, cwd: Path, timeout_mins: int = 20, agents: list[str] | None = None) -> tuple[str, int, str]:
    """Run an agent prompt across configured harnesses, falling back automatically on usage limits."""
    if harness is None:
        return "harness module unavailable", 127, "none"
    engines = list(agents or CONFIG.get("agents") or ["claude", "codex"])
    last_out, last_rc, last_eng = "", 127, engines[0] if engines else "none"
    for eng in engines:
        last_eng = eng
        try:
            cmd, stdin_text = harness.argv(eng, prompt, stream=False)
            p = subprocess.run(
                cmd, input=stdin_text, cwd=str(cwd), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=timeout_mins * 60,
            )
            last_out, last_rc = p.stdout or "", p.returncode
        except subprocess.TimeoutExpired as e:
            last_out = ((e.stdout or b"") if isinstance(e.stdout, bytes) else (e.stdout or ""))
            if isinstance(last_out, bytes):
                last_out = last_out.decode(errors="replace")
            last_out += "\n[timed out]"
            last_rc = 124
        except (ValueError, OSError) as e:
            last_out, last_rc = str(e), 127
            continue
        if last_rc == 0 or not LIMIT_RE.search(last_out):
            return last_out, last_rc, eng
    return last_out, last_rc, last_eng


def parse_bughunt_json(text: str) -> list[dict]:
    """Extract structured findings from a fenced ```bughunt-json ... ``` block in agent output."""
    m = re.search(r"```bughunt-json\s*(.*?)\s*```", text, re.S | re.I)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Track B: Diff-Aware Commit Regression LLM Explorer
# ---------------------------------------------------------------------------

def run_llm_explorer(repo_dir: Path, commits: list[dict], recent_files: set[str], run_dir: Path) -> list[dict]:
    """Ask a headless coding agent to exercise recently changed flows against a live --uitest app."""
    if sys.platform != "darwin":
        return []
    commit_lines = "\n".join(f"- {c['sha']} {c['subject']} ({', '.join(c['files'][:6])})" for c in commits[:15])
    if not commit_lines:
        commit_lines = "- (No commits in window — focus on long-tail interactive UI flows: look dialog, palette, diff, settings, chat)"

    shots = run_dir / "screenshots" / "llm"
    shots.mkdir(parents=True, exist_ok=True)
    prompt = f"""You are Greenlight's nightly QA & regression hunter running in READ-ONLY exploration mode.
Do NOT modify any repository files.

Recent commits to scrutinize for regressions:
{commit_lines}

Instructions:
1. Launch an isolated background app instance with:
   `python3 scripts/uitest.py start --fixture default` (and optionally `--fixture scale` or `--fixture adversarial`).
   Save the returned socket path. NEVER touch the real `~/.greenlight`.
2. Drive the app through `scripts/uitest.py` (`action`, `click`, `type`, `eval`, `state`, `screenshot`) to thoroughly test:
   - The UI/backend features touched by the recent commits above.
   - Edge cases (empty states, resizing, switching Pipe <-> Basement <-> Greenlight Center <-> Settings, opening/closing dialogs).
3. Capture screenshots into `{shots}` when you spot visual/layout issues or after key state transitions.
4. ALWAYS stop every started instance cleanly with `python3 scripts/uitest.py stop --socket <SOCK>` before finishing.
5. Report ONLY concrete, observed bugs (zero speculation). End your response with a single fenced JSON block:

```bughunt-json
[
  {{
    "title": "Short bug title",
    "oracle": "commit_regression | js_error | console_error | hang | layout_issue | visual_issue | security",
    "fixture": "default",
    "summary": "Exact observed failure or error message",
    "where": "app/dashboard.html or scripts/dash.py:123",
    "files": ["app/dashboard.html"],
    "reproducible": true,
    "details": "Exact uitest.py commands that reproduced it and observed output"
  }}
]
```
If no real bugs are found, output `[]` inside the `bughunt-json` block.
"""
    with diag.span("bughunt", phase="llm_explorer"):
        out, rc, eng = run_agent_with_fallback(prompt, cwd=repo_dir, timeout_mins=15)
        (run_dir / "llm-explorer.log").write_text(f"engine={eng} rc={rc}\n\n{out}")

    raw_items = parse_bughunt_json(out)
    findings = []
    for item in raw_items:
        if not isinstance(item, dict) or not item.get("summary"):
            continue
        oracle = str(item.get("oracle") or "visual_issue")
        if oracle not in ORACLE_SCORES:
            oracle = "visual_issue"
        files = item.get("files") if isinstance(item.get("files"), list) else []
        findings.append(make_finding(
            oracle=oracle,
            track="llm_diff",
            fixture=str(item.get("fixture") or "default"),
            title=str(item.get("title") or item["summary"][:100]),
            summary=str(item["summary"]),
            details=str(item.get("details") or ""),
            where=str(item.get("where") or ""),
            files=[str(x) for x in files],
            reproducible=bool(item.get("reproducible")),
            recent_commit_overlap=bool(commits) or bool(set(files) & recent_files),
        ))
    return findings


# ---------------------------------------------------------------------------
# Optional Tier-2 macOS VM HID smoke check
# ---------------------------------------------------------------------------

def run_vm_hid(repo_dir: Path, run_dir: Path) -> list[dict]:
    """Run optional Tier-2 macOS VM HID smoke via scripts/vm_test.sh if tart or lume is available."""
    if sys.platform != "darwin" or not (shutil.which("tart") or shutil.which("lume")):
        return []
    out_dir = run_dir / "vm-shots"
    out_dir.mkdir(parents=True, exist_ok=True)
    with diag.span("bughunt", phase="vm_hid"):
        try:
            p = subprocess.run(
                ["bash", str(repo_dir / "scripts" / "vm_test.sh"), "--run", "scripts/vm_hid_smoke.sh", "--out", str(out_dir)],
                cwd=str(repo_dir), capture_output=True, text=True, timeout=900,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return [make_finding(
                oracle="crash", track="vm", fixture="vm-hid",
                title="Tier-2 VM HID run errored", summary=str(e),
                where="scripts/vm_hid_smoke.sh", reproducible=False,
            )]
        if p.returncode != 0:
            return [make_finding(
                oracle="baseline_regression", track="vm", fixture="vm-hid",
                title=f"Tier-2 VM HID smoke failed (exit {p.returncode})",
                summary=f"vm_hid_smoke.sh exited with {p.returncode}",
                details=(p.stdout + "\n" + p.stderr)[-3000:],
                where="scripts/vm_hid_smoke.sh", reproducible=True,
            )]
    return []


# ---------------------------------------------------------------------------
# Stage 4: Repro-First Autonomous Draft PR Patcher (Top-N Priority Cap)
# ---------------------------------------------------------------------------

def verify_patch_worktree(wt_dir: Path, finding: dict, base_ref: str = "HEAD~1") -> tuple[bool, str]:
    """Independent host-side verification gate before pushing or opening a Draft PR."""
    notes = []
    # 1. Must have a real diff across all commits since base_ref AND at least one test change under tests/
    st = subprocess.run(["git", "-C", str(wt_dir), "status", "--porcelain"], capture_output=True, text=True)
    changed = [line[3:].strip() for line in st.stdout.splitlines() if line.strip()]
    diff_committed = subprocess.run(
        ["git", "-C", str(wt_dir), "diff", "--name-only", f"{base_ref}..HEAD"], capture_output=True, text=True,
    )
    committed_files = [l.strip() for l in diff_committed.stdout.splitlines() if l.strip()] if diff_committed.returncode == 0 else []
    all_touched = sorted(set(changed) | set(committed_files))
    if not all_touched:
        return False, "No code changes produced by patcher."
    if not any(p.startswith("tests/") for p in all_touched):
        return False, f"Repro-first gate failed: no test file modified/added in tests/ (touched: {all_touched})."
    notes.append(f"Touched files ({len(all_touched)}): {', '.join(all_touched[:10])}")

    # 2. Static safety, lint, release leak check, Python tests, Swift typecheck
    gates = [
        ("check_safety", [*_python_cmd(), "scripts/check_safety.py"], 60),
        ("test_release", [*_python_cmd(), "tests/test_release.py"], 60),
        ("test-py", ["make", "test-py"], 300),
    ]
    if shutil.which("ruff"):
        gates.insert(0, ("ruff", ["ruff", "check", "scripts/", "tests/"], 120))
    if sys.platform == "darwin":
        gates.append(("typecheck", ["make", "typecheck"], 180))

    for gname, cmd, timeout in gates:
        try:
            p = subprocess.run(cmd, cwd=str(wt_dir), capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"Verification gate `{gname}` crashed: {e}"
        if p.returncode != 0:
            tail = ((p.stdout or "") + "\n" + (p.stderr or ""))[-1200:]
            return False, f"Verification gate `{gname}` failed (rc={p.returncode}):\n{tail}"
        notes.append(f"{gname}: PASS")

    # 3. If UI bug with minimized trace, replay against rebuilt binary from worktree to verify fix!
    if sys.platform == "darwin" and finding.get("trace_path") and Path(finding["trace_path"]).is_file():
        wt_bin = Path(tempfile.gettempdir()) / f"gl-bughunt-verify-{os.getpid()}-{finding['fingerprint']}"
        try:
            wt_srcs = sorted((wt_dir / "app" / "Sources").glob("*.swift"))
            cmd = ["swiftc", "-Onone", *[str(s) for s in wt_srcs], "-o", str(wt_bin)]
            if platform.machine() == "arm64":
                cmd = ["arch", "-arm64", *cmd]
            env = {**os.environ, "DEVELOPER_DIR": "/Library/Developer/CommandLineTools"}
            cp = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
            if cp.returncode != 0:
                return False, f"Worktree swiftc build failed:\n{(cp.stderr or cp.stdout)[-1000:]}"
            fx, tr_steps = load_trace_file(Path(finding["trace_path"]))
            old_root = uitest.ROOT
            try:
                uitest.ROOT = wt_dir
                still_fails, _ = replay_trace(tr_steps, fixture=fx, binary=wt_bin, target_fingerprint=finding["fingerprint"])
            finally:
                uitest.ROOT = old_root
            if still_fails:
                return False, "Trace replay verification failed: bug still reproduces after patch."
            notes.append("trace-replay: PASS (bug no longer reproduces)")
        except Exception as e:
            return False, f"Trace replay verification errored: {e}"
        finally:
            wt_bin.unlink(missing_ok=True)

    return True, "\n".join(notes)


def patch_and_open_draft_pr(
    finding: dict,
    repo_dir: Path,
    base_ref: str,
    run_dir: Path,
    dry_run: bool = False,
) -> dict:
    """Create an isolated worktree, run repro-first agent patcher, verify independently, and open Draft PR."""
    fp = finding["fingerprint"]
    date_s = dt.date.today().isoformat()
    branch = f"bughunt/{date_s}-{fp}"
    wt_dir = Path(tempfile.mkdtemp(prefix=f"gl-bughunt-wt-{fp}-"))
    result = {"fingerprint": fp, "title": finding["title"], "priority": finding["priority"], "status": "skipped", "pr_url": None, "note": ""}

    if dry_run:
        shutil.rmtree(wt_dir, ignore_errors=True)
        result["status"] = "dry_run"
        result["note"] = f"Would create branch {branch} and open Draft PR for priority={finding['priority']} ({finding['oracle']})"
        return result

    try:
        # Prepare clean worktree off base_ref
        shutil.rmtree(wt_dir, ignore_errors=True)
        add = subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "add", "-B", branch, str(wt_dir), base_ref],
            capture_output=True, text=True, timeout=60,
        )
        if add.returncode != 0:
            result["status"] = "failed"
            result["note"] = f"git worktree add failed: {add.stderr.strip()}"
            return result

        replay_hint = (
            f"python3 scripts/bughunt.py replay {finding['trace_path']}"
            if finding.get("trace_path")
            else "See details below"
        )
        prompt = f"""You are Greenlight's autonomous nightly bug fixer working in an isolated worktree (`{wt_dir}`).

Target finding (priority {finding['priority']}, oracle `{finding['oracle']}`, fixture `{finding['fixture']}`):
- Title: {finding['title']}
- Summary: {finding['summary']}
- Location: {finding.get('where') or 'unknown'}
- Repro command: `{replay_hint}`
- Details:
```
{finding.get('details', '')[:2500]}
```

Strict 4-gate contract (do NOT skip any gate):
1. **Repro-first failing test:** Write a clean, focused regression test in `tests/` that reproduces this bug, and run it BEFORE editing production code to prove it fails on `{base_ref}`. If you cannot reproduce the bug with a failing test, stop immediately and explain why without editing production files.
2. **Minimal fix:** Implement the smallest, cleanest fix respecting `AGENTS.md` (stdlib-only Python, privacy rules, no raw quoted interpolation in Swift `evaluateJavaScript`).
3. **Verify:** Confirm your new test passes, `make test` passes (and if you changed `app/`, `make typecheck` and `make test-ui` pass).
4. **Safety & privacy:** Confirm `python3 scripts/check_safety.py` and `python3 tests/test_release.py` pass.

Leave your fix cleanly committed on branch `{branch}` with a concise commit message (do NOT push; the orchestrator verifies and opens the Draft PR).
"""
        with diag.span("bughunt", phase="patch"):
            out, rc, eng = run_agent_with_fallback(prompt, cwd=wt_dir, timeout_mins=25)
            (run_dir / f"patch-{fp}.log").write_text(f"engine={eng} rc={rc}\n\n{out}")

        # Ensure any unstaged edits are committed if the agent forgot to commit
        st = subprocess.run(["git", "-C", str(wt_dir), "status", "--porcelain"], capture_output=True, text=True)
        if st.stdout.strip():
            subprocess.run(["git", "-C", str(wt_dir), "add", "-A"], check=True)
            cm = subprocess.run(
                ["git", "-C", str(wt_dir), "commit", "-m", f"fix: {finding['title'][:72]} (bughunt {fp})"],
                capture_output=True, text=True,
            )
            if cm.returncode != 0:
                result["status"] = "failed"
                result["note"] = f"git commit failed: {(cm.stderr or cm.stdout).strip()}"
                return result

        # Confirm branch actually advanced beyond base_ref
        rev = subprocess.run(
            ["git", "-C", str(wt_dir), "rev-list", "--count", f"{base_ref}..HEAD"],
            capture_output=True, text=True,
        )
        if rev.returncode != 0 or int((rev.stdout or "0").strip() or "0") == 0:
            result["status"] = "no_fix"
            result["note"] = "Agent produced no commit (could not reproduce or fix cleanly)."
            return result

        ok, verify_notes = verify_patch_worktree(wt_dir, finding, base_ref=base_ref)
        if not ok:
            result["status"] = "verify_failed"
            result["note"] = verify_notes
            return result

        # Push branch & open Draft PR
        push = subprocess.run(
            ["git", "-C", str(wt_dir), "push", "-u", "origin", branch, "--force-with-lease"],
            capture_output=True, text=True, timeout=60,
        )
        if push.returncode != 0:
            result["status"] = "push_failed"
            result["note"] = f"git push failed: {push.stderr.strip()}"
            return result

        pr_title = f"[Nightly Bug Hunt] {finding['title'][:95]}"
        pr_body = f"""## Nightly Bug Hunt — Automated Draft Fix

- **Priority score:** `{finding['priority']}` (`{finding['oracle']}`)
- **Track:** `{', '.join(finding.get('tracks') or [finding['track']])}`
- **Fixture(s):** `{', '.join(finding.get('fixtures') or [finding['fixture']])}`
- **Location:** `{finding.get('where') or 'unknown'}`
- **Repro command:** `{replay_hint}`

### Observed issue
{finding['summary']}

### Host-side verification
```
{verify_notes}
```

---
<sub>Generated autonomously by `scripts/bughunt.py` (repro-first Draft PR; never auto-merged).</sub>
<sub>bughunt-fingerprint: {fp}</sub>
"""
        pr_cmd = ["gh", "pr", "create", "--draft", "--title", pr_title, "--body", pr_body, "--head", branch, "--label", PR_LABEL]
        pr = subprocess.run(pr_cmd, cwd=str(wt_dir), capture_output=True, text=True, timeout=60)
        if pr.returncode != 0 and "label" in (pr.stderr or "").lower():
            # Fallback if label doesn't exist in repo yet
            pr = subprocess.run(
                ["gh", "pr", "create", "--draft", "--title", pr_title, "--body", pr_body, "--head", branch],
                cwd=str(wt_dir), capture_output=True, text=True, timeout=60,
            )
        if pr.returncode != 0:
            result["status"] = "pr_failed"
            result["note"] = f"gh pr create failed: {(pr.stderr or pr.stdout).strip()}"
            return result

        result["status"] = "opened"
        result["pr_url"] = pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else ""
        result["note"] = verify_notes
        return result
    finally:
        subprocess.run(["git", "-C", str(repo_dir), "worktree", "remove", "--force", str(wt_dir)], capture_output=True)
        shutil.rmtree(wt_dir, ignore_errors=True)


def open_top_draft_prs(
    ranked_findings: list[dict],
    repo_dir: Path,
    base_ref: str,
    run_dir: Path,
    max_prs: int = 2,
    dry_run: bool = False,
    existing_fps: set[str] | None = None,
    known_fps: set[str] | None = None,
) -> list[dict]:
    """Select strictly the highest-priority eligible findings and open up to `max_prs` Draft PRs."""
    candidates = select_pr_candidates(ranked_findings, existing_fps=existing_fps, known_fps=known_fps)
    outcomes = []
    opened = 0
    for f in candidates:
        if opened >= max_prs:
            break
        res = patch_and_open_draft_pr(f, repo_dir=repo_dir, base_ref=base_ref, run_dir=run_dir, dry_run=dry_run)
        outcomes.append(res)
        if res["status"] in ("opened", "dry_run"):
            opened += 1
    return outcomes


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def write_report(
    run_dir: Path,
    ranked_findings: list[dict],
    pr_outcomes: list[dict],
    metrics: dict,
    commits: list[dict],
    max_prs: int,
) -> Path:
    now_s = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Nightly Bug Hunt Report — {now_s}",
        "",
        f"- **Host:** `{platform.node()}` ({platform.system()} {platform.machine()})",
        f"- **Commits inspected:** {len(commits)}",
        f"- **Total unique findings:** {len(ranked_findings)} "
        f"({sum(1 for f in ranked_findings if f['auto_pr'])} PR-eligible, "
        f"{sum(1 for f in ranked_findings if not f['auto_pr'])} digest-only)",
        f"- **Draft PR cap:** {max_prs} (strictly highest-priority confirmed bugs)",
        "",
    ]

    if pr_outcomes:
        lines += ["## Draft PRs Attempted (Top Priority)", ""]
        for o in pr_outcomes:
            link = f" — {o['pr_url']}" if o.get("pr_url") else ""
            lines.append(f"- **[{o['status']}]** (P={o['priority']}, `{o['fingerprint']}`) {o['title']}{link}")
            if o.get("note") and o["status"] != "opened":
                lines.append(f"  - {o['note'].splitlines()[0]}")
        lines.append("")

    soak = metrics.get("soak") or {}
    if soak:
        lines += [
            "## Performance & Soak Summary (`scale` fixture)",
            "",
            f"- RSS: `{soak.get('rss_start_mb', '?')} -> {soak.get('rss_end_mb', '?')} MB` "
            f"(delta `{soak.get('rss_delta_mb', '?')} MB`, max `{soak.get('rss_max_mb', '?')} MB`)",
            f"- Open FDs: `{soak.get('fds_start', '?')} -> {soak.get('fds_end', '?')}` (delta `{soak.get('fds_delta', '?')}`)",
            f"- UI RPC latency: p50 `{soak.get('rpc_p50_ms', '?')} ms`, p95 `{soak.get('rpc_p95_ms', '?')} ms`",
            f"- Max Dashboard DOM nodes: `{soak.get('dom_dashboard_max', '?')}`",
            "",
        ]

    lines += ["## All Ranked Findings", ""]
    if not ranked_findings:
        lines.append("_Clean run — zero issues detected across all tracks._")
    else:
        for i, f in enumerate(ranked_findings, 1):
            badge = "AUTO-PR" if f["auto_pr"] else "DIGEST"
            fxs = ", ".join(f.get("fixtures") or [f["fixture"]])
            lines.append(f"{i}. **[P={f['priority']} | {badge} | `{f['oracle']}`]** {f['title']} (`{f['fingerprint']}`)")
            lines.append(f"   - Fixtures: `{fxs}` | Reproducible: `{f['reproducible']}` | Where: `{f.get('where') or '-'}`")
            lines.append(f"   - {f['summary']}")
            if f.get("trace_path"):
                lines.append(f"   - Replay: `python3 scripts/bughunt.py replay {f['trace_path']}`")
            if f.get("screenshot_path"):
                lines.append(f"   - Screenshot: `{f['screenshot_path']}`")
    lines.append("")

    report_path = run_dir / "report.md"
    report_path.write_text("\n".join(lines))
    (run_dir / "findings.json").write_text(json.dumps({
        "ts": int(time.time()),
        "metrics": metrics,
        "findings": ranked_findings,
        "pr_outcomes": pr_outcomes,
    }, indent=2))
    latest = bughunt_home() / "latest.md"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(report_path.read_text())
    return report_path


# ---------------------------------------------------------------------------
# Mac mini / Remote macOS scheduling (launchd + caffeinate)
# ---------------------------------------------------------------------------

def build_launchd_plist(repo: Path, hour: int = 2, minute: int = 30, max_prs: int = 2, with_vm: bool = False) -> str:
    if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
        raise ValueError(f"invalid schedule time {hour}:{minute}")
    if int(max_prs) < 0:
        raise ValueError(f"max_prs must be >= 0 (got {max_prs})")
    py = sys.executable
    args = ["/usr/bin/caffeinate", "-i", py, str(repo / "scripts" / "bughunt.py"), "run", "--max-prs", str(int(max_prs))]
    if with_vm:
        args.append("--with-vm")
    gl_home = str(Path(os.environ.get("GREENLIGHT_HOME") or (Path.home() / ".greenlight")).expanduser())
    log_dir = str(bughunt_home())
    env_path = os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    obj = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": args,
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": {"PATH": env_path, "GREENLIGHT_HOME": gl_home},
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "StandardOutPath": f"{log_dir}/launchd.out.log",
        "StandardErrorPath": f"{log_dir}/launchd.err.log",
    }
    return plistlib.dumps(obj, fmt=plistlib.FMT_XML, sort_keys=False).decode()


def _quote_remote_path(path: str) -> str:
    """Safely shell-quote a remote path while preserving leading ~/ tilde expansion."""
    if path == "~":
        return "~"
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def _ssh_exec(host: str, remote_cmd: str, stream: bool = False) -> subprocess.CompletedProcess:
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, remote_cmd]
    if stream:
        return subprocess.run(cmd)
    return subprocess.run(cmd, capture_output=True, text=True)


def install_schedule(
    host: str | None = None,
    repo: str | None = None,
    hour: int = 2,
    minute: int = 30,
    max_prs: int = 2,
    with_vm: bool = False,
    uninstall: bool = False,
) -> int:
    if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
        print(f"bughunt install: invalid time {hour:02d}:{minute:02d} (hour 0-23, minute 0-59)", file=sys.stderr)
        return 2
    if int(max_prs) < 0:
        print(f"bughunt install: --max-prs must be >= 0 (got {max_prs})", file=sys.stderr)
        return 2
    if host and host != "local":
        # Verify remote host is macOS, then delegate install onto the remote host (e.g. Mac mini)
        chk = _ssh_exec(host, "uname -s")
        if chk.returncode != 0:
            print(f"bughunt: SSH to {host} failed: {chk.stderr.strip()}", file=sys.stderr)
            return 1
        if chk.stdout.strip() != "Darwin":
            print(f"bughunt: remote host {host} is {chk.stdout.strip()!r}, expected macOS (Darwin) for --uitest.", file=sys.stderr)
            return 1
        q_repo = _quote_remote_path(repo or "~/.claude/skills/greenlight")
        flags = [
            f"--hour {int(hour)}",
            f"--minute {int(minute)}",
            f"--max-prs {int(max_prs)}",
        ]
        if with_vm:
            flags.append("--with-vm")
        if uninstall:
            flags.append("--uninstall")
        cmd = f"cd {q_repo} && python3 scripts/bughunt.py install {' '.join(flags)}"
        p = _ssh_exec(host, cmd, stream=True)
        return p.returncode

    if sys.platform != "darwin":
        print("bughunt install: local schedule requires macOS (or pass --host <mac-mini-ssh-host>).", file=sys.stderr)
        return 1

    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    if uninstall:
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        plist_path.unlink(missing_ok=True)
        print(f"uninstalled {plist_path}")
        return 0

    bughunt_home().mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    target_repo = Path(repo).expanduser().resolve() if repo else ROOT
    plist_path.write_text(build_launchd_plist(target_repo, hour=hour, minute=minute, max_prs=max_prs, with_vm=with_vm))
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    subprocess.run(["launchctl", "load", "-w", str(plist_path)], check=True)
    print(f"installed {LAUNCHD_LABEL} at {hour:02d}:{minute:02d} daily (max_prs={max_prs}) -> {plist_path}")
    return 0


def run_remote(host: str, repo: str | None = None, extra_args: list[str] | None = None) -> int:
    q_repo = _quote_remote_path(repo or "~/.claude/skills/greenlight")
    q_args = " ".join(shlex.quote(a) for a in (extra_args or []))
    cmd = f"cd {q_repo} && (git fetch origin main --quiet 2>/dev/null || true) && caffeinate -i python3 scripts/bughunt.py run {q_args}"
    p = _ssh_exec(host, cmd, stream=True)
    return p.returncode


def show_status(host: str | None = None, repo: str | None = None) -> int:
    if host and host != "local":
        q_repo = _quote_remote_path(repo or "~/.claude/skills/greenlight")
        p = _ssh_exec(host, f"cd {q_repo} && python3 scripts/bughunt.py status", stream=True)
        return p.returncode

    home = bughunt_home()
    latest = home / "latest.md"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    print(f"Host:     {platform.node()} ({platform.system()} {platform.machine()})")
    print(f"Schedule: {'installed (' + str(plist) + ')' if plist.is_file() else 'not installed'}")
    hist = load_perf_history(home, limit=5)
    if hist:
        print(f"Recent runs ({len(hist)}):")
        for h in hist:
            soak = h.get("soak") or {}
            print(f"  {h.get('date', '?')}  findings={h.get('findings', 0)}  prs={h.get('prs_opened', 0)}  "
                  f"rss_max={soak.get('rss_max_mb', '-')}MB  rpc_p95={soak.get('rpc_p95_ms', '-')}ms")
    if latest.is_file():
        print("\n" + latest.read_text())
    else:
        print("No nightly runs recorded yet.")
    return 0


# ---------------------------------------------------------------------------
# Full Nightly Orchestration (`bughunt.py run`)
# ---------------------------------------------------------------------------

def _acquire_pid_lock(lock_file: Path) -> bool:
    """Atomically acquire a PID lock file, reclaiming only if the recorded holder PID is dead."""
    for _ in range(2):
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return True
        except FileExistsError:
            try:
                old_pid = int(lock_file.read_text().strip())
                os.kill(old_pid, 0)
                print(f"bughunt: another run is active (pid {old_pid}); skipping.", file=sys.stderr)
                return False
            except (OSError, ValueError):
                lock_file.unlink(missing_ok=True)
    return False


def run_nightly(args) -> int:
    fixtures = tuple(x.strip() for x in args.fixtures.split(",") if x.strip())
    bad_fx = [fx for fx in fixtures if fx not in uitest.FIXTURES]
    if bad_fx:
        print(f"bughunt: unknown fixture(s): {', '.join(bad_fx)} (expected one of {', '.join(uitest.FIXTURES)})", file=sys.stderr)
        return 2

    # Atomic single-instance lock so overlapping schedules never collide
    home = bughunt_home()
    home.mkdir(parents=True, exist_ok=True)
    lock_file = home / "run.pid"
    if not _acquire_pid_lock(lock_file):
        return 0

    run_id = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Resolve base ref (refresh origin/main if present)
        base_ref = args.base
        if base_ref.startswith("origin/"):
            subprocess.run(["git", "-C", str(ROOT), "fetch", "origin", base_ref.split("/", 1)[1], "--quiet"],
                           capture_output=True, timeout=30)
            chk = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", base_ref], capture_output=True)
            if chk.returncode != 0:
                base_ref = "HEAD"

        commits, recent_files = get_recent_commits(ROOT, since_hours=args.since_hours)
        history = load_perf_history(home)
        raw_findings: list[dict] = []
        metrics: dict = {}

        with diag.span("bughunt", phase="full"):
            # Track 0: Baseline gate
            if not args.skip_baseline:
                raw_findings.extend(run_baseline(ROOT))

            # Track A: Parallel chaos fuzzer + scale perf soak
            fuzz_findings, fuzz_metrics = run_fuzz_matrix(
                fixtures=fixtures,
                seed=args.seed,
                steps=args.fuzz_steps,
                soak_secs=args.soak_secs,
                workers=args.workers,
                run_dir=run_dir,
                history=history,
            )
            raw_findings.extend(fuzz_findings)
            metrics.update(fuzz_metrics)

            # Track B: Diff-aware commit regression LLM explorer
            if not args.skip_llm:
                raw_findings.extend(run_llm_explorer(ROOT, commits, recent_files, run_dir))

            # Optional Tier-2 VM HID smoke
            if args.with_vm:
                raw_findings.extend(run_vm_hid(ROOT, run_dir))

            # Rank ALL findings across all tracks, then pick strictly top `max_prs` (default 2)
            ranked = merge_and_rank_findings(raw_findings, recent_files=recent_files)
            existing_fps = fetch_open_pr_fingerprints(ROOT)
            known_fps = load_known_fingerprints(home)
            pr_outcomes = open_top_draft_prs(
                ranked,
                repo_dir=ROOT,
                base_ref=base_ref,
                run_dir=run_dir,
                max_prs=args.max_prs,
                dry_run=args.dry_run,
                existing_fps=existing_fps,
                known_fps=known_fps,
            )

            report_path = write_report(run_dir, ranked, pr_outcomes, metrics, commits, args.max_prs)
            prs_opened = sum(1 for o in pr_outcomes if o["status"] == "opened")
            append_perf_history({
                "ts": int(time.time()),
                "date": run_id[:10],
                "findings": len(ranked),
                "eligible": sum(1 for f in ranked if f["auto_pr"]),
                "prs_opened": prs_opened,
                "soak": metrics.get("soak") or {},
            }, home_dir=home)
            diag.event(
                "bughunt",
                status="ok",
                findings=len(ranked),
                eligible=sum(1 for f in ranked if f["auto_pr"]),
                prs_opened=prs_opened,
            )
            print(f"bughunt complete: {len(ranked)} unique findings, {prs_opened} Draft PR(s) opened. Report: {report_path}")
        return 0
    finally:
        lock_file.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description="Greenlight nightly autonomous bug hunter & Draft PR patcher")
    sub = p.add_subparsers(dest="cmd", required=True)

    rn = sub.add_parser("run", help="Run full nightly hunt, rank all issues, open top-N Draft PRs")
    rn.add_argument("--max-prs", type=int, default=2, help="Max Draft PRs to open per run (default: 2)")
    rn.add_argument("--dry-run", action="store_true", help="Hunt & rank without opening PRs")
    rn.add_argument("--since-hours", type=int, default=72, help="Commit lookback window for regression hunting")
    rn.add_argument("--fixtures", default=",".join(DEFAULT_FIXTURES), help="Comma-separated fixture list")
    rn.add_argument("--seed", type=int, default=int(dt.date.today().strftime("%Y%m%d")), help="Base RNG seed")
    rn.add_argument("--fuzz-steps", type=int, default=60, help="Monkey steps per fixture")
    rn.add_argument("--soak-secs", type=int, default=90, help="Scale soak duration in seconds (0 to skip)")
    rn.add_argument("--workers", type=int, default=2, help="Parallel --uitest fixture workers")
    rn.add_argument("--skip-baseline", action="store_true", help="Skip Track 0 baseline suite")
    rn.add_argument("--skip-llm", action="store_true", help="Skip Track B diff-aware LLM explorer")
    rn.add_argument("--with-vm", action="store_true", help="Also run optional Tier-2 VM HID smoke")
    rn.add_argument("--base", default="origin/main", help="Base git ref for fix worktrees")

    fz = sub.add_parser("fuzz", help="Run standalone Track A fuzzer on one fixture")
    fz.add_argument("--fixture", default="default", choices=uitest.FIXTURES)
    fz.add_argument("--seed", type=int, default=42)
    fz.add_argument("--steps", type=int, default=60)
    fz.add_argument("--soak-secs", type=int, default=0)
    fz.add_argument("--out-dir", default=None)

    rp = sub.add_parser("replay", help="Replay a recorded trace.jsonl and optionally delta-shrink it")
    rp.add_argument("trace", help="Path to trace.jsonl")
    rp.add_argument("--shrink", action="store_true", help="Delta-minimize the trace")
    rp.add_argument("--out", default=None, help="Output path for shrunk trace")
    rp.add_argument("--oracle", default=None, help="Specific oracle name to match")

    inst = sub.add_parser("install", help="Install nightly launchd schedule (locally or on Mac mini via --host)")
    inst.add_argument("--host", default=os.environ.get("GREENLIGHT_HOST"), help="SSH host (e.g. Mac mini) or 'local'")
    inst.add_argument("--repo", default=None, help="Repo path on target host")
    inst.add_argument("--hour", type=int, default=2)
    inst.add_argument("--minute", type=int, default=30)
    inst.add_argument("--max-prs", type=int, default=2)
    inst.add_argument("--with-vm", action="store_true")
    inst.add_argument("--uninstall", action="store_true")

    rem = sub.add_parser("run-remote", help="Trigger an on-demand hunt on a remote Mac (e.g. Mac mini)")
    rem.add_argument("--host", required=True, help="SSH host of the Mac mini")
    rem.add_argument("--repo", default=None, help="Repo path on remote host")
    rem.add_argument("extra", nargs=argparse.REMAINDER, help="Extra arguments forwarded to `bughunt.py run`")

    st = sub.add_parser("status", help="Show latest nightly report, schedule, and perf history")
    st.add_argument("--host", default=None, help="Optional SSH host (e.g. Mac mini)")
    st.add_argument("--repo", default=None, help="Repo path on remote host")

    args = p.parse_args()
    if args.cmd == "run":
        return run_nightly(args)
    if args.cmd == "fuzz":
        out_dir = Path(args.out_dir) if args.out_dir else Path(tempfile.mkdtemp(prefix="gl-fuzz-"))
        findings, _, metrics = fuzz_session(fixture=args.fixture, seed=args.seed, steps=args.steps, run_dir=out_dir)
        if args.soak_secs > 0:
            sf, sm = run_soak(soak_secs=args.soak_secs, fixture=args.fixture)
            findings.extend(sf)
            metrics = {"fuzz": metrics, "soak": sm}
        ranked = merge_and_rank_findings(findings)
        print(json.dumps({"fixture": args.fixture, "seed": args.seed, "findings": ranked, "metrics": metrics, "out_dir": str(out_dir)}, indent=2))
        return 1 if any(f["auto_pr"] or f["oracle"] in HARD_ORACLES for f in ranked) else 0
    if args.cmd == "replay":
        fx, steps = load_trace_file(Path(args.trace))
        ok, findings = replay_trace(steps, fixture=fx, target_oracle=args.oracle)
        print(json.dumps({"reproduced": ok, "steps": len(steps), "findings": findings}, indent=2))
        if ok and args.shrink:
            minimal = shrink_trace(steps, lambda cand: replay_trace(cand, fixture=fx, target_oracle=args.oracle)[0])
            out = Path(args.out) if args.out else Path(args.trace).with_suffix(".min.jsonl")
            save_trace_file(out, fx, minimal)
            print(f"shrunk {len(steps)} -> {len(minimal)} steps -> {out}")
        return 0 if ok else 1
    if args.cmd == "install":
        return install_schedule(
            host=args.host, repo=args.repo, hour=args.hour, minute=args.minute,
            max_prs=args.max_prs, with_vm=args.with_vm, uninstall=args.uninstall,
        )
    if args.cmd == "run-remote":
        extra = [a for a in (args.extra or []) if a != "--"]
        return run_remote(host=args.host, repo=args.repo, extra_args=extra)
    if args.cmd == "status":
        return show_status(host=args.host, repo=args.repo)
    return 2


if __name__ == "__main__":
    diag.install("bughunt")
    sys.exit(main())
