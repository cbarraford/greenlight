#!/usr/bin/env python3
"""Static JavaScript linter for Greenlight's inline HTML scripts and JS test suites.

Uses Python stdlib + `node --check` (zero external npm dependencies).

Checks:
  JS001  JavaScript syntax validation via `node --check` (script & module)
  JS101  Disallowed `debugger` statement
  JS102  Disallowed `eval(` call (app/*.html)
  JS103  Disallowed `new Function(` call (app/*.html)
  JS104  Disallowed `document.write(` call (app/*.html)
  JS105  Disallowed `alert(` / `confirm(` / `prompt(` dialog call (app/*.html)
  JS201  Invalid `typeof` comparison target (e.g. `typeof x === "undef"`)
  JS202  Direct comparison with `NaN` (must use `Number.isNaN(...)`)
  JS203  Duplicate key in object literal
  JS204  Possible accidental assignment in `if` / `while` condition
  JS205  `console.log(` leftover in production page (warning; error with --strict)
  JS206  Static `getElementById("...")` referencing an ID absent from the HTML file

Inline suppression: append `// lint-ok` (or `/* lint-ok */`) on the line to suppress
non-syntax findings on that line.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML_PAGES = ("dashboard.html", "terminal.html", "stage.html")
SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)

VALID_TYPEOF_VALUES = frozenset({
    "undefined",
    "object",
    "boolean",
    "number",
    "string",
    "function",
    "symbol",
    "bigint",
})
CMP_OPS = frozenset({"===", "!==", "==", "!="})
DIALOG_CALLS = frozenset({"alert", "confirm", "prompt"})
REGEX_PREV_PUNCT = frozenset({"(", "[", "{", ",", ";", ":", "?"})
REGEX_PREV_IDENT = frozenset({
    "return", "case", "throw", "in", "of", "typeof", "void",
    "delete", "new", "yield", "await", "else", "do",
})
OBJ_PREV_OPS = frozenset({
    "=", "&&", "||", "??", "+", "-", "*", "/", "%",
    "===", "!==", "==", "!=", "<", ">", "<=", ">=",
    "+=", "-=", "*=", "/=", "??=", "||=", "&&=", "...",
})
OBJ_PREV_PUNCT = frozenset({"(", "[", ",", "?"})
OBJ_PREV_IDENT = frozenset({"return", "throw", "yield", "await"})
NON_OBJ_PREV_IDENT = frozenset({"const", "let", "var", "import", "export", "catch", "function", "class"})
THREE_CHAR_OPS = frozenset({"===", "!==", ">>>", "**=", "&&=", "||=", "??=", "..."})
TWO_CHAR_OPS = frozenset({
    "==", "!=", "<=", ">=", "=>", "&&", "||", "??", "++", "--",
    "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "?.", "<<", ">>", "**",
})
PUNCT_CHARS = frozenset("()[]{},;:?")


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    line: int


@dataclass(frozen=True)
class ScriptBlock:
    path: Path
    source: str
    start_line: int
    is_module: bool
    is_html: bool
    html_text: str = ""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    code: str
    message: str
    severity: str = "error"  # "error" or "warning"

    def format(self, strict: bool = False) -> str:
        prefix = "" if (self.severity == "error" or strict) else "WARN "
        return f"{self.path}:{self.line}: {prefix}{self.code} {self.message}"


def _skip_line_comment(src: str, i: int) -> int:
    nl = src.find("\n", i + 2)
    return len(src) if nl == -1 else nl


def _skip_block_comment(src: str, i: int) -> tuple[int, int]:
    end = src.find("*/", i + 2)
    if end == -1:
        chunk = src[i:]
        return len(src), chunk.count("\n")
    chunk = src[i : end + 2]
    return end + 2, chunk.count("\n")


def _read_string(src: str, i: int, quote: str) -> tuple[str, int, int]:
    j = i + 1
    n = len(src)
    newlines = 0
    while j < n:
        ch = src[j]
        if ch == "\\" and j + 1 < n:
            if src[j + 1] == "\n":
                newlines += 1
            j += 2
            continue
        if ch == "\n":
            newlines += 1
        if ch == quote:
            j += 1
            break
        j += 1
    return src[i:j], j, newlines


def _read_template_chunk(src: str, i: int) -> tuple[str, int, int, bool]:
    """Read template literal text starting after `` ` `` or `}`. Returns (text, next_i, newlines, opened_expr)."""
    j = i
    n = len(src)
    newlines = 0
    while j < n:
        ch = src[j]
        if ch == "\\" and j + 1 < n:
            if src[j + 1] == "\n":
                newlines += 1
            j += 2
            continue
        if ch == "\n":
            newlines += 1
            j += 1
            continue
        if ch == "`":
            return src[i:j], j + 1, newlines, False
        if ch == "$" and j + 1 < n and src[j + 1] == "{":
            return src[i:j], j + 2, newlines, True
        j += 1
    return src[i:j], j, newlines, False


