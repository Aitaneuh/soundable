"""`soundable` command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys

from soundable_client import __version__
from soundable_client.config import Config, config_dir, delete_token, load_token, log_file


def _logout(config: Config) -> int:
    from soundable_client.api import Api, ApiError

    if not config.server_url:
        print("Not logged in.")
        return 0
    token = load_token(config.server_url)
    if token:

        async def revoke() -> None:
            api = Api(config.server_url, token)
            try:
                await api.logout()
            finally:
                await api.aclose()

        try:
            asyncio.run(revoke())
            print("Token revoked on the server.")
        except ApiError as e:
            print(f"Could not revoke the token on the server ({e}); deleting it locally anyway.")
    delete_token(config.server_url)
    print("Logged out.")
    return 0


def _doctor(config: Config) -> int:
    from soundable_client.mpv import find_mpv, install_hint

    print(f"soundable {__version__}")
    print(f"config dir : {config_dir()}")
    print(f"log file   : {log_file()}")
    print(f"server     : {config.server_url or '(not configured)'}")
    has_token = bool(config.server_url and load_token(config.server_url))
    print(f"token      : {'present' if has_token else 'missing'}")
    mpv = find_mpv(config.mpv_path)
    print(f"mpv        : {mpv or 'NOT FOUND. ' + install_hint()}")
    return 0 if mpv else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="soundable", description="soundable music client")
    parser.add_argument("--version", action="version", version=f"soundable {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("login", help="log in (or switch server) and start the app")
    sub.add_parser("logout", help="revoke and delete the stored token")
    sub.add_parser("doctor", help="check configuration and mpv")
    args = parser.parse_args(argv)

    config = Config.load()
    if args.cmd == "logout":
        return _logout(config)
    if args.cmd == "doctor":
        return _doctor(config)

    from soundable_client.app import SoundableApp
    from soundable_client.config import setup_logging

    setup_logging()

    SoundableApp(config, force_login=args.cmd == "login").run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
