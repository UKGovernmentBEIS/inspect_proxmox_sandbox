#!/bin/bash
# Usage: check-commits.sh <carried-range> <stock-ref>
# Run inside a QEMU clone. For each non-merge commit in <carried-range>, report
# whether <stock-ref> already contains the same change, and how we know:
#   ancestor      the commit itself is in <stock-ref>'s history
#   upstream=SHA  its "cherry picked from" master commit is in <stock-ref>'s history
#   backport=SHA  a commit in <stock-ref>'s history cites the same master commit
#   patch-id=SHA  a commit in <stock-ref>'s history has an identical diff
# MISSING means none of these matched; check the code by hand before trusting it.
set -euo pipefail
range=$1 stock=$2
for c in $(git rev-list --no-merges --reverse "$range"); do
  subj=$(git log -1 --format=%s "$c")
  if git merge-base --is-ancestor "$c" "$stock"; then
    echo "PRESENT ancestor $c $subj"; continue
  fi
  up=$(git log -1 --format=%B "$c" | sed -nE 's/.*cherry picked from commit ([0-9a-f]{40}).*/\1/p' | tail -1)
  if [ -n "$up" ] && git cat-file -e "$up^{commit}" 2>/dev/null &&
     git merge-base --is-ancestor "$up" "$stock"; then
    echo "PRESENT upstream=$up $c $subj"; continue
  fi
  base=$(git merge-base "$c" "$stock")
  bp=$(git log --max-count=1 --format=%H --fixed-strings \
         --grep="cherry picked from commit ${up:-$c}" "$base..$stock")
  if [ -n "$bp" ]; then
    echo "PRESENT backport=$bp $c $subj"; continue
  fi
  pid=$(git show "$c" | git patch-id --stable | cut -d' ' -f1)
  mapfile -t files < <(git diff-tree --no-commit-id --name-only -r "$c")
  hit=$(git log -p --no-merges "$base..$stock" -- "${files[@]}" |
        git patch-id --stable | awk -v p="$pid" '$1 == p && !f { print $2; f = 1 }')
  if [ -n "$hit" ]; then
    echo "PRESENT patch-id=$hit $c $subj"
  else
    echo "MISSING upstream=${up:-none} $c $subj"
  fi
done
