"""loom — parallel git-worktree dev orchestrator.

Run multiple isolated copies of a project's dev/test stack at once, one per
worktree/branch, so several Claude Code sessions can each work a different PR
without checkout conflicts. See the design doc on the Desktop for the why.
"""

__version__ = "0.1.0"