def _can_start_regex(prev: Token | None) -> bool:
    if prev is None:
        return True
    if prev.kind == "punct":
        return prev.value in REGEX_PREV_PUNCT
    if prev.kind == "op":
        return prev.value not in {"++", "--"}
    if prev.kind == "ident":
        return prev.value in REGEX_PREV_IDENT
    return False


def _read_regex(src: str, i: int) -> tuple[str, int]:
    j = i + 1
    n = len(src)
    in_class = False
    while j < n:
        ch = src[j]
        if ch == "\\" and j + 1 < n:
            j += 2
            continue
        if ch == "\n":
            return src[i:j], j
        if ch == "[":
            in_class = True
        elif ch == "]" and in_class:
            in_class = False
        elif ch == "/" and not in_class:
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            return src[i:j], j
        j += 1
    return src[i:j], j


def _read_ident_or_num(src: str, i: int, line: int) -> tuple[Token, int]:
    n = len(src)
    j = i
    ch = src[i]
    if ch.isdigit() or (ch == "." and i + 1 < n and src[i + 1].isdigit()):
        while j < n and (src[j].isalnum() or src[j] in "._"):
            j += 1
        return Token("num", src[i:j], line), j
    while j < n and (src[j].isalnum() or src[j] in "_$"):
        j += 1
    return Token("ident", src[i:j], line), j


def _read_op_or_punct(src: str, i: int, line: int) -> tuple[Token, int]:
    three = src[i : i + 3]
    if three in THREE_CHAR_OPS:
        return Token("op", three, line), i + 3
    two = src[i : i + 2]
    if two in TWO_CHAR_OPS:
        return Token("op", two, line), i + 2
    ch = src[i]
    kind = "punct" if ch in PUNCT_CHARS else "op"
    return Token(kind, ch, line), i + 1


def _consume_template(
    src: str,
    i: int,
    line: int,
    tokens: list[Token],
    tpl_stack: list[int],
    brace_depth: int,
) -> tuple[int, int, int]:
    txt, nxt, nls, opened = _read_template_chunk(src, i)
    tokens.append(Token("tpl", txt, line))
    line += nls
    if opened:
        tpl_stack.append(brace_depth)
        brace_depth += 1
        tokens.append(Token("punct", "${", line))
    return nxt, line, brace_depth


def _next_non_ws(src: str, i: int, line: int) -> tuple[int, int]:
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "\n":
            line += 1
            i += 1
        elif ch.isspace():
            i += 1
        elif ch == "/" and i + 1 < n and src[i + 1] == "/":
            i = _skip_line_comment(src, i)
        elif ch == "/" and i + 1 < n and src[i + 1] == "*":
            i, nls = _skip_block_comment(src, i)
            line += nls
        else:
            break
    return i, line


def tokenize_js(src: str, start_line: int = 1) -> list[Token]:
    """Tokenize JavaScript source while preserving 1-based line numbers."""
    tokens: list[Token] = []
    i = 0
    n = len(src)
    line = start_line
    brace_depth = 0
    tpl_stack: list[int] = []
    while i < n:
        i, line = _next_non_ws(src, i, line)
        if i >= n:
            break
        ch = src[i]
        if ch in ('"', "'"):
            raw, i, nls = _read_string(src, i, ch)
            tokens.append(Token("str", raw, line))
            line += nls
        elif ch == "`":
            i, line, brace_depth = _consume_template(src, i + 1, line, tokens, tpl_stack, brace_depth)
        elif ch == "/" and _can_start_regex(tokens[-1] if tokens else None):
            raw, i = _read_regex(src, i)
            tokens.append(Token("regex", raw, line))
        elif ch.isalnum() or ch in "_$" or (ch == "." and i + 1 < n and src[i + 1].isdigit()):
            tok, i = _read_ident_or_num(src, i, line)
            tokens.append(tok)
        else:
            tok, i = _read_op_or_punct(src, i, line)
            tokens.append(tok)
            if tok.value == "{":
                brace_depth += 1
            elif tok.value == "}":
                brace_depth = max(0, brace_depth - 1)
                if tpl_stack and brace_depth == tpl_stack[-1]:
                    tpl_stack.pop()
                    i, line, brace_depth = _consume_template(src, i, line, tokens, tpl_stack, brace_depth)
    return tokens


