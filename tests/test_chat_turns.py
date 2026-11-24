"""Tests for chat.py's detached turns: a turn outlives the `send` (and the app or ssh) that started it, and `follow` picks it up."""
import io
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

import chat

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
TID = "0123456789abcdef0123456789abcdef"


@pytest.fixture()
def turns(gl_home):
    d = Path(chat.DIR) / chat.TURNS
    d.mkdir(parents=True)
    return d


def write(tid, *lines, meta=None, raw=""):
    log, m, _ = chat.turn_paths(tid)
    Path(log).write_text("".join(json.dumps(x) + "\n" for x in lines) + raw)
    if meta is not None:
        Path(m).write_text(json.dumps(meta))
    return log


def followed(tid, capsys, **kw):
    rc = chat.follow(tid, poll=0.01, **kw)
    return rc, [json.loads(x) for x in capsys.readouterr().out.splitlines()]


def test_follow_streams_a_finished_turn_from_its_first_event(turns, capsys):
    write(TID, {"e": "start"}, {"e": "text", "delta": "hi"}, {"e": "done", "ok": True, "text": "hi"}, chat.END)
    rc, out = followed(TID, capsys)
    assert rc == 0 and [e["e"] for e in out] == ["start", "text", "done"]  # END itself never reaches the app


def test_follow_waits_for_a_half_written_line_and_a_running_runner(turns, capsys):
    log = write(TID, {"e": "start"}, raw='{"e": "te')
    alive = iter([True, True, False])
    def later(_):
        with open(log, "a") as fh:
            fh.write('xt", "delta": "x"}\n' + json.dumps({"e": "done", "ok": True, "text": "x"}) + "\n" + json.dumps(chat.END) + "\n")
        return next(alive)
    with mock.patch.object(chat, "runner_alive", side_effect=later):
        rc, out = followed(TID, capsys)
    assert rc == 0 and [e["e"] for e in out] == ["start", "text", "done"]


def test_a_turn_whose_runner_died_without_a_reply_gets_one(turns, capsys):
    write(TID, {"e": "start"}, "not json", meta={"pid": 1, "birth": "gone"})  # a bad line is skipped
    with mock.patch.object(chat.auto, "process_start", return_value="other"):
        rc, out = followed(TID, capsys)
    assert rc == 0 and out[-1]["e"] == "done" and out[-1]["ok"] is False and "stopped" in out[-1]["text"]


def test_an_end_without_done_also_gets_a_failed_reply(turns, capsys):
    write(TID, {"e": "start"}, chat.END)  # the runner crashed in the turn: its finally still wrote END
    rc, out = followed(TID, capsys)
    assert rc == 0 and [e["e"] for e in out] == ["start", "done"] and out[-1]["ok"] is False


@pytest.mark.parametrize("tid", [TID, "../../etc/passwd", "", "ABC"])
def test_follow_of_an_unknown_or_bad_id_is_3(turns, tid):
    assert chat.follow(tid) == 3


def test_runner_alive_by_identity_and_a_grace_period_before_it(turns):
    log = write(TID)
    assert chat.runner_alive(TID)  # just started: no identity on file yet
    os.utime(log, (time.time() - chat.TURN_GRACE - 1,) * 2)
    assert not chat.runner_alive(TID)
    write(TID, meta={"pid": os.getpid(), "birth": chat.auto.process_start(os.getpid())})
    assert chat.runner_alive(TID)
    write(TID, meta={"pid": os.getpid(), "birth": "reused-pid"})
    assert not chat.runner_alive(TID)


def test_start_turn_detaches_the_runner_and_keeps_the_message_private(turns):
    with mock.patch.object(chat.subprocess, "Popen", return_value=mock.Mock(pid=4242)) as popen, \
         mock.patch.object(chat.auto, "process_start", return_value="b1"):
        tid = chat.start_turn(["send", "--model", "claude"], "the message")
    argv, kw = popen.call_args.args[0], popen.call_args.kwargs
    assert argv[1:] == [os.path.realpath(chat.__file__), "_run", tid, "send", "--model", "claude"]
    assert kw["start_new_session"] is True and kw["stdin"] is subprocess.DEVNULL and kw["stderr"] is subprocess.DEVNULL
    log, meta, inp = chat.turn_paths(tid)
    assert Path(inp).read_text() == "the message"
    assert all(stat.S_IMODE(os.stat(f).st_mode) == 0o600 for f in (log, meta, inp))
    assert stat.S_IMODE(os.stat(turns).st_mode) == 0o700
    assert json.loads(Path(meta).read_text()) == {"pid": 4242, "birth": "b1"}


