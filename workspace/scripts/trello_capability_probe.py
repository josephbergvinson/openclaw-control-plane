#!/usr/bin/env python3
"""Read-only Trello capability probe for configured workspace credentials."""

from __future__ import annotations

import argparse
import json
import sys

from trello_common import DEFAULT_ENV_PATH, TrelloError, build_client


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe Trello auth and optional board access")
    parser.add_argument(
        "--env-path",
        default=str(DEFAULT_ENV_PATH),
        help="Runtime dotenv path (absolute or relative to the Workspace)",
    )
    parser.add_argument("--board-id", help="Optional board id or short link to verify")
    return parser.parse_args()



def main() -> int:
    args = parse_args()
    out: dict[str, object] = {
        "overall": "blocked",
        "auth": {},
        "board": None,
        "errors": [],
    }
    try:
        client = build_client(env_path=args.env_path)
        me = client.get("/members/me", {"fields": "id,username,fullName,url"})
        out["auth"] = {
            "status": "ready",
            "member_id": me.get("id"),
            "username": me.get("username"),
            "full_name": me.get("fullName"),
            "url": me.get("url"),
        }
        if args.board_id:
            board = client.get(f"/boards/{args.board_id}", {"fields": "id,name,url,closed"})
            out["board"] = {
                "status": "ready",
                "id": board.get("id"),
                "name": board.get("name"),
                "url": board.get("url"),
                "closed": board.get("closed"),
            }
        out["overall"] = "ready"
        print(json.dumps(out, indent=2))
        return 0
    except TrelloError as exc:
        out["errors"] = [str(exc)]
        print(json.dumps(out, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
