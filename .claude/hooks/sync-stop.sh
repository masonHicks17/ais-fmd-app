#!/usr/bin/env bash
# Stop hook: when Claude finishes a turn, commit any uncommitted work as a WIP
# commit and push the branch, so the other machine can pick it up.
# Skips main, detached HEAD, and in-progress merges/rebases. Never fails the turn.

cd "$CLAUDE_PROJECT_DIR" 2>/dev/null || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

msg() { printf '{"systemMessage": "[sync] %s"}\n' "$1"; exit 0; }

branch=$(git symbolic-ref --short -q HEAD)
[ -z "$branch" ] && exit 0
# main, and the branch Streamlit Cloud deploys from, are never auto-pushed:
# a WIP commit there would redeploy the live app without review.
case "$branch" in main|master|sandbox/mvp-rebuild|design/*) exit 0 ;; esac

gitdir=$(git rev-parse --git-dir)
for f in MERGE_HEAD REBASE_HEAD CHERRY_PICK_HEAD rebase-merge rebase-apply; do
  [ -e "$gitdir/$f" ] && exit 0
done

# The repo is public and real bank statements live near it, so a NEW file is
# auto-added only when it is a source or doc type (an allowlist: an export can
# take any extension). JSON counts only where config lives. Anything held back
# is named in the message for the user to add (or ignore) by hand.
held=""
if [ -n "$(git status --porcelain)" ]; then
  git add -u
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    lower=$(printf '%s' "$f" | tr '[:upper:]' '[:lower:]')
    case "$lower" in
      *.py|*.md|*.sql|*.sh|*.toml|*.yml|*.yaml|*.ini|*.cfg|*.css|pipfile|pipfile.lock|requirements*.txt|.gitignore|.gitattributes|*/.gitignore|.github/*) ;;
      ais_fmd/config/*.json|.claude/*.json|.claude/*/*.json) ;;
      *) held="$held $f"; continue ;;
    esac
    if [ "$(wc -c < "$f")" -gt 1000000 ]; then held="$held $f"; continue; fi
    git add -- "$f"
  done < <(git ls-files --others --exclude-standard)
  if ! git diff --cached --quiet; then
    git commit --quiet -m "WIP: auto-sync from $(hostname)" \
      -m "Made by the Claude Code Stop hook; squash before merging." || msg "WIP commit failed on $branch; changes left uncommitted."
  fi
fi
[ -n "$held" ] && held_note=" Not auto-committed (possible data files):$held" || held_note=""

if git rev-parse -q --verify "@{u}" >/dev/null 2>&1; then
  if [ "$(git rev-list --count @{u}..HEAD)" -eq 0 ]; then
    [ -n "$held_note" ] && msg "${held_note# }"
    exit 0
  fi
  git push --quiet 2>/dev/null || msg "Push of $branch failed (GitHub may have newer commits). Pull before continuing.$held_note"
else
  git push --quiet -u origin "$branch" 2>/dev/null || msg "First push of $branch failed.$held_note"
fi
msg "Pushed $branch to GitHub.$held_note"
