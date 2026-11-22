"""Pytest suite for scripts/attach.py (validation, safe naming, storage permissions, pruning, and redaction)."""
import base64
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import attach

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 12
GIF_BYTES = b"GIF89a" + b"\x00" * 10
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_size_formatting():
    assert attach.size(512) == "512 B"
    assert attach.size(2048) == "2 KB"
    assert attach.size(3 * attach.MB) == "3.0 MB"


def test_shown_and_safe_name_strip_traversal_controls_and_commas():
    assert attach.shown("../../etc/\x00passwd\n") == "passwd"
    assert attach.shown("   ") == "file"
    assert attach.safe_name("...hidden,file.txt") == "hidden_file.txt"
    assert attach.safe_name("/a/b/c/..secret.png") == "secret.png"
    long_name = "a" * 120 + ".png"
    safe = attach.safe_name(long_name)
    assert len(safe) <= 80 and safe.endswith(".png")
    emoji_name = "🚀" * 70 + ".txt"
    safe_emoji = attach.safe_name(emoji_name)
    assert len(safe_emoji.encode()) <= 200 and safe_emoji.endswith(".txt")


def test_kind_detects_magic_bytes_and_rejects_binary():
    assert attach.kind(PNG_BYTES) == "image"
    assert attach.kind(JPEG_BYTES) == "image"
    assert attach.kind(GIF_BYTES) == "image"
    assert attach.kind(WEBP_BYTES) == "image"
    assert attach.kind(PDF_BYTES) == "pdf"
    assert attach.kind(b"hello utf-8 world\n") == "text"
    assert attach.kind(b"abc\x00def") is None
    assert attach.kind(b"\xff\xfe\xfd") is None


def test_check_validates_shape_count_encoding_empty_type_and_limits():
    with pytest.raises(attach.Rejected, match="form"):
        attach.check("not-a-list")
    with pytest.raises(attach.Rejected, match="up to 5"):
        attach.check([{"name": f"{i}.txt", "data": b64(b"x")} for i in range(6)])
    with pytest.raises(attach.Rejected, match="couldn't be read"):
        attach.check([{"name": "bad.txt", "data": "!!!not-base64!!!"}])
    with pytest.raises(attach.Rejected, match="empty"):
        attach.check([{"name": "empty.txt", "data": ""}])
    with pytest.raises(attach.Rejected, match="isn't a file"):
        attach.check([{"name": "bin.dat", "data": b64(b"\x00\x01\x02")}])
    with pytest.raises(attach.Rejected, match="up to 1.0 MB"):
        attach.check([{"name": "huge.txt", "data": b64(b"a" * (attach.LIMITS["text"] + 1))}])

    checked = attach.check([
        {"name": "/tmp/shot.png", "data": b64(PNG_BYTES)},
        {"name": "notes.txt", "data": b64(b"hello world")},
    ])
    assert [(n, k) for n, k, _ in checked] == [("shot.png", "image"), ("notes.txt", "text")]


def test_store_permissions_dedup_note_prune_and_clear(tmp_path):
    checked = [
        ("dup.txt", "text", b"short inline secret text line here"),
        ("dup.txt", "text", b"second copy"),
        ("pic.png", "image", PNG_BYTES),
    ]
    stored = attach.store(str(tmp_path), checked)
    assert len(stored) == 3
    names = [Path(f["path"]).name for f in stored]
    assert names == ["dup.txt", "dup-2.txt", "pic.png"]
    assert attach.images(stored) == [stored[2]["path"]]

    # Verify private permissions on directory (0700) and files (0600)
    top_mode = stat.S_IMODE(os.stat(attach.root(str(tmp_path))).st_mode)
    file_mode = stat.S_IMODE(os.stat(stored[0]["path"]).st_mode)
    assert top_mode == 0o700 and file_mode == 0o600

    # Verify note() inlines short text with randomized fence and omits binary contents
    rendered = attach.note(stored)
    assert "ATTACHED FILES" in rendered
    assert "short inline secret text line here" in rendered
    assert "open it with your file-reading tool" in rendered

    # Prune old directories and clear all
    old_dir = Path(attach.root(str(tmp_path))) / "20200101-000000-abcdef"
    old_dir.mkdir()
    os.utime(old_dir, (1, 1))
    attach.prune(str(tmp_path), days=7)
    assert not old_dir.exists()
    assert Path(stored[0]["path"]).exists()

    attach.clear(str(tmp_path))
    assert not Path(attach.root(str(tmp_path))).exists()


def test_mentions_needles_showing_and_hide_redact_leaked_content(tmp_path):
    secret_line = "CONFIDENTIAL_TOKEN_ALPHA_9876543210_PAYLOAD"
    stored = attach.store(str(tmp_path), [("secret.txt", "text", (secret_line + "\n").encode())])
    nds = attach.needles(str(tmp_path))

    assert attach.mentions(str(tmp_path), {"detail": f"cat {stored[0]['path']}"})
    assert attach.mentions(str(tmp_path), {"call": "grep -R foo ./attachments/x"})
    assert not attach.mentions(str(tmp_path), {"detail": "cat app/attachments/foo.png"})

    assert attach.showing(secret_line, nds)
    assert attach.showing(secret_line[:20] + "…", nds)
    assert attach.showing("…" + secret_line[-20:], nds)
    assert not attach.showing("unrelated normal stdout line", nds)

    event = {
        "out": f"safe line\n{secret_line}",
        "full": f"keep me\n{secret_line}\n{stored[0]['path']}",
        "detail": "./attachments/secret.txt",
    }
    assert attach.hide(str(tmp_path), event, nds) is True
    assert event["out"] == attach.HIDDEN
    assert event["full"] == f"keep me\n{attach.HIDDEN}\n{attach.HIDDEN}"
    assert event["detail"] == attach.HIDDEN
