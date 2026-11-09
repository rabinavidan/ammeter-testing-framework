#!/usr/bin/env sh
# Protect `main` so new code reaches it only through a pull request whose CI is green.
# Applies .github/branch-protection.json with the GitHub CLI (run `gh auth login` first; needs admin on the repo):
#   - pull requests required: no direct pushes, no force pushes, no deletion, also for admins
#   - the `ci-gate` check (green only if lint, unit, sanity, regression, docker and diff coverage all pass) must pass
#   - the branch must be up to date with main before merging (strict), linear history, conversations resolved
# Usage: tools/setup_branch_protection.sh [owner/repo] [branch]      (defaults: the current repo, main)
set -eu
REPO="${1:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
BRANCH="${2:-main}"
gh api --method PUT "repos/${REPO}/branches/${BRANCH}/protection" \
  -H "Accept: application/vnd.github+json" \
  --input "$(dirname "$0")/../.github/branch-protection.json"
echo "Protected ${REPO}@${BRANCH}: merges need a pull request with a green ci-gate."
