"""Application entry point — CLI dispatcher and bot bootstrap.

Handles two execution modes:
  1. `ccbot hook` — delegates to hook.hook_main() for Claude Code hook processing.
  2. `ccbot send` / `ccbot runtime-input` — delegates to focused CLIs.
  3. Default — configures logging, initializes tmux session, and starts the
     Telegram bot polling loop via bot.create_bot().
"""

import argparse
import logging
import sys


def _build_parser(prog: str = "ccbot") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Control tmux-hosted coding runtimes from Telegram. Run without a "
            "subcommand to start the Telegram bot service."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("hook", "send", "send_bot_message", "runtime-input", "inject"),
        help=(
            "Optional subcommand. `send` delivers text/files to Telegram; "
            "`runtime-input`/`inject` send text to a live runtime input plane."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Main entry point."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"-h", "--help"}:
        _build_parser().print_help()
        raise SystemExit(0)
    if args and args[0] == "hook":
        from .hook import hook_main

        hook_main()
        return
    if args and args[0] in {"send_bot_message", "send"}:
        from .send_bot_message import send_bot_message_main

        command_name = args[0]
        raise SystemExit(send_bot_message_main(args[1:], prog=f"ccbot {command_name}"))
    if args and args[0] in {"runtime-input", "inject"}:
        from .runtime_input_cli import runtime_input_main

        command_name = args[0]
        raise SystemExit(runtime_input_main(args[1:], prog=f"ccbot {command_name}"))
    if args:
        _build_parser().error(f"unknown command: {args[0]}")

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.WARNING,
    )

    # Import config before enabling DEBUG — avoid leaking debug logs on config errors
    try:
        from .config import config
    except ValueError as e:
        from .utils import ccbot_dir

        config_dir = ccbot_dir()
        env_path = config_dir / ".env"
        print(f"Error: {e}\n")
        print(f"Create {env_path} with the following content:\n")
        print("  TELEGRAM_BOT_TOKEN=your_bot_token_here")
        print("  ALLOWED_USERS=your_telegram_user_id")
        print()
        print("Get your bot token from @BotFather on Telegram.")
        print("Get your user ID from @userinfobot on Telegram.")
        sys.exit(1)

    logging.getLogger("ccbot").setLevel(logging.DEBUG)
    # AIORateLimiter (max_retries=5) handles retries itself; keep INFO for visibility
    logging.getLogger("telegram.ext.AIORateLimiter").setLevel(logging.INFO)
    logger = logging.getLogger(__name__)

    from .tmux_manager import tmux_manager

    logger.info("Allowed users: %s", config.allowed_users)
    logger.info("Claude projects path: %s", config.claude_projects_path)

    # Ensure tmux session exists
    session = tmux_manager.get_or_create_session()
    logger.info("Tmux session '%s' ready", session.session_name)

    logger.info("Starting Telegram bot...")
    from .bot import create_bot, telegram_poll_timeout

    application = create_bot()
    application.run_polling(
        allowed_updates=["message", "callback_query"],
        timeout=telegram_poll_timeout(),
    )


if __name__ == "__main__":
    main()
