# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = [
#   "kokoro-mlx==0.1.4",
#   "en-core-web-sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl",
# ]
# ///
"""Greenlight's Kokoro voice: one JSON line in ({"text", "voice"}), 24 kHz 16-bit mono PCM out, length-prefixed.
The app runs it with `uv run`, over a pipe; it ends when the app does (stdin closes)."""
import json, os, struct, sys

out = os.fdopen(os.dup(1), "wb")
os.dup2(2, 1)  # libraries print to stdout: keep that off the audio pipe
import numpy as np
from kokoro_mlx import KokoroTTS

tts = KokoroTTS.from_pretrained()
for line in sys.stdin:
    try:
        r = json.loads(line)
        a = tts.generate(r["text"], voice=r.get("voice") or "af_heart").audio
        pcm = (np.clip(np.nan_to_num(a), -1, 1) * 32767).astype("<i2").tobytes()
    except Exception as e:
        print("kokoro:", e, file=sys.stderr)
        pcm = b""
    out.write(struct.pack("<I", len(pcm)) + pcm)
    out.flush()
