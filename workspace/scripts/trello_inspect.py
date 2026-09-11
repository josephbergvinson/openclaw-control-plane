#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys

from trello_common import DEFAULT_ENV_PATH, TrelloError, build_client



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect Trello boards, cards, lists, and labels")
    parser.add_argument(
        "--env-path",
        default=str(DEFAULT_ENV_PATH),
        help="Runtime dotenv path (absolute or relative to the Workspace)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    board = subparsers.add_parser("board", help="Inspect a board and optional related collections")
    board.add_argument("board_id", help="Board id or short link")
    board.add_argument("--lists", action="store_true", help="Include board lists")
    board.add_argument("--labels", action="store_true", help="Include board labels")
    board.add_argument("--cards", action="store_true", help="Include board cards")

    card = subparsers.add_parser("card", help="Inspect a card by id or short id")
    card.add_argument("card_id", help="Card id or short id")
    card.add_argument(
        "--actions-limit",
        type=int,
        default=0,
        help="Optionally include recent actions (default: 0)",
    )

    return parser



def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        client = build_client(env_path=args.env_path)
        if args.command == "board":
            payload: dict[str, object] = {
                "board": client.get(f"/boards/{args.board_id}", {"fields": "id,name,url,closed,shortLink"})
            }
            if args.lists:
                payload["lists"] = client.get(f"/boards/{args.board_id}/lists", {"fields": "id,name,pos,closed"})
            if args.labels:
                payload["labels"] = client.get(f"/boards/{args.board_id}/labels", {"fields": "id,name,color"})
            if args.cards:
                payload["cards"] = client.get(f"/boards/{args.board_id}/cards", {"fields": "id,name,idList,closed,url,idLabels"})
            print(json.dumps(payload, indent=2))
            return 0

        if args.command == "card":
            payload = {
                "card": client.get(
                    f"/cards/{args.card_id}",
                    {"fields": "name,desc,id,idBoard,idList,url,labels,closed,shortLink"},
                )
            }
            if args.actions_limit > 0:
                payload["actions"] = client.get(
                    f"/cards/{args.card_id}/actions",
                    {"limit": args.actions_limit, "filter": "commentCard,updateCard,createCard"},
                )
            print(json.dumps(payload, indent=2))
            return 0

        parser.error("unknown command")
        return 2
    except TrelloError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
