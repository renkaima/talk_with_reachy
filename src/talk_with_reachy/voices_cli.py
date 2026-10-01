"""``talk-with-reachy-voices``: manage the voices Talk with Reachy recognizes.

The running app owns the voice library, so this command never edits it
directly. It drops a small request file into ``people/requests/``; the app
applies it within a couple of seconds (or at its next start) and appends the
outcome to ``people/requests_log.jsonl``. ``list`` only reads.

Standard library only, so it runs with any Python 3.
"""

from __future__ import annotations
import sys
import json
import time
import argparse
from typing import Any
from pathlib import Path
from datetime import datetime

from talk_with_reachy import study_log
from talk_with_reachy.voice_files import (
    LIBRARY_FILENAME,
    REQUEST_LOG_FILENAME,
    valid_id,
    people_dir,
    requests_dir,
)


WAIT_FOR_APP_S = 6.0


def _queue(data_dir: Path, request: dict[str, Any]) -> int:
    folder = requests_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = folder / f"{stamp}_{request['action']}.json"
    tmp = folder / f".{path.name}.tmp"
    tmp.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)

    deadline = time.monotonic() + WAIT_FOR_APP_S
    while time.monotonic() < deadline:
        if not path.exists():
            print(_last_result(data_dir) or "Done.")
            return 0
        time.sleep(0.3)
    print("Queued. Talk with Reachy is not running right now; it will apply this when it next starts.")
    return 0


def _last_result(data_dir: Path) -> str | None:
    log_path = people_dir(data_dir) / REQUEST_LOG_FILENAME
    try:
        last = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        return str(json.loads(last).get("result"))
    except (OSError, IndexError, json.JSONDecodeError):
        return None


def _list(data_dir: Path) -> int:
    try:
        library = json.loads((people_dir(data_dir) / LIBRARY_FILENAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        print("No voices yet.")
        return 0
    voices = sorted(library.get("voices", []), key=lambda v: (v.get("kind") != "enrolled", v.get("id", "")))
    print(f"{'ID':<10} {'KIND':<9} {'NAME':<20} {'UTTERANCES':>10}  LAST HEARD            ALSO KNOWN AS")
    for voice in voices:
        print(
            f"{voice.get('id', ''):<10} {voice.get('kind', ''):<9} {voice.get('name', '')[:20]:<20} "
            f"{voice.get('count', 0):>10}  {voice.get('last_seen', '')[:19]:<20}  {', '.join(voice.get('aliases', []))}"
        )
    pending = sorted(requests_dir(data_dir).glob("*.json")) if requests_dir(data_dir).exists() else []
    if pending:
        print(f"\n{len(pending)} command(s) waiting for the app to apply them.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and queue the request."""
    parser = argparse.ArgumentParser(
        prog="talk-with-reachy-voices",
        description="Manage the voices Talk with Reachy recognizes. Changes apply in the running app.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Show known voices.")
    enroll = sub.add_parser(
        "enroll",
        help="Learn a participant's voice from the next stretch of speech Reachy hears.",
        description=(
            "Start this, then have only that person talk with Reachy until they have spoken for about "
            "SECONDS seconds in total (normally 1-2 minutes of conversation)."
        ),
    )
    enroll.add_argument("id", help="Participant ID, for example P01")
    enroll.add_argument("--name", default="", help="Name Reachy may use for them")
    enroll.add_argument("--seconds", type=float, default=20.0, help="Speech to collect (default 20)")
    sub.add_parser("cancel", help="Stop an enrollment that is in progress.")
    link = sub.add_parser("link", help="Give an automatic voice (V###) a participant ID, or merge two voices.")
    link.add_argument("source", help="Voice to rename or merge, for example V007")
    link.add_argument("target", help="ID it should become, for example P01")
    link.add_argument("--name", default="", help="Name Reachy may use for them")
    rename = sub.add_parser("rename", help="Change the name Reachy uses for a voice.")
    rename.add_argument("id")
    rename.add_argument("name")
    delete = sub.add_parser("delete", help="Forget a voice and its memories (for example after a withdrawal).")
    delete.add_argument("id")
    delete.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    args = parser.parse_args(argv)

    data_dir = study_log.configured_data_dir()
    try:
        if args.command == "list":
            return _list(data_dir)
        if args.command == "enroll":
            request = {
                "action": "enroll",
                "id": valid_id(args.id, allow_auto=False),
                "name": args.name,
                "seconds": args.seconds,
            }
            print(
                f"Enrolling {request['id']}: have only them talk with Reachy until about {args.seconds:.0f} s of speech."
            )
            return _queue(data_dir, request)
        if args.command == "cancel":
            return _queue(data_dir, {"action": "cancel"})
        if args.command == "link":
            target = valid_id(args.target, allow_auto=True)
            return _queue(data_dir, {"action": "link", "source": args.source, "target": target, "name": args.name})
        if args.command == "rename":
            return _queue(data_dir, {"action": "rename", "id": args.id, "name": args.name})
        if args.command == "delete":
            if (
                not args.yes
                and input(f"Delete {args.id}'s voiceprint and memories on this robot? [y/N] ").lower() != "y"
            ):
                print("Nothing deleted.")
                return 1
            return _queue(data_dir, {"action": "delete", "id": args.id})
    except ValueError as e:
        print(f"Error: {e}")
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
