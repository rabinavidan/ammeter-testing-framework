"""
Local pre-push guard: refuse to push straight to main (the server-side rule is branch protection; this catches it
before the network). pre-commit passes the target in PRE_COMMIT_REMOTE_BRANCH. Override in an emergency with
ALLOW_PUSH_TO_MAIN=1.
"""
import os
import sys

PROTECTED = {"main", "refs/heads/main"}

target = os.environ.get("PRE_COMMIT_REMOTE_BRANCH", "")
if target in PROTECTED and os.environ.get("ALLOW_PUSH_TO_MAIN") != "1":
    print("Direct pushes to main are blocked: push a branch and open a pull request; it merges when CI is green.")
    sys.exit(1)
