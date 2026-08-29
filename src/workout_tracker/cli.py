from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .journal import CommandError, StorageUnavailable, TrainingJournal, maybe_backup
from .render import render_progress


DEFAULT_DB = Path("~/.local/share/workout-tracker/workout.sqlite3").expanduser()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="workout", description="筋トレのセット実績を記録します。")
    commands = root.add_subparsers(dest="subcommand", required=True)

    record = commands.add_parser("record", help="command JSONを標準入力から1件記録します。")
    record.add_argument("--request-id", required=True)

    context = commands.add_parser("context", help="トレーニング文脈を返します。")
    context.add_argument("--json", action="store_true", required=True)

    progress = commands.add_parser("progress", help="種目一覧又は重量推移を返します。")
    progress.add_argument("--exercise")
    output = progress.add_mutually_exclusive_group(required=True)
    output.add_argument("--json", action="store_true")
    output.add_argument("--html", metavar="PATH")
    return root


def db_path() -> Path:
    return Path(os.environ.get("WORKOUT_DB_PATH", DEFAULT_DB)).expanduser()


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    path = db_path()
    try:
        existed = path.exists()
        warning = maybe_backup(path, path.with_name("workout.backup.sqlite3")) if existed else None
        journal = TrainingJournal(path)
        if not existed:
            warning = maybe_backup(path, path.with_name("workout.backup.sqlite3"))
        if warning:
            print(warning, file=sys.stderr)

        if args.subcommand == "record":
            try:
                command = json.load(sys.stdin)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                print(f"INVALID_JSON: {error}", file=sys.stderr)
                return 2
            receipt = journal.record(command, args.request_id, warnings=[warning] if warning else None)
            print_json(receipt)
            return 0

        if args.subcommand == "context":
            print_json(journal.get_training_context())
            return 0

        if args.subcommand == "progress":
            try:
                progress = journal.get_progress(args.exercise)
            except CommandError as error:
                print_json(error.receipt())
                return 0
            if args.html:
                if args.exercise is None:
                    parser().error("--htmlには--exerciseが必要です。")
                output = render_progress(progress, args.html)
                print_json({"status": "generated", "path": str(output)})
            else:
                print_json(progress)
            return 0
    except StorageUnavailable as error:
        print(f"STORAGE_UNAVAILABLE: {error}", file=sys.stderr)
        print_json({"error": {"code": "STORAGE_UNAVAILABLE"}})
        return 1
    return 2
