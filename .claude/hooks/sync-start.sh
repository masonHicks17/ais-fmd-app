#!/usr/bin/env bash
# SessionStart hook: pull the latest from GitHub so the laptop and the desktop
# start from the same place. Prints plain text, which Claude Code adds to the
# session's context. Never fails the session.

cd "$CLAUDE_PROJECT_DIR" 2>/dev/null || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

if ! git fetch --prune --quiet origin 2>/dev/null; then
  echo "[sync] Could not reach GitHub; working from local state."
  exit 0
fi

branch=$(git symbolic-ref --short -q HEAD)
if [ -z "$branch" ]; then
  echo "[sync] Detached HEAD; nothing pulled."
  exit 0
fi

if ! git rev-parse -q --verify "@{u}" >/dev/null 2>&1; then
  echo "[sync] Branch $branch has no upstream on GitHub yet; nothing pulled."
else
  read -r ahead behind < <(git rev-list --left-right --count HEAD...@{u})
  if [ "$behind" -eq 0 ]; then
    echo "[sync] $branch is up to date with GitHub."
  elif [ "$ahead" -eq 0 ]; then
    if git merge --ff-only --quiet "@{u}" 2>/dev/null; then
      echo "[sync] Pulled $behind new commit(s) into $branch from GitHub."
    else
      echo "[sync] $branch is $behind commit(s) behind GitHub, but local edits block a fast-forward. Tell the user before touching those files."
    fi
  else
    echo "[sync] $branch has diverged from GitHub ($ahead local, $behind remote). Ask the user how to reconcile before committing."
  fi
fi

# Work pushed from the other machine to a different branch is easy to miss.
since=$(( $(date +%s) - 86400 ))
recent=$(git for-each-ref --format='%(committerdate:unix) %(refname:lstrip=3)' refs/remotes/origin \
  | awk -v s="$since" -v b="$branch" '$1 > s && $2 != b && $2 != "HEAD" {print $2}' | tr '\n' ' ')
if [ -n "$recent" ]; then
  echo "[sync] Other branches updated on GitHub in the last 24h: $recent"
fi
exit 0
