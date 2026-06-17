"""Git-backed checkpointer for hypothesis branching.

Uses git plumbing (write-tree / commit-tree / update-ref) to snapshot the
full working tree into a side ref WITHOUT moving HEAD or leaving the working
tree dirty.

See design.v3.md §"Checkpoint Branching".

The concrete `GitCheckpointer` is a reference implementation for git-backed
environments (SWE-bench testbeds). It is constructed by the task adapter
and injected into the activator — keeping all git-specific logic out of the
core loop.

Why this matters for the hypothesis loop:
  Each hypothesis can choose which prior state to branch from. When H1 makes a
  partially-correct change but the oracle still fails, H2 can either continue
  from H1's state (build on it) or revert to 'base' (discard it and try a
  different approach). This prevents "cumulative patch corruption" where each
  hypothesis patches the previous failed patch.
"""
from __future__ import annotations

from typing import Callable, Optional


class NullCheckpointer:
    """No-op checkpointer — used when the environment has no snapshot support.

    The activator degrades gracefully to pure forward-editing (today's behavior).
    """

    def snapshot(self, label: str) -> bool:
        return False

    def restore(self, label: str) -> bool:
        return False

    def labels(self) -> list[str]:
        return []


class GitCheckpointer:
    """Git-backed checkpointer for SWE-bench testbeds.

    Uses git plumbing (write-tree / commit-tree / update-ref) to snapshot the
    full working tree into a side ref WITHOUT moving HEAD or leaving the working
    tree staged. Restore uses read-tree + checkout-index + clean.

    The harness evaluates by running tests against the working tree (not via
    `git diff`), so these operations are invisible to the final evaluation —
    only the working-tree file contents matter.

    `exec_fn(argv, workdir) -> (exit_code, output)` runs a command in the target
    environment (e.g. docker exec). Injected so this class has no docker import.
    """

    def __init__(
        self,
        exec_fn: Callable[..., tuple[int, str]],
        workdir: str = "/testbed",
    ) -> None:
        self._exec = exec_fn
        self._workdir = workdir
        self._labels: list[str] = []
        self._ok = self._verify_git()

    def _run(self, sh: str) -> tuple[int, str]:
        """Run a shell snippet in the workdir via the injected exec fn."""
        return self._exec(["bash", "-lc", f"cd {self._workdir} && {sh}"], self._workdir)

    def _verify_git(self) -> bool:
        code, _ = self._run("git rev-parse --is-inside-work-tree")
        return code == 0

    def available(self) -> bool:
        return self._ok

    def snapshot(self, label: str) -> bool:
        """Capture working tree into refs/cat/<label> without disturbing it."""
        if not self._ok:
            return False
        ref = f"refs/cat/{label}"
        # Stage everything → write a tree object → wrap in a commit pointing at
        # current HEAD as parent → store under our private ref → unstage (so the
        # working-tree-vs-HEAD diff the harness/oracle relies on is unchanged).
        sh = (
            "git add -A >/dev/null 2>&1; "
            "_tree=$(git write-tree); "
            f'_commit=$(git commit-tree "$_tree" -m "cat-cp-{label}" -p HEAD 2>/dev/null); '
            f'git update-ref {ref} "$_commit" && '
            "git reset -q HEAD >/dev/null 2>&1; "
            'echo "OK:$_commit"'
        )
        code, out = self._run(sh)
        ok = code == 0 and "OK:" in out and len(out.split("OK:")[-1].strip()) >= 7
        if ok and label not in self._labels:
            self._labels.append(label)
        return ok

    def restore(self, label: str) -> bool:
        """Reset working tree to the snapshot stored under `label`."""
        if not self._ok or label not in self._labels:
            return False
        ref = f"refs/cat/{label}"
        # read-tree loads the snapshot into the index; checkout-index -f -a writes
        # every indexed file to the working tree (overwriting); clean -fdq removes
        # files that exist now but not in the snapshot; reset puts the index back
        # to HEAD so subsequent status/diff stays meaningful.
        sh = (
            f"git read-tree {ref} && "
            "git checkout-index -f -a && "
            "git clean -fdq >/dev/null 2>&1; "
            "git reset -q HEAD >/dev/null 2>&1; "
            "echo OK"
        )
        code, out = self._run(sh)
        return code == 0 and "OK" in out

    def labels(self) -> list[str]:
        return list(self._labels)


def build_git_checkpointer(
    exec_fn: Callable[..., tuple[int, str]],
    workdir: str = "/testbed",
) -> "GitCheckpointer | NullCheckpointer":
    """Build a GitCheckpointer bound to a docker container (SWE-bench adapter).

    Returns a `GitCheckpointer` if the workdir is a git repo,
    else a `NullCheckpointer`. The activator only sees the `Checkpointer`
    protocol — this keeps git/docker specifics out of the core loop.

    exec_fn(argv, workdir) -> (exit_code, output) runs commands in the
    target environment (e.g. wrapping container.exec_run from the docker SDK).
    """
    cp = GitCheckpointer(exec_fn, workdir=workdir)
    return cp if cp.available() else NullCheckpointer()
