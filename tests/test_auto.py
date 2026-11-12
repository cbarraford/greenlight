import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import os, tempfile; os.environ.update(GREENLIGHT_HOME=tempfile.mkdtemp(), GREENLIGHT_ME="octo")  # before greenlight: never the real ~/.greenlight or glab
import json, os, pathlib, sys, tempfile, time
import auto

t = lambda s: time.strptime(s, "%Y-%m-%d %H:%M")  # 2026-10-23 is a Friday (dow 5)
assert auto.due("*/30 * * * *", t("2026-10-23 03:30")) and not auto.due("*/30 * * * *", t("2026-10-23 03:31"))
assert auto.due("30 7 * * 1", t("2026-10-26 07:30")) and not auto.due("30 7 * * 1", t("2026-10-23 07:30"))
assert auto.due("0 9 * * 1-5", t("2026-10-23 09:00")) and not auto.due("0 9 * * 1-5", t("2026-10-24 09:00"))
assert auto.due("0 0 * * 7", t("2026-10-25 00:00")) and auto.due("0 0 * * 0", t("2026-10-25 00:00"))
assert auto.due("0 0 1 * 5", t("2026-10-23 00:00"))  # dom and dow both set: either matches, like cron
assert auto.due("15,45 8-10 * 10 *", t("2026-10-23 09:45")) and not auto.due("15,45 8-10 * 10 *", t("2026-10-23 11:45"))

# a run: precheck skip, then claude hitting its limit falls back to codex
auto.STATE = pathlib.Path(tempfile.mkdtemp()); auto.FILE = auto.STATE / "automations.json"; auto.DIR = auto.STATE / "auto"
auto.save({"x": {"schedule": "* * * * *", "prompt": "hi", "precheck": "exit 1"}})
auto.execute("x")
calls = []
auto.agent = lambda a, e, log: (calls.append(e), ("You've hit your usage limit", 1) if e == "claude" else ("done", 0))[1]
auto.save({"x": {"schedule": "* * * * *", "prompt": "hi", "precheck": "true"}})
auto.execute("x")
runs = [json.loads(l) for l in (auto.DIR / "runs.jsonl").read_text().splitlines()]
assert [r["status"] for r in runs] == ["skipped", "ok"] and runs[1]["engine"] == "codex" and calls == ["claude", "codex"], runs
assert not auto.alive("x")
print("test_auto ok")
import plistlib  # the macOS timer's plist parses and runs `tick`
pl = plistlib.loads(auto.PLIST.format(exe="/py", path="/a.py", env_path="/bin").encode())
assert pl["ProgramArguments"] == ["/py", "/a.py", "tick"] and pl["StartInterval"] == 60
print("plist ok")
