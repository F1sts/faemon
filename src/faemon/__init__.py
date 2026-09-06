from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import threading

import uvicorn

from .app import create_app
from .state import Config, State


def main() -> None:
    parser = argparse.ArgumentParser(description="Faemon local media server")
    parser.add_argument(
        "--language", default="en", help="YouTube Music language (default: en)"
    )
    parser.add_argument(
        "--location", default="", help="YouTube Music location (default: empty)"
    )
    parser.add_argument(
        "--max-proxy-sessions",
        type=int,
        default=20,
        help="Max concurrent proxy stream sessions (default: 20)",
    )
    parser.add_argument(
        "--parent-death-fd",
        type=int,
        default=None,
        help="File descriptor whose EOF signals the parent process has died",
    )
    args = parser.parse_args()

    if args.parent_death_fd is not None:
        fd = args.parent_death_fd

        def _watch_parent_death() -> None:
            try:
                while os.read(fd, 4096):
                    pass
            except OSError:
                pass
            os._exit(0)

        threading.Thread(target=_watch_parent_death, daemon=True).start()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    token = secrets.token_urlsafe(32)

    state = State(
        config=Config(language=args.language, location=args.location),
        max_proxy_sessions=args.max_proxy_sessions,
        base_url=f"http://127.0.0.1:{port}",
        token=token,
    )
    app = create_app(token, state)

    handshake = json.dumps({"port": port, "token": token}, ensure_ascii=False)
    print(handshake, flush=True)

    uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="error", access_log=False
        )
    ).run()
