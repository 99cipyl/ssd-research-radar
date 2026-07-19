#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: persist_state.sh DATABASE FINGERPRINT" >&2
  exit 64
fi

database=$1
fingerprint=$2
remote_ref=refs/remotes/origin/radar-state
state_dir=$(mktemp -d)
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

cleanup() {
  git worktree remove --force "$state_dir" >/dev/null 2>&1 || true
}
trap cleanup EXIT

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

if git show-ref --verify --quiet "$remote_ref"; then
  git worktree add --detach "$state_dir" "$remote_ref"
  git -C "$state_dir" switch -C radar-state
else
  git worktree add --detach "$state_dir" "${GITHUB_SHA:-HEAD}"
  git -C "$state_dir" switch --orphan radar-state
  git -C "$state_dir" rm -rf --ignore-unmatch .
fi

cp "$database" "$state_dir/radar.sqlite3"
printf '%s\n' "$fingerprint" > "$state_dir/fingerprint.txt"
cp "$script_dir/state-branch-README.md" "$state_dir/README.md"

git -C "$state_dir" add README.md fingerprint.txt radar.sqlite3
if git -C "$state_dir" diff --cached --quiet; then
  echo "State files are already current"
  exit 0
fi

git -C "$state_dir" commit -m "Update radar state"
git -C "$state_dir" push origin HEAD:radar-state
