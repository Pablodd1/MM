"""Push a directory to GitHub over the REST API when `git push` cannot complete.

This exists because `git send-pack` disconnects against one specific repository while plain
HTTPS to the API works fine from the same machine. Rather than claim the push happened, the
script creates real blobs, a tree, a commit and a ref, then reads the ref back to prove it.

Usage:
    GITHUB_TOKEN=... python scripts/push_via_api.py OWNER/REPO /path/to/dir "commit message"

Files come from `git ls-files` when the directory is a git checkout (so .gitignore is respected),
otherwise from a walk that skips .git, .venv, __pycache__ and the local-only data directories.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
SKIP_DIRS = {".git", ".venv", "__pycache__", "audio", "transcripts", "runtime", ".pytest_cache"}


def call(method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{API}{path}", data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        return exc.code, {"error": detail}


def tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                             capture_output=True, check=True).stdout
        names = [n for n in out.decode().split("\0") if n]
        return [root / n for n in names if (root / n).is_file()]
    except subprocess.CalledProcessError:
        files = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if SKIP_DIRS & set(path.parts):
                continue
            files.append(path)
        return files


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    repo, root_s, message = sys.argv[1], sys.argv[2], sys.argv[3]
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("GITHUB_TOKEN is not set")
        return 2
    root = Path(root_s).resolve()

    status, ref = call("GET", f"/repos/{repo}/git/ref/heads/main", token)
    parent_sha = ref.get("object", {}).get("sha") if status == 200 else None
    base_tree = None
    if parent_sha:
        st, commit = call("GET", f"/repos/{repo}/git/commits/{parent_sha}", token)
        base_tree = commit.get("tree", {}).get("sha") if st == 200 else None
        print(f"parent commit: {parent_sha[:8]}")
    else:
        print("empty repository — creating the first commit")

    files = tracked_files(root)
    print(f"{len(files)} files to upload from {root}")
    entries = []
    for i, path in enumerate(files, 1):
        rel = path.relative_to(root).as_posix()
        content = base64.b64encode(path.read_bytes()).decode()
        status, blob = call("POST", f"/repos/{repo}/git/blobs", token,
                            {"content": content, "encoding": "base64"})
        if status not in (200, 201):
            print(f"  FAILED blob {rel}: {status} {blob.get('error', '')[:160]}")
            return 1
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        if i % 20 == 0 or i == len(files):
            print(f"  uploaded {i}/{len(files)}")

    tree_payload: dict = {"tree": entries}
    if base_tree:
        tree_payload["base_tree"] = base_tree
    status, tree = call("POST", f"/repos/{repo}/git/trees", token, tree_payload)
    if status not in (200, 201):
        print(f"FAILED tree: {status} {tree.get('error', '')[:300]}")
        return 1
    print(f"tree {tree['sha'][:8]}")

    commit_payload = {"message": message, "tree": tree["sha"]}
    if parent_sha:
        commit_payload["parents"] = [parent_sha]
    status, commit = call("POST", f"/repos/{repo}/git/commits", token, commit_payload)
    if status not in (200, 201):
        print(f"FAILED commit: {status} {commit.get('error', '')[:300]}")
        return 1
    print(f"commit {commit['sha'][:8]}")

    if parent_sha:
        status, updated = call("PATCH", f"/repos/{repo}/git/refs/heads/main", token,
                               {"sha": commit["sha"], "force": False})
    else:
        status, updated = call("POST", f"/repos/{repo}/git/refs", token,
                               {"ref": "refs/heads/main", "sha": commit["sha"]})
    if status not in (200, 201):
        print(f"FAILED ref update: {status} {updated.get('error', '')[:300]}")
        return 1
    print(f"ref main -> {updated.get('object', {}).get('sha', '')[:8]}")

    # Verify by reading back, not by trusting the write.
    st, files_now = call("GET", f"/repos/{repo}/git/trees/main?recursive=1", token)
    count = len([e for e in files_now.get("tree", []) if e.get("type") == "blob"])
    print(f"VERIFIED: main now has {count} files at {commit['sha'][:8]}")
    return 0 if count else 1


if __name__ == "__main__":
    sys.exit(main())
