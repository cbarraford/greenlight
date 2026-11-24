"""Orca backend (GREENLIGHT_ADE=orca): ade.py's contract answered by the Orca CLI.

Orca is the one backend whose own CLI is the shape every row is in, so `call` passes its arguments through whole
(placement first: a `worktree create` stays on this machine, greenlight.place_here). The typed methods are the
same calls, for code that asks the contract instead of Orca's flags.
"""
NAME = "orca"


def _gl():
    import greenlight  # here, not at the top: greenlight imports ade, which imports the backends
    return greenlight


def call(*args, timeout=120):
    gl = _gl()
    args, why = gl.place_here(args)
    if why:
        raise gl.PlacementRefused(why)
    return gl.orca_cli(*args, timeout=timeout)


def worktrees():
    return _gl().orca_cli("worktree", "ps", "--limit", "10000")["worktrees"]


def terminals():
    return _gl().orca_cli("terminal", "list")["terminals"]


def read(handle, limit):
    return _gl().orca_cli("terminal", "read", "--terminal", handle, "--limit", str(int(limit)))


def send(handle, text, enter):
    return _gl().orca_cli("terminal", "send", "--terminal", handle, "--text", text, *(["--enter"] if enter else []))


def launch(worktree_id, agent, prompt):
    return call("agent", "launch", "--worktree", f"id:{worktree_id}", "--agent", agent, *(["--prompt", prompt] if prompt else []))


def set_goal(worktree_id, goal):
    return _gl().orca_cli("worktree", "set", "--worktree", f"id:{worktree_id}", "--comment", goal)


def remove(worktree_id, force=False):
    return _gl().orca_cli("worktree", "rm", "--worktree", f"id:{worktree_id}", *(["--force"] if force else []), timeout=300)


def create(repo, name, base=None, comment="", agent=None, prompt=""):
    return call("worktree", "create", "--repo", f"id:{repo}", "--name", name, *(["--base-branch", base] if base else []),
                *(["--comment", comment] if comment else []), *(["--agent", agent] if agent else []),
                *(["--prompt", prompt] if prompt and agent else []))


def repos():
    return _gl().orca_cli("repo", "list")