def test_run_turn_reads_then_deletes_the_message_and_always_ends(turns, capsys):
    inp = chat.turn_paths(TID)[2]
    Path(inp).write_text("hello")
    seen = {}
    def fake(args):
        seen["args"], seen["stdin"] = args, sys.stdin.read()
        raise RuntimeError("boom")
    with mock.patch.object(chat, "dispatch", side_effect=fake), mock.patch.object(chat.signal, "signal") as sig, \
         pytest.raises(RuntimeError):
        chat.run_turn(TID, ["send"])
    assert seen == {"args": ["send"], "stdin": "hello"} and not os.path.exists(inp)
    assert sig.call_args.args == (signal.SIGHUP, signal.SIG_IGN)
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == chat.END


def test_prune_keeps_live_and_recent_turns(turns):
    old = time.time() - chat.TURN_KEEP - 60
    for tid, meta in (("a" * 32, {"pid": 1, "birth": "dead"}), ("b" * 32, {"pid": 1, "birth": "live"}), ("c" * 32, {"pid": 1, "birth": "dead"})):
        for f in chat.turn_paths(tid)[:2]:
            Path(f).write_text(json.dumps(meta))
        if tid != "c" * 32:
            os.utime(chat.turn_paths(tid)[0], (old, old))
    with mock.patch.object(chat.auto, "process_start", return_value="live"):
        assert chat.prune_turns() == ["a" * 32]
        assert sorted(chat.prune_turns(everything=True)) == ["c" * 32]  # Clear: every finished one, however new
    assert sorted(p.name for p in turns.iterdir()) == ["b" * 32 + ".json", "b" * 32 + ".jsonl"]


@pytest.mark.parametrize("cmd", sorted(chat.QUICK))
def test_quick_commands_answer_inline(gl_home, monkeypatch, cmd):
    monkeypatch.setattr(sys, "argv", ["chat.py", cmd])
    with mock.patch.object(chat, "dispatch") as d, mock.patch.object(chat, "start_turn") as st:
        chat.main()
    assert d.call_args.args == ([cmd],) and not st.called


