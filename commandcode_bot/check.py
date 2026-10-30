"""Validate the configuration without starting the bot: python -m commandcode_bot.check

Prints a summary with no secrets and exits non-zero on any configuration error, so deploy scripts
can refuse to restart the service into a crash loop."""

import sys

from .config import load_config


def main() -> int:
    try:
        config = load_config()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    print(f"Owner Telegram ID: {config.allowed_user_id}")
    print(f"Command Code: {config.command_bin}")
    print(f"Edit mode: {config.edit_mode}; model: {config.default_model or 'account default'}; "
          f"effort: {config.default_effort or 'model default'}")
    for project in config.projects.values():
        mode = f"writable, base {project.base}" if project.writable else "read-only"
        print(f"Project {project.name}: {project.path} ({mode})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