def _unquote(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        return raw[1:-1]
    return raw


def _is_suppressed(raw_lines: list[str], lineno: int) -> bool:
    if 1 <= lineno <= len(raw_lines):
        line = raw_lines[lineno - 1]
        return "// lint-ok" in line or "/* lint-ok */" in line
    return False


def extract_html_scripts(html_path: Path) -> list[ScriptBlock]:
    """Extract inline `<script>` blocks from an HTML file with exact 1-based line offsets."""
    text = html_path.read_text(encoding="utf-8")
    blocks: list[ScriptBlock] = []
    for m in SCRIPT_RE.finditer(text):
        attrs, body = m.group(1), m.group(2)
        if re.search(r"\bsrc\s*=", attrs, re.I) or not body.strip():
            continue
        is_module = bool(re.search(r'\btype\s*=\s*["\']module["\']', attrs, re.I))
        start_line = text[: m.start(2)].count("\n") + 1
        blocks.append(ScriptBlock(html_path, body, start_line, is_module, True, text))
    return blocks


def _check_node_syntax(blocks: list[ScriptBlock], root: Path) -> list[Finding]:
    node = shutil.which("node")
    if not node:
        if os.environ.get("CI") == "true":
            return [Finding("js_lint", 1, "JS001", "node is required in CI for JS syntax checks")]
        return []
    findings: list[Finding] = []
    with tempfile.TemporaryDirectory(prefix="gl-js-lint-") as tmp:
        tmp_dir = Path(tmp)
        for idx, blk in enumerate(blocks, 1):
            rel = _rel_path(blk.path, root)
            if blk.is_html:
                ext = "mjs" if blk.is_module else "js"
                probe = tmp_dir / f"{blk.path.stem}_{idx}.{ext}"
                probe.write_text("\n" * (blk.start_line - 1) + blk.source, encoding="utf-8")
                target = probe
            else:
                target = blk.path
            p = subprocess.run([node, "--check", str(target)], capture_output=True, text=True, timeout=15)
            if p.returncode != 0:
                detail = (p.stderr or p.stdout).strip().replace(str(target), rel)
                lineno = _parse_node_error_line(detail, blk.start_line)
                first_msg = _first_syntax_message(detail)
                findings.append(Finding(rel, lineno, "JS001", f"syntax error: {first_msg}"))
    return findings


def _parse_node_error_line(detail: str, default_line: int) -> int:
    m = re.search(r":(\d+)\b", detail)
    return int(m.group(1)) if m else default_line


def _first_syntax_message(detail: str) -> str:
    for row in detail.splitlines():
        if "SyntaxError:" in row:
            return row.strip()
    return (detail.splitlines()[0] if detail.splitlines() else "invalid syntax")[:160]


def _check_disallowed(tokens: list[Token], rel: str, is_html: bool) -> list[Finding]:
    findings: list[Finding] = []
    n = len(tokens)
    for i, tok in enumerate(tokens):
        prev = tokens[i - 1] if i > 0 else None
        nxt = tokens[i + 1] if i + 1 < n else None
        if tok.kind != "ident":
            continue
        is_prop = prev is not None and prev.value == "."
        is_key = nxt is not None and nxt.value == ":"
        if tok.value == "debugger" and not is_prop and not is_key:
            findings.append(Finding(rel, tok.line, "JS101", "disallowed `debugger` statement"))
        if not is_html:
            continue
        _check_html_disallowed_at(tokens, i, rel, findings)
    return findings


def _check_html_disallowed_at(tokens: list[Token], i: int, rel: str, out: list[Finding]) -> None:
    tok = tokens[i]
    prev = tokens[i - 1] if i > 0 else None
    nxt = tokens[i + 1] if i + 1 < len(tokens) else None
    nxt2 = tokens[i + 2] if i + 2 < len(tokens) else None
    nxt3 = tokens[i + 3] if i + 3 < len(tokens) else None
    is_prop = prev is not None and prev.value == "."
    if tok.value == "eval" and not is_prop and nxt and nxt.value == "(":
        out.append(Finding(rel, tok.line, "JS102", "disallowed `eval(...)` call in production HTML"))
    elif tok.value == "new" and nxt and nxt.kind == "ident" and nxt.value == "Function" and nxt2 and nxt2.value == "(":
        out.append(Finding(rel, tok.line, "JS103", "disallowed `new Function(...)` call in production HTML"))
    elif (
        tok.value == "document"
        and not is_prop
        and nxt and nxt.value == "."
        and nxt2 and nxt2.kind == "ident" and nxt2.value == "write"
        and nxt3 and nxt3.value == "("
    ):
        out.append(Finding(rel, tok.line, "JS104", "disallowed `document.write(...)` call in production HTML"))
    elif (
        tok.value in DIALOG_CALLS
        and nxt and nxt.value == "("
        and (not is_prop or (i >= 2 and tokens[i - 2].value == "window"))
        and (prev is None or prev.value != "function")
    ):
        out.append(Finding(rel, tok.line, "JS105", f"disallowed blocking dialog `{tok.value}(...)` in production HTML"))
    elif (
        tok.value == "console"
        and not is_prop
        and nxt and nxt.value == "."
        and nxt2 and nxt2.kind == "ident" and nxt2.value == "log"
        and nxt3 and nxt3.value == "("
    ):
        out.append(Finding(rel, tok.line, "JS205", "`console.log(...)` leftover in production HTML", severity="warning"))


def _check_typeof(tokens: list[Token], rel: str) -> list[Finding]:
    findings: list[Finding] = []
    n = len(tokens)
    stop_vals = frozenset({";", ",", ")", "]", "}", "&&", "||", "?", ":", "="})
    for i, tok in enumerate(tokens):
        if tok.kind == "ident" and tok.value == "typeof":
            j = i + 1
            depth = 0
            while j < n:
                v = tokens[j].value
                if v in ("(", "[", "{", "${"):
                    depth += 1
                elif v in (")", "]", "}"):
                    if depth == 0:
                        break
                    depth -= 1
                elif depth == 0 and v in CMP_OPS:
                    if j + 1 < n and tokens[j + 1].kind == "str":
                        val = _unquote(tokens[j + 1].value)
                        if val not in VALID_TYPEOF_VALUES:
                            findings.append(Finding(rel, tokens[j + 1].line, "JS201", f"invalid `typeof` comparison value {val!r}"))
                    break
                elif depth == 0 and v in stop_vals:
                    break
                j += 1
        elif (
            tok.kind == "str"
            and i + 2 < n
            and tokens[i + 1].value in CMP_OPS
            and tokens[i + 2].kind == "ident"
            and tokens[i + 2].value == "typeof"
        ):
            val = _unquote(tok.value)
            if val not in VALID_TYPEOF_VALUES:
                findings.append(Finding(rel, tok.line, "JS201", f"invalid `typeof` comparison value {val!r}"))
    return findings


def _check_nan_compare(tokens: list[Token], rel: str) -> list[Finding]:
    findings: list[Finding] = []
    n = len(tokens)
    for i, tok in enumerate(tokens):
        if tok.kind != "op" or tok.value not in CMP_OPS:
            continue
        lhs_nan = i > 0 and tokens[i - 1].kind == "ident" and tokens[i - 1].value == "NaN" and (i < 2 or tokens[i - 2].value != ".")
        rhs_nan = i + 1 < n and tokens[i + 1].kind == "ident" and tokens[i + 1].value == "NaN" and (i + 2 >= n or tokens[i + 2].value != ".")
        if lhs_nan or rhs_nan:
            findings.append(Finding(rel, tok.line, "JS202", "direct comparison with `NaN`; use `Number.isNaN(...)`"))
    return findings


def _check_cond_assign(tokens: list[Token], rel: str) -> list[Finding]:
    findings: list[Finding] = []
    n = len(tokens)
    for i, tok in enumerate(tokens):
        if tok.kind != "ident" or tok.value not in ("if", "while"):
            continue
        if i + 1 >= n or tokens[i + 1].value != "(":
            continue
        p_depth = 1
        b_depth = 0
        k_depth = 0
        j = i + 2
        while j < n and p_depth > 0:
            cur = tokens[j]
            if cur.value == "(":
                p_depth += 1
            elif cur.value == ")":
                p_depth -= 1
            elif cur.value in ("{", "${"):
                b_depth += 1
            elif cur.value == "}":
                b_depth = max(0, b_depth - 1)
            elif cur.value == "[":
                k_depth += 1
            elif cur.value == "]":
                k_depth = max(0, k_depth - 1)
            elif cur.kind == "op" and cur.value == "=" and p_depth == 1 and b_depth == 0 and k_depth == 0:
                findings.append(
                    Finding(rel, cur.line, "JS204", f"possible accidental assignment in `{tok.value}` condition")
                )
            j += 1
    return findings


def _is_object_literal_open(tokens: list[Token], i: int) -> bool:
    if i == 0:
        return False
    prev = tokens[i - 1]
    if prev.kind == "ident" and prev.value in NON_OBJ_PREV_IDENT:
        return False
    if prev.kind == "op" and prev.value in OBJ_PREV_OPS:
        return True
    if prev.kind == "punct" and prev.value in OBJ_PREV_PUNCT:
        return True
    if prev.kind == "punct" and prev.value == ":":
        prev2 = tokens[i - 2] if i >= 2 else None
        return not (prev2 and prev2.kind == "ident" and prev2.value == "default")
    return prev.kind == "ident" and prev.value in OBJ_PREV_IDENT


@dataclass
class _ObjFrame:
    is_obj: bool
    keys: dict[tuple[str, str], int]
    expect: bool
    paren: int = 0
    bracket: int = 0


def _extract_obj_key(tokens: list[Token], i: int) -> tuple[str, str, int] | None:
    """Return (namespace, key_name, line) if tokens[i] starts a static object literal entry."""
    n = len(tokens)
    tok = tokens[i]
    ns = "prop"
    idx = i
    if tok.kind == "ident" and tok.value in ("get", "set", "async") and idx + 1 < n:
        mod_next = tokens[idx + 1]
        if mod_next.kind in ("ident", "str", "num"):
            if tok.value in ("get", "set"):
                ns = tok.value
            idx += 1
            tok = tokens[idx]
    if tok.kind not in ("ident", "str", "num"):
        return None
    after: Token | None = tokens[idx + 1] if idx + 1 < n else None
    if after is None or after.value not in (":", "("):
        return None
    key_name = _unquote(tok.value) if tok.kind == "str" else tok.value
    return ns, key_name, tok.line


def _check_duplicate_keys(tokens: list[Token], rel: str) -> list[Finding]:
    findings: list[Finding] = []
    stack: list[_ObjFrame] = []
    for i, tok in enumerate(tokens):
        if tok.value in ("{", "${"):
            is_obj = tok.value == "{" and _is_object_literal_open(tokens, i)
            stack.append(_ObjFrame(is_obj=is_obj, keys={}, expect=is_obj))
            continue
        if tok.value == "}":
            if stack:
                stack.pop()
            continue
        if not stack or not stack[-1].is_obj:
            continue
        top = stack[-1]
        if _update_obj_nesting(top, tok.value):
            continue
        if top.expect and top.paren == 0 and top.bracket == 0:
            entry = _extract_obj_key(tokens, i)
            if entry is not None:
                ns, key_name, ln = entry
                if (ns, key_name) in top.keys:
                    findings.append(
                        Finding(rel, ln, "JS203", f"duplicate object key {key_name!r} (first defined on line {top.keys[(ns, key_name)]})")
                    )
                else:
                    top.keys[(ns, key_name)] = ln
                top.expect = False
            elif tok.value not in ("get", "set", "async", "*"):
                top.expect = False
    return findings


def _update_obj_nesting(top: _ObjFrame, val: str) -> bool:
    if val == "(":
        top.paren += 1
        return True
    if val == ")":
        top.paren = max(0, top.paren - 1)
        return True
    if val == "[":
        top.bracket += 1
        return True
    if val == "]":
        top.bracket = max(0, top.bracket - 1)
        return True
    if val == "," and top.paren == 0 and top.bracket == 0:
        top.expect = True
        return True
    return False


def collect_defined_dom_ids(html_text: str) -> set[str]:
    """Collect every static element ID defined in HTML markup, templates, or JS helpers."""
    ids: set[str] = set()
    for m in re.finditer(r"""\bid\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s>"'=`]+))""", html_text):
        val = m.group(1) or m.group(2) or m.group(3)
        if val:
            ids.add(val)
    for m in re.finditer(r"""\bid\s*:\s*(?:"([^"]+)"|'([^']+)')""", html_text):
        ids.add(m.group(1) or m.group(2))
    for m in re.finditer(r"""\.id\s*=\s*(?:"([^"]+)"|'([^']+)')""", html_text):
        ids.add(m.group(1) or m.group(2))
    for m in re.finditer(r"""setAttribute\s*\(\s*["']id["']\s*,\s*["']([^"']+)["']\s*\)""", html_text):
        ids.add(m.group(1))
    return ids


def _check_dom_ids(tokens: list[Token], rel: str, html_text: str) -> list[Finding]:
    known_ids = collect_defined_dom_ids(html_text)
    findings: list[Finding] = []
    n = len(tokens)
    for i, tok in enumerate(tokens):
        if tok.kind != "ident" or tok.value != "getElementById":
            continue
        if i + 3 >= n or tokens[i + 1].value != "(" or tokens[i + 2].kind != "str" or tokens[i + 3].value != ")":
            continue
        target_id = _unquote(tokens[i + 2].value)
        if target_id and target_id not in known_ids:
            findings.append(
                Finding(rel, tokens[i + 2].line, "JS206", f"getElementById({target_id!r}) references unknown DOM ID")
            )
    return findings


def _rel_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def discover_blocks(root: Path, targets: list[Path] | None = None) -> list[ScriptBlock]:
    """Discover inline `<script>` blocks in `app/*.html` and JS files in `tests/`."""
    blocks: list[ScriptBlock] = []
    if targets:
        for t in targets:
            if t.is_dir():
                blocks.extend(discover_blocks(t))
            elif t.suffix.lower() in (".html", ".htm"):
                blocks.extend(extract_html_scripts(t))
            elif t.suffix.lower() in (".js", ".cjs", ".mjs"):
                blocks.append(ScriptBlock(t, t.read_text(encoding="utf-8"), 1, t.suffix == ".mjs", False))
        return blocks

    for name in HTML_PAGES:
        html_path = root / "app" / name
        if html_path.is_file():
            blocks.extend(extract_html_scripts(html_path))
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        blocks.extend(
            ScriptBlock(js_path, js_path.read_text(encoding="utf-8"), 1, False, False)
            for js_path in sorted(list(tests_dir.glob("*.cjs")) + list(tests_dir.glob("*.js")))
        )
    return blocks


def lint_block(blk: ScriptBlock, root: Path) -> list[Finding]:
    """Run all token-based static lint checks on a single script block."""
    rel = _rel_path(blk.path, root)
    tokens = tokenize_js(blk.source, start_line=blk.start_line)
    raw_lines = blk.path.read_text(encoding="utf-8").splitlines()
    raw_findings: list[Finding] = []
    raw_findings.extend(_check_disallowed(tokens, rel, blk.is_html))
    raw_findings.extend(_check_typeof(tokens, rel))
    raw_findings.extend(_check_nan_compare(tokens, rel))
    raw_findings.extend(_check_cond_assign(tokens, rel))
    raw_findings.extend(_check_duplicate_keys(tokens, rel))
    if blk.is_html:
        raw_findings.extend(_check_dom_ids(tokens, rel, blk.html_text))
    return [f for f in raw_findings if not _is_suppressed(raw_lines, f.line)]


def lint_repo(root: Path = ROOT, targets: list[Path] | None = None) -> list[Finding]:
    """Lint all discovered HTML inline scripts and JS files, returning sorted findings."""
    blocks = discover_blocks(root, targets)
    findings = _check_node_syntax(blocks, root)
    for blk in blocks:
        findings.extend(lint_block(blk, root))
    findings.sort(key=lambda f: (f.path, f.line, f.code))
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Static JavaScript linter for Greenlight.")
    ap.add_argument("--strict", action="store_true", help="Treat warnings (e.g. console.log in app/*.html) as errors")
    ap.add_argument("--root", default=str(ROOT), help="Repository root path")
    ap.add_argument("paths", nargs="*", help="Optional specific HTML/JS files or directories to lint")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    targets = [Path(p).resolve() for p in args.paths] if args.paths else None
    findings = lint_repo(root, targets)

    errors = [f for f in findings if f.severity == "error" or args.strict]
    warnings = [f for f in findings if f.severity == "warning" and not args.strict]

    for f in errors:
        print(f.format(strict=args.strict), file=sys.stderr)
    for f in warnings:
        print(f.format(strict=False))

    if errors:
        print(f"JS LINT FAIL: {len(errors)} error(s), {len(warnings)} warning(s)", file=sys.stderr)
        return 1
    suffix = f" ({len(warnings)} warning(s); run with --strict to enforce)" if warnings else ""
    print(f"ok  js_lint passed (0 errors){suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
