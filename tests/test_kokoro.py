"""Unit tests for app/kokoro.py binary PCM framing, stdout isolation, and error handling."""
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_kokoro_framing_and_error_recovery(tmp_path):
    """Run app/kokoro.py with stubbed numpy & kokoro_mlx to verify length-prefixed PCM and error fallback."""
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    (stub_dir / "numpy.py").write_text(
        "import struct\n"
        "class Arr:\n"
        "    def __init__(self, vals): self.vals = vals\n"
        "    def __mul__(self, k): return Arr([v * k for v in self.vals])\n"
        "    def astype(self, _dt): return self\n"
        "    def tobytes(self): return b''.join(struct.pack('<h', int(v)) for v in self.vals)\n"
        "def nan_to_num(a): return [0.0 if (v != v) else v for v in a]\n"
        "def clip(a, lo, hi): return Arr([max(lo, min(hi, v)) for v in a])\n",
        encoding="utf-8",
    )
    (stub_dir / "kokoro_mlx.py").write_text(
        "class _Out:\n"
        "    def __init__(self, audio): self.audio = audio\n"
        "class KokoroTTS:\n"
        "    @classmethod\n"
        "    def from_pretrained(cls):\n"
        "        print('noisy library stdout that must go to stderr')\n"
        "        return cls()\n"
        "    def generate(self, text, voice='af_heart'):\n"
        "        if text == 'BOOM': raise RuntimeError('bad text')\n"
        "        return _Out([0.0, 0.5, -1.5, float('nan')])\n",
        encoding="utf-8",
    )

    env = {**os.environ, "PYTHONPATH": f"{stub_dir}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"}
    stdin_data = (
        json.dumps({"text": "hello", "voice": "af_heart"}) + "\n"
        + "{not valid json\n"
        + json.dumps({"text": "BOOM"}) + "\n"
    ).encode()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "app" / "kokoro.py")],
        input=stdin_data,
        capture_output=True,
        env=env,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert b"noisy library stdout" in proc.stderr

    # Parse 3 length-prefixed frames from stdout
    buf = proc.stdout
    frames = []
    pos = 0
    while pos < len(buf):
        (length,) = struct.unpack("<I", buf[pos : pos + 4])
        pos += 4
        frames.append(buf[pos : pos + length])
        pos += length
    assert len(frames) == 3
    samples = struct.unpack("<4h", frames[0])
    assert samples == (0, 16383, -32767, 0)
    assert frames[1] == b""
    assert frames[2] == b""
