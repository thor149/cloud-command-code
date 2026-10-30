"""Prompt prefixes shared by the Telegram bot and the GitHub Actions runner."""

ASK_PREFIX = ("Investigate the repository and answer the question. "
              "This run is read-only. Do not edit files or execute commands.\n\n")
PLAN_PREFIX = ("Investigate the repository and produce a concrete plan. "
               "This run is read-only. Do not edit files or execute commands.\n\n")
EDIT_PREFIX = {
    "edits": ("You are working in a dedicated git worktree on branch {branch}, created from {base}. "
              "In this run you can read and edit files only: shell commands are not available, so do not "
              "try to run tests or git. The bot commits your changes when you finish. Do not modify files "
              "outside this worktree. Finish with a short summary of what you changed and what should be "
              "tested.\n\nTask: "),
    "full": ("You are working in a dedicated git worktree on branch {branch}, created from {base}. "
             "Complete the task below. Run the project's relevant tests or checks if they exist. "
             "Commit your work on this branch with clear messages. Do not push, do not switch or create "
             "branches, do not change git remotes or git config, and do not modify files outside this "
             "worktree. Finish with a short summary of what you changed and how you verified it.\n\nTask: "),
}
