#!/usr/bin/env python3
"""Run Playwright JS tests on Chromium with V8 coverage mapped to app/*.html inline <script> lines.

Usage:
    python3 scripts/js_coverage.py [--json out.json] [--lcov out.info] [--fail-under 80] [--min-file 75]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
HTML_PAGES = ("dashboard.html", "terminal.html", "stage.html")
SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)


def discover_node_path() -> str | None:
    """Locate a directory containing playwright-core when NODE_PATH is not already configured."""
    existing = os.environ.get("NODE_PATH")
    if existing:
        return existing
    candidates = [
        ROOT / "node_modules",
        Path("/tmp/pw/node_modules"),  # noqa: S108 - standard CI cache location
        Path.home() / ".cache" / "ms-playwright" / "node_modules",
    ]
    workshop = ROOT.parent
    if workshop.is_dir():
        for child in sorted(workshop.iterdir()):
            nm = child / "node_modules"
            if (nm / "playwright-core").is_dir():
                candidates.append(nm)
    for cand in candidates:
        if (cand / "playwright-core").is_dir() or (cand / "playwright").is_dir():
            return str(cand)
    return None


def extract_inline_scripts(html_path: Path) -> list[dict[str, Any]]:
    """Return metadata for each inline <script> block in an HTML file."""
    text = html_path.read_text(encoding="utf-8")
    blocks: list[dict[str, Any]] = []
    for m in SCRIPT_RE.finditer(text):
        attrs, body = m.group(1), m.group(2)
        if re.search(r"\bsrc\s*=", attrs, re.I) or not body.strip():
            continue
        start_line = text[: m.start(2)].count("\n") + 1
        blocks.append({"start_line": start_line, "source": body})
    return blocks


def _is_non_executable_js_line(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith(("//", "/*", "*", "*/")):
        return True
    return s in {"{", "}", "};", ");", "]);", "});", "]", "[", "),"}


def _line_spans(source: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    pos = 0
    for raw in source.splitlines(keepends=True):
        line_no_nl = raw.rstrip("\r\n")
        spans.append((pos, pos + len(line_no_nl), line_no_nl))
        pos += len(raw)
    return spans


def _apply_v8_ranges(source: str, functions: list[dict[str, Any]], hits: dict[int, int], start_line: int) -> None:
    """Map V8 function byte ranges to 1-based HTML line hit counts using deepest-range precedence."""
    spans = _line_spans(source)
    if not spans:
        return
    # Flatten all ranges sorted outermost-first (larger span first, smaller/inner overrides outer)
    flat: list[tuple[int, int, int]] = []
    for fn in functions:
        for r in fn.get("ranges") or []:
            s, e, c = int(r.get("startOffset", 0)), int(r.get("endOffset", 0)), int(r.get("count", 0))
            if e > s:
                flat.append((s, e, c))
    flat.sort(key=lambda t: (t[0], -(t[1] - t[0])))

    for idx, (ls, le, text) in enumerate(spans):
        if _is_non_executable_js_line(text):
            continue
        # Find the narrowest range covering the non-whitespace slice of this line
        lead = len(text) - len(text.lstrip())
        probe_start = ls + lead
        probe_end = max(probe_start + 1, le)
        matched_count: int | None = None
        best_len = 1 << 60
        for rs, re_, cnt in flat:
            if rs <= probe_start and re_ >= probe_end:
                span_len = re_ - rs
                if span_len <= best_len:
                    best_len = span_len
                    matched_count = cnt
        if matched_count is None:
            continue
        html_line = start_line + idx
        prev = hits.get(html_line, 0)
        hits[html_line] = max(prev, matched_count)


def _match_page(url: str) -> str | None:
    for name in HTML_PAGES:
        if url.endswith(f"/{name}") or f"/{name}?" in url:
            return name
    return None


def aggregate_v8_coverage(cov_dir: Path, root: Path = ROOT) -> dict[str, Any]:
    """Aggregate raw V8 JSON coverage dumps into per-HTML-file line metrics."""
    page_blocks = {name: extract_inline_scripts(root / "app" / name) for name in HTML_PAGES}
    page_hits: dict[str, dict[int, int]] = {name: {} for name in HTML_PAGES}

    for dump in sorted(cov_dir.glob("v8-*.json")):
        try:
            entries = json.loads(dump.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in entries:
            page = _match_page(entry.get("url", ""))
            if not page:
                continue
            src = entry.get("source") or ""
            if not src.strip():
                continue
            for blk in page_blocks[page]:
                if blk["source"] == src or blk["source"].strip() == src.strip():
                    off = blk["source"].find(src)
                    adj = blk["source"][:off].count("\n") if off >= 0 else 0
                    _apply_v8_ranges(src, entry.get("functions") or [], page_hits[page], blk["start_line"] + adj)
                    break

    files: dict[str, dict[str, Any]] = {}
    tot_cov = 0
    tot_lines = 0
    for name in HTML_PAGES:
        hits = page_hits[name]
        valid = sorted(hits)
        covered = sum(1 for ln in valid if hits[ln] > 0)
        missed = [ln for ln in valid if hits[ln] == 0]
        total = len(valid)
        pct = round((covered / total * 100.0) if total else 0.0, 2)
        tot_cov += covered
        tot_lines += total
        files[f"app/{name}"] = {
            "covered": covered,
            "total": total,
            "pct": pct,
            "missed_lines": missed,
            "hits": hits,
        }
    total_pct = round((tot_cov / tot_lines * 100.0) if tot_lines else 0.0, 2)
    return {"line_pct": total_pct, "lines_covered": tot_cov, "lines_valid": tot_lines, "files": files}


def write_lcov(summary: dict[str, Any], out_path: Path) -> None:
    lines: list[str] = []
    for rel, info in summary["files"].items():
        lines.append(f"SF:{rel}")
        for ln, cnt in sorted(info["hits"].items()):
            lines.append(f"DA:{ln},{cnt}")
        lines.append(f"LF:{info['total']}")
        lines.append(f"LH:{info['covered']}")
        lines.append("end_of_record")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _format_ranges(nums: list[int], limit: int = 12) -> str:
    if not nums:
        return ""
    ranges: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
        else:
            ranges.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = n
    ranges.append(f"{start}-{prev}" if start != prev else str(start))
    if len(ranges) > limit:
        return ", ".join(ranges[:limit]) + f" (+{len(ranges) - limit} more)"
    return ", ".join(ranges)


def _check_js_thresholds(summary: dict[str, Any], fail_under: float | None, min_file: float | None) -> list[str]:
    errs: list[str] = []
    if fail_under is not None and summary["line_pct"] < fail_under:
        errs.append(f"JS total coverage {summary['line_pct']:.2f}% < required {fail_under:.2f}%")
    if min_file is not None:
        for rel, info in summary["files"].items():
            if info["pct"] < min_file:
                errs.append(f"JS file {rel} coverage {info['pct']:.2f}% < per-file floor {min_file:.2f}%")
    return errs


def _run_js_suites(node: str, node_path: str, cov_dir: Path) -> list[str]:
    env = {**os.environ, "NODE_PATH": node_path, "BROWSER": "chromium", "JS_COV_DIR": str(cov_dir)}
    failed: list[str] = []
    for tf in sorted((ROOT / "tests").glob("test_*.cjs")):
        rel = str(tf.relative_to(ROOT))
        if subprocess.run([node, str(tf)], cwd=ROOT, env=env).returncode != 0:
            print(f"FAIL {rel}", file=sys.stderr)
            failed.append(rel)
    return failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run Playwright Chromium JS tests and report inline <script> coverage.")
    ap.add_argument("--json", default=None, help="Write JSON coverage summary to this path")
    ap.add_argument("--lcov", default=None, help="Write LCOV report to this path")
    ap.add_argument("--fail-under", type=float, default=None, help="Fail if total JS line coverage is below this %%")
    ap.add_argument("--min-file", type=float, default=None, help="Fail if any HTML page JS coverage is below this %%")
    args = ap.parse_args(argv)

    node = shutil.which("node")
    if not node:
        print("FAIL node is required for JS coverage", file=sys.stderr)
        return 1
    node_path = discover_node_path()
    if not node_path:
        print("FAIL playwright-core not found; set NODE_PATH or install playwright-core", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="gl-jscov-") as tmp:
        cov_dir = Path(tmp)
        failed = _run_js_suites(node, node_path, cov_dir)
        if failed:
            print(f"FAILED {len(failed)} JS suite(s): {', '.join(failed)}", file=sys.stderr)
            return 1
        summary = aggregate_v8_coverage(cov_dir, ROOT)

    print("\n=== JavaScript Line Coverage (app/*.html inline scripts) ===")
    for rel, info in summary["files"].items():
        print(f"  {rel:22s} {info['pct']:6.2f}%  ({info['covered']:4d}/{info['total']:4d})  {_format_ranges(info['missed_lines'])}")
    print(f"  {'TOTAL':22s} {summary['line_pct']:6.2f}%  ({summary['lines_covered']:4d}/{summary['lines_valid']:4d})")

    if args.lcov:
        write_lcov(summary, Path(args.lcov))
    if args.json:
        serializable = {
            **summary,
            "files": {k: {fk: fv for fk, fv in v.items() if fk != "hits"} for k, v in summary["files"].items()},
        }
        Path(args.json).write_text(json.dumps(serializable, indent=2) + "\n", encoding="utf-8")

    errs = _check_js_thresholds(summary, args.fail_under, args.min_file)
    for e in errs:
        print(f"COVERAGE FAIL: {e}", file=sys.stderr)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
