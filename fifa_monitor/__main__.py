"""CLI entry point: `python -m fifa_monitor [--test] [--config config.json]`."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional; env vars may be set another way
    pass

from .config import Config
from .discord import DiscordNotifier
from .logging_setup import setup_logging
from .monitor import Monitor


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fifa_monitor",
        description="Monitor store.fifa.com for new/restocked ring products.",
    )
    parser.add_argument("--config", default="config.json", help="Path to config.json.")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Send a fake alert to the webhook to verify formatting, then exit.",
    )
    return parser.parse_args(argv)


async def run_test(cfg: Config) -> int:
    if not cfg.webhook_url:
        print("ERROR: DISCORD_WEBHOOK_URL is not set in the environment/.env.")
        return 1
    notifier = DiscordNotifier(cfg.webhook_url, cfg.base_url, cfg.mention)
    await notifier.send_test()
    await notifier.aclose()
    print("Test alert dispatched. Check your Discord channel.")
    return 0


async def run_monitor(cfg: Config) -> int:
    monitor = Monitor(cfg)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, monitor.stop)
        except NotImplementedError:  # e.g. Windows
            pass
    await monitor.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = Config.load(args.config)
    setup_logging(cfg.log_file, cfg.log_level)

    if args.test:
        return asyncio.run(run_test(cfg))
    try:
        return asyncio.run(run_monitor(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
