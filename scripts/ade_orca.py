"""Read-only Orca source for importing repos and workspace goals.

The native backend owns all live workspace operations. Orca is contacted only
when the user imports existing data, or during the optional first-run import.
"""
NAME = "orca"


def _gl():
    import greenlight  # here, not at the top: greenlight imports ade, which imports the backends
    return greenlight


def worktrees():
    return _gl().orca_cli("worktree", "ps", "--limit", "10000")["worktrees"]


def repos():
    return _gl().orca_cli("repo", "list")
