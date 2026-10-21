#!/usr/bin/env python3
"""Read local Git readiness as JSON. No fetch, writes, URLs, or credential access.

Requires Python 3.9+ and Git. Exit 0 means inspection completed, not ready to sync;
exit 2 means a Git operation failed. Inspect the fields before choosing an action.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


DEPENDENCIES = (
    "pyproject.toml", "uv.lock", "requirements.txt", "poetry.lock",
    "package.json", "pnpm-lock.yaml", "package-lock.json", "yarn.lock",
    "environment.yml", "Cargo.lock", "go.mod", "global.json",
    "Dockerfile", "compose.yaml", ".python-version", ".node-version",
)
HANDOFFS = (
    "docs/STATUS.md", "docs/progress.md", "STATUS.md", "HANDOFF.md",
    "SESSION_HANDOFF.md", "学习任务规划与进度.md",
    ".workflow/state.json", ".workflow/project.md",
)


class GitInspectionError(Exception):
    pass


def git(root, *args, required=True):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"  # Partial clones must not contact their promisor remote.
    # Ignore ambient Git routing; inspect exactly the requested checkout.
    for key in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE", "GIT_PREFIX",
    ):
        env.pop(key, None)
    result = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root), *args],
        capture_output=True, env=env, timeout=30,
    )
    if result.returncode != 0:
        if required:
            # Raw Git diagnostics can contain sensitive configuration values.
            raise GitInspectionError("git_command_failed:" + args[0])
        return None
    return result.stdout.decode("utf-8", errors="replace").rstrip("\r\n")


def changed_files(porcelain):
    counts = dict(total=0, staged=0, unstaged=0, untracked=0, conflicts=0)
    entries = iter(porcelain.split("\0"))
    for entry in entries:
        if not entry:
            continue
        xy = entry[:2]
        counts["total"] += 1
        if xy == "??":
            counts["untracked"] += 1
        elif xy in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}:
            counts["conflicts"] += 1
        else:
            counts["staged"] += int(xy[0] != " ")
            counts["unstaged"] += int(xy[1] != " ")
        if "R" in xy or "C" in xy:
            next(entries, None)  # A rename/copy has a second NUL-delimited path.
    return counts


def git_path(root, value):
    result = Path(git(root, "rev-parse", "--git-path", value))
    return result if result.is_absolute() else root / result


def marker_files(root, names, repository=False):
    """Known candidates only; never read contents or descend ignored directories."""
    if not repository:
        return sorted(name for name in names if (root / name).is_file())
    paths = git(
        root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--",
        *(":(glob)**/" + name for name in names),
    )
    return sorted({
        name for name in paths.split("\0") if name and (root / name).is_file()
    })


def inspect(path):
    root = Path(path).expanduser().resolve()
    data = {
        "schema_version": 1,
        "requested_path": str(root),
        "git_available": shutil.which("git") is not None,
        "is_repository": False,
        "snapshot_scope": "local_cached_refs_no_fetch",
    }
    if not root.is_dir():
        data["error"] = "path_not_directory"
        return data
    data["instruction_scope"] = "root_only_check_ancestors_and_target_separately"
    data["candidate_scope"] = "known_names_at_requested_root"
    data["instruction_files"] = marker_files(root, ("AGENTS.md", "AGENTS.override.md"))
    data["dependency_markers"] = marker_files(root, DEPENDENCIES)
    data["handoff_candidates"] = marker_files(root, HANDOFFS)
    if not data["git_available"]:
        data["error"] = "git_not_found"
        return data
    inside = git(root, "rev-parse", "--is-inside-work-tree", required=False)
    if inside != "true":
        data["error"] = "not_worktree_or_git_refused_access"
        return data
    root = Path(git(root, "rev-parse", "--show-toplevel")).resolve()
    data.update(is_repository=True, root=str(root))
    data["branch"] = git(root, "symbolic-ref", "--quiet", "--short", "HEAD", required=False)
    data["head"] = git(root, "rev-parse", "--verify", "HEAD", required=False)
    data["detached_head"] = data["head"] is not None and data["branch"] is None
    git_dir = Path(git(root, "rev-parse", "--absolute-git-dir")).resolve()
    common = Path(git(root, "rev-parse", "--git-common-dir"))
    common = common.resolve() if common.is_absolute() else (root / common).resolve()
    data["is_linked_worktree"] = git_dir != common
    # Preserve leading spaces: porcelain's first two columns encode the state.
    status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    data["changes"] = changed_files(status)
    data["in_progress"] = [
        name for name, markers in (
            ("merge", ("MERGE_HEAD",)),
            ("rebase", ("rebase-merge", "rebase-apply")),
            ("cherry_pick", ("CHERRY_PICK_HEAD",)),
            ("revert", ("REVERT_HEAD",)),
        ) if any(git_path(root, marker).exists() for marker in markers)
    ]
    data["remotes"] = git(root, "remote").splitlines()
    data["stash_count"] = len(git(root, "stash", "list", "--format=%gd").splitlines())
    data["upstream"] = git(
        root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", required=False,
    ) if data["head"] and data["branch"] else None
    data["ahead"] = data["behind"] = None
    if data["upstream"]:
        counts = git(root, "rev-list", "--left-right", "--count", "HEAD...@{upstream}")
        data["ahead"], data["behind"] = map(int, counts.split())
    data["instruction_files"] = [
        name for name in ("AGENTS.md", "AGENTS.override.md") if (root / name).is_file()
    ]
    data["candidate_scope"] = "known_names_tracked_and_untracked_nonignored_no_submodule_recursion"
    data["dependency_markers"] = marker_files(root, DEPENDENCIES, repository=True)
    data["handoff_candidates"] = marker_files(root, HANDOFFS, repository=True)
    data["cloud_folder_hint"] = any(
        part.lower() == "dropbox" or part.lower().startswith("onedrive") for part in root.parts
    )
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=".", help="Project directory; default: current directory")
    args = parser.parse_args()
    try:
        data = inspect(args.path)
        code = 0
    except (OSError, subprocess.TimeoutExpired, GitInspectionError) as exc:
        error = str(exc) if isinstance(exc, GitInspectionError) else type(exc).__name__
        data = {"schema_version": 1, "error": error}
        code = 2
    print(json.dumps(data, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
