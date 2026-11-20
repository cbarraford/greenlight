"""Files the user attaches in the menubar chat: checked, kept privately on greenlight's host, handed to the agent.

The app runs `chat.py send --attach` with one JSON object on stdin:
  {"text": "the message", "files": [{"name": "shot.png", "data": "<base64>"}]}
Only the file's own name comes over, never the folder it was in on the Mac. Each message's files land in
<chat dir>/attachments/<stamp>/ (0700 folders, 0600 files), and are deleted after KEEP_DAYS or on Clear.
Nothing here prints a file's path or contents: the only things said about a file are its name, kind and size.
The app checks the same rules first (Greenlight.swift's chatFile), so a rejection here is a backstop.
"""
import base64, binascii, os, re, secrets, shutil, time

MB = 1 << 20
LIMITS = {"image": 5 * MB, "pdf": 10 * MB, "text": 1 * MB}  # image: what Claude takes per image
KINDS = {"image": "image", "pdf": "PDF", "text": "text"}
MAX_FILES = 5
MAX_TOTAL = 20 * MB
INLINE = 64 << 10  # text this long or shorter rides in the message itself; longer is read from its file
KEEP_DAYS = 7
SUPPORTED = "images (PNG, JPEG, GIF, WebP), PDFs and text files"


class Rejected(ValueError):
    """A file the chat won't take, with the sentence the user sees."""


def size(n):
    return f"{n} B" if n < 1024 else f"{n / 1024:.0f} KB" if n < MB else f"{n / MB:.1f} MB"


def safe_name(name):
    """The file's own name only: no folders, no control characters, no leading dots, at most 80 characters."""
    n = re.split(r"[/\\]", str(name or ""))[-1]
    n = re.sub(r"[\x00-\x1f\x7f]", "", n)
    n = " ".join(n.split()).lstrip(".").strip()
    if len(n) > 80:
        stem, dot, ext = n.rpartition(".")
        n = (stem[: 79 - len(ext)] + "." + ext) if dot and 0 < len(ext) <= 10 else n[:80]
    return n or "file"


def kind(data):
    """image | pdf | text from the bytes themselves (never the name), or None."""
    if data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return "image"
    if data.startswith(b"%PDF-"):
        return "pdf"
    if b"\x00" in data:
        return None
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return "text"


def check(files):
    """[(name, kind, bytes)] for the app's files, or Rejected saying what's wrong with the first bad one."""
    if not isinstance(files, list):
        raise Rejected("The attachments didn't arrive in a form greenlight understands.")
    if len(files) > MAX_FILES:
        raise Rejected(f"Attach up to {MAX_FILES} files at a time.")
    out, total = [], 0
    for f in files:
        name = safe_name(f.get("name") if isinstance(f, dict) else "")
        try:
            data = base64.b64decode((f or {}).get("data") or "", validate=True)
        except (binascii.Error, ValueError, TypeError, AttributeError):
            raise Rejected(f"{name} couldn't be read. Try attaching it again.")
        k = kind(data)
        if not data:
            raise Rejected(f"{name} is empty.")
        if k is None:
            raise Rejected(f"{name} isn't a file the chat can use: attach {SUPPORTED}.")
        if len(data) > LIMITS[k]:
            raise Rejected(f"{name} is {size(len(data))}: {KINDS[k]}s can be up to {size(LIMITS[k])}.")
        total += len(data)
        if total > MAX_TOTAL:
            raise Rejected(f"These files come to more than {size(MAX_TOTAL)}: send fewer at a time.")
        out.append((name, k, data))
    return out


def root(chat_dir):
    return os.path.join(chat_dir, "attachments")


def store(chat_dir, checked):
    """Write one message's files; [{name, kind, size, path}]. Two files with one name get "name-2.ext"."""
    top = root(chat_dir)
    os.makedirs(top, mode=0o700, exist_ok=True)
    os.chmod(top, 0o700)
    d = os.path.join(top, time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3))
    os.mkdir(d, 0o700)
    out, used = [], set()
    for name, k, data in checked:
        n, i = name, 2
        while n.lower() in used:
            stem, dot, ext = name.rpartition(".")
            n = f"{stem}-{i}.{ext}" if dot and stem else f"{name}-{i}"
            i += 1
        used.add(n.lower())
        p = os.path.join(d, n)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        out.append({"name": n, "kind": k, "size": len(data), "path": p})
    return out


def prune(chat_dir, days=KEEP_DAYS):
    """Delete messages' files older than `days`: a resumed session that still mentions one finds it gone."""
    top, cut = root(chat_dir), time.time() - days * 86400
    for e in os.listdir(top) if os.path.isdir(top) else []:
        p = os.path.join(top, e)
        try:
            if os.path.getmtime(p) < cut:
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        except OSError:
            pass


def clear(chat_dir):
    shutil.rmtree(root(chat_dir), ignore_errors=True)


def note(stored):
    """What the agent reads after the user's message: each file's name, kind, size and where it is; short text inline."""
    if not stored:
        return ""
    lines = ["ATTACHED FILES (the user attached these to this message; they are part of it, so use them to answer):"]
    for f in stored:
        head = f"- {f['name']} ({KINDS[f['kind']]}, {size(f['size'])}) at {f['path']}"
        if f["kind"] == "text" and f["size"] <= INLINE:
            with open(f["path"], encoding="utf-8") as fh:
                body = fh.read()
            lines += [head + ", its contents:", f"<<<BEGIN {f['name']}", body.rstrip("\n"), f"END {f['name']}>>>"]
        elif f["kind"] == "text":
            lines.append(head + ": too long to include here, read the file.")
        else:
            lines.append(head + ": open it with your file-reading tool to see it.")
    return "\n".join(lines)


def images(stored):
    return [f["path"] for f in stored if f["kind"] == "image"]