@pytest.mark.parametrize("argv,stdin", [(["send", "--model", "m"], "msg"), (["compact"], ""), ([], "msg")])
def test_turns_start_detached_print_their_id_then_follow(gl_home, monkeypatch, capsys, argv, stdin):
    monkeypatch.setattr(sys, "argv", ["chat.py", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO("msg"))
    with mock.patch.object(chat, "start_turn", return_value=TID) as st, mock.patch.object(chat, "follow", return_value=0) as fo, \
         pytest.raises(SystemExit) as e:
        chat.main()
    assert st.call_args.args == (argv, stdin) and fo.call_args.args == (TID,) and e.value.code == 0
    assert json.loads(capsys.readouterr().out) == {"e": "turn", "id": TID}


def test_clear_deletes_finished_turns(gl_home, turns, monkeypatch):
    write(TID, {"e": "done"}, chat.END, meta={"pid": 1, "birth": "dead"})
    monkeypatch.setattr(sys, "argv", ["chat.py", "new"])
    chat.main()
    assert not any(turns.iterdir())


def test_a_turn_outlives_its_killed_send_and_follow_gets_the_reply(gl_home, tmp_path):
    """The real thing: the app quits (its send is killed) mid-turn, and after relaunch `follow` brings the reply."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text("#!" + sys.executable + "\nimport json, sys, time\nsys.stdin.read()\n"
                    "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's-1'}), flush=True)\ntime.sleep(2)\n"
                    "print(json.dumps({'type': 'result', 'result': 'survived', 'is_error': False, 'session_id': 's-1'}), flush=True)\n")
    fake.chmod(0o755)
    env = os.environ | {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "CHAT_DIR": str(tmp_path / "chat")}
    send = subprocess.Popen([sys.executable, str(SCRIPTS / "chat.py"), "send", "--model", "claude"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, env=env)
    send.stdin.write("hello")
    send.stdin.close()
    tid = json.loads(send.stdout.readline())["id"]
    send.terminate()  # what applicationWillTerminate does to the app's ssh
    send.wait(10)
    out = subprocess.run([sys.executable, str(SCRIPTS / "chat.py"), "follow", tid], capture_output=True, text=True, env=env, timeout=60)
    done = [json.loads(x) for x in out.stdout.splitlines()][-1]
    assert out.returncode == 0 and done["e"] == "done" and done["ok"] is True and done["text"] == "survived"


def test_a_runner_killed_while_its_own_send_follows_is_reaped_and_gets_a_reply(turns, capsys, tmp_path):
    """send is the runner's parent: a SIGKILLed runner stays its zombie (same pid, same start time) until reaped."""
    stub = tmp_path / "runner.py"
    stub.write_text("import json, time\nprint(json.dumps({'e': 'start'}), flush=True)\ntime.sleep(60)\n")
    with mock.patch.object(chat, "__file__", str(stub)):
        tid = chat.start_turn([], "hi")
    pid = json.loads(Path(chat.turn_paths(tid)[1]).read_text())["pid"]
    deadline = time.time() + 10
    while not Path(chat.turn_paths(tid)[0]).read_text() and time.time() < deadline:
        time.sleep(0.02)
    os.kill(pid, signal.SIGKILL)
    got = {}
    t = threading.Thread(target=lambda: got.update(rc=chat.follow(tid, poll=0.01)), daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "follow kept waiting on its dead runner's zombie"
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert got["rc"] == 0 and [e["e"] for e in out] == ["start", "done"] and out[-1]["ok"] is False and "stopped" in out[-1]["text"]


def test_a_runner_without_a_start_time_is_judged_by_its_pid(turns):
    write(TID, meta={"pid": os.getpid(), "birth": None})  # ps failed when it started: alive while the pid is
    assert chat.runner_alive(TID)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    write(TID, meta={"pid": proc.pid, "birth": None})
    assert not chat.runner_alive(TID)


def test_a_turn_cleared_before_its_runner_starts_never_runs(gl_home, turns, capsys, monkeypatch):
    monkeypatch.setenv(chat.TURN_GEN, chat.generation_unlocked())
    with chat.scope_lock():
        chat.rotate_generation_unlocked()  # Clear, between send starting the runner and the runner getting going
    Path(chat.turn_paths(TID)[2]).write_text("old request")
    with mock.patch.object(chat, "dispatch") as d, mock.patch.object(chat.signal, "signal"):
        chat.run_turn(TID, ["send"])
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert not d.called and out[0]["e"] == "done" and out[0]["text"] == "Cleared before it started." and out[-1] == chat.END
    assert chat.TURN_GEN not in os.environ  # the harness it would start doesn't inherit it


def test_a_turn_in_the_same_conversation_runs(gl_home, turns, capsys, monkeypatch):
    monkeypatch.setenv(chat.TURN_GEN, chat.generation_unlocked())
    Path(chat.turn_paths(TID)[2]).write_text("msg")
    with mock.patch.object(chat, "dispatch") as d, mock.patch.object(chat.signal, "signal"):
        chat.run_turn(TID, ["send"])
    assert d.called


def test_start_turn_passes_the_conversation_generation_to_its_runner(turns):
    with mock.patch.object(chat.subprocess, "Popen", return_value=mock.Mock(pid=4242)) as popen, \
         mock.patch.object(chat.auto, "process_start", return_value="b1"):
        tid = chat.start_turn(["send"], "m")
    assert popen.call_args.kwargs["env"][chat.TURN_GEN] == chat.generation_unlocked() and chat.SPAWNED[tid].pid == 4242
