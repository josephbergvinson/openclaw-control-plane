#!/usr/bin/env python3
'Apply a simple Trello board sync spec.\n\nSpec schema:\n{\n  "boardId": "operator-owned-value",\n  "labels": [{"name": "P0", "color": "red"}],\n  "cards": [\n    {\n      "code": "RH-01",\n      "name": "RH-01 — Example",\n      "desc": "...",\n      "list": "Ready",\n      "labels": ["P0"],\n      "position": "bottom"\n    }\n  ]\n}\n\nDefault mode is dry-run. Pass --execute to apply the plan.\n'

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from trello_common import (
    DEFAULT_ENV_PATH,
    TrelloError,
    build_client,
    cards_by_code,
    labels_by_name,
)



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run or apply a Trello board sync spec")
    parser.add_argument("spec_path", help="Path to sync spec JSON")
    parser.add_argument(
        "--env-path",
        default=str(DEFAULT_ENV_PATH),
        help="Runtime dotenv path (absolute or relative to the Workspace)",
    )
    parser.add_argument("--execute", action="store_true", help="Apply the computed plan")
    parser.add_argument("--update-existing", action="store_true", help="Update matched existing cards instead of skipping them")
    parser.add_argument("--sleep-ms", type=int, default=250, help="Delay between write operations (default: 250)")
    return parser.parse_args()



def load_spec(path_value: str) -> dict[str, Any]:
    path = Path(path_value)
    if not path.is_absolute():
        path = Path.cwd() / path
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TrelloError("sync spec must be a JSON object")
    return payload



def maybe_sleep(delay_ms: int) -> None:
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)



def main() -> int:
    args = parse_args()
    try:
        spec = load_spec(args.spec_path)
        board_id = str(spec["boardId"])
        label_specs = spec.get("labels", []) or []
        card_specs = spec.get("cards", []) or []
        if not isinstance(label_specs, list) or not isinstance(card_specs, list):
            raise TrelloError("spec labels/cards must be arrays")

        client = build_client(env_path=args.env_path)
        board = client.get(f"/boards/{board_id}", {"fields": "id,name,url"})
        lists = client.get(f"/boards/{board_id}/lists", {"fields": "id,name,pos,closed"})
        labels = client.get(f"/boards/{board_id}/labels", {"fields": "id,name,color"})
        cards = client.get(f"/boards/{board_id}/cards", {"fields": "id,name,desc,idList,idLabels,url,closed"})

        list_by_name = {str(item.get("name")): item for item in lists}
        label_by_name = labels_by_name(labels)
        existing_cards = cards_by_code(cards)

        created_labels: list[dict[str, Any]] = []
        planned_labels: list[dict[str, Any]] = []
        for label_spec in label_specs:
            if not isinstance(label_spec, dict):
                raise TrelloError("each label spec must be an object")
            name = str(label_spec["name"])
            color = str(label_spec.get("color", "null"))
            if name in label_by_name:
                planned_labels.append({"action": "keep", "name": name, "id": label_by_name[name].get("id")})
                continue
            planned_labels.append({"action": "create", "name": name, "color": color})
            if args.execute:
                created = client.post("/labels", {"idBoard": board_id, "name": name, "color": color})
                label_by_name[name] = created
                created_labels.append({"name": name, "id": created.get("id"), "color": created.get("color")})
                maybe_sleep(args.sleep_ms)

        plan: list[dict[str, Any]] = []
        applied: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for card_spec in card_specs:
            if not isinstance(card_spec, dict):
                raise TrelloError("each card spec must be an object")
            code = str(card_spec["code"])
            target_name = str(card_spec["name"])
            target_desc = str(card_spec.get("desc", ""))
            list_name = str(card_spec["list"])
            position = str(card_spec.get("position", "bottom"))
            label_names = [str(item) for item in (card_spec.get("labels", []) or [])]
            if list_name not in list_by_name:
                raise TrelloError(f"list not found on board {board_id}: {list_name}")
            missing_labels = [name for name in label_names if name not in label_by_name]
            if missing_labels:
                raise TrelloError(f"card {code} references missing labels: {missing_labels}")
            label_ids = [str(label_by_name[name]["id"]) for name in label_names]

            existing = existing_cards.get(code)
            if existing and not args.update_existing:
                skipped.append({"code": code, "reason": "exists", "cardId": existing.get("id"), "url": existing.get("url")})
                continue

            if not existing:
                record = {
                    "action": "create",
                    "code": code,
                    "list": list_name,
                    "name": target_name,
                    "labels": label_names,
                }
                plan.append(record)
                if args.execute:
                    created = client.post(
                        "/cards",
                        {
                            "idList": list_by_name[list_name]["id"],
                            "name": target_name,
                            "desc": target_desc,
                            "idLabels": ",".join(label_ids),
                            "pos": position,
                        },
                    )
                    applied.append({"action": "create", "code": code, "cardId": created.get("id"), "url": created.get("url")})
                    maybe_sleep(args.sleep_ms)
                continue

            desired_list_id = str(list_by_name[list_name]["id"])
            current_label_ids = sorted(str(item) for item in (existing.get("idLabels") or []))
            desired_label_ids = sorted(label_ids)
            changes: dict[str, Any] = {}
            if str(existing.get("name")) != target_name:
                changes["name"] = target_name
            if str(existing.get("desc", "")) != target_desc:
                changes["desc"] = target_desc
            if str(existing.get("idList")) != desired_list_id:
                changes["idList"] = desired_list_id
            if current_label_ids != desired_label_ids:
                changes["idLabels"] = ",".join(label_ids)
            if not changes:
                skipped.append({"code": code, "reason": "already_matches", "cardId": existing.get("id"), "url": existing.get("url")})
                continue

            plan.append({"action": "update", "code": code, "cardId": existing.get("id"), "changes": changes})
            if args.execute:
                updated = client.put(f"/cards/{existing['id']}", changes)
                applied.append({"action": "update", "code": code, "cardId": updated.get("id"), "url": updated.get("url"), "changes": changes})
                maybe_sleep(args.sleep_ms)

        result = {
            "board": board,
            "execute": args.execute,
            "updateExisting": args.update_existing,
            "plannedLabels": planned_labels,
            "createdLabels": created_labels,
            "plan": plan,
            "applied": applied,
            "skipped": skipped,
        }
        print(json.dumps(result, indent=2))
        return 0
    except TrelloError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
