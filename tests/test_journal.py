from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from workout_tracker.journal import SCHEMA, CommandError, TrainingJournal, maybe_backup
from workout_tracker.render import render_progress


JST = dt.timezone(dt.timedelta(hours=9))


class JournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "workout.sqlite3"
        self.now = dt.datetime(2026, 8, 23, 10, 0, tzinfo=JST)
        self.journal = TrainingJournal(self.db, clock=lambda: self.now)

    def request_id(self) -> str:
        return str(uuid.uuid4())

    def register(self, name: str = "ベンチプレス", aliases: list[str] | None = None) -> dict:
        return self.journal.record(
            {
                "type": "register_exercise",
                "name": name,
                "aliases": aliases or [],
                "source_text": f"{name}を登録して",
            },
            self.request_id(),
        )

    def add(
        self,
        *,
        request_id: str | None = None,
        exercise: str = "ベンチプレス",
        weight: float = 60,
        reps: int = 10,
        performed_at: str | None = None,
        source_text: str = "ベンチプレス60キロ10回",
    ) -> dict:
        command = {
            "type": "add_set",
            "exercise": exercise,
            "weight_kg": weight,
            "reps": reps,
            "source_text": source_text,
        }
        if performed_at is not None:
            command["performed_at"] = performed_at
        return self.journal.record(command, request_id or self.request_id())

    def rows(self, query: str, params: tuple = ()) -> list[sqlite3.Row]:
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(query, params).fetchall()
        finally:
            con.close()

    def test_unknown_exercise_is_not_registered_implicitly(self) -> None:
        receipt = self.add()

        self.assertEqual(receipt["status"], "not_recorded")
        self.assertEqual(receipt["error"]["code"], "UNKNOWN_EXERCISE")
        self.assertEqual(self.rows("SELECT * FROM exercises"), [])
        self.assertEqual(self.rows("SELECT * FROM set_performances"), [])

    def test_register_and_record_through_alias(self) -> None:
        registered = self.register(aliases=["ベンチ", "Bench Press"])
        receipt = self.add(exercise="ｂｅｎｃｈ　ｐｒｅｓｓ")

        self.assertEqual(registered["registered_exercise"]["name"], "ベンチプレス")
        self.assertEqual(receipt["normalized_set"]["exercise"], "ベンチプレス")
        self.assertEqual(receipt["normalized_set"]["weight_kg"], 60.0)

    def test_alias_conflict_is_rejected(self) -> None:
        self.register(aliases=["ベンチ"])
        result = self.register("インクラインベンチ", aliases=[" ベンチ "])

        self.assertEqual(result["error"]["code"], "EXERCISE_CONFLICT")
        self.assertEqual(len(self.rows("SELECT * FROM exercises")), 1)

    def test_same_request_returns_same_receipt_once(self) -> None:
        self.register()
        request_id = self.request_id()
        first = self.add(request_id=request_id)
        second = self.add(request_id=request_id)
        third = self.add(request_id=request_id)

        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(len(self.rows("SELECT * FROM set_performances")), 1)

    def test_backup_warning_is_part_of_idempotent_receipt(self) -> None:
        self.register()
        request_id = self.request_id()
        command = {
            "type": "add_set",
            "exercise": "ベンチプレス",
            "weight_kg": 60,
            "reps": 10,
            "source_text": "ベンチプレス60キロ10回",
        }
        first = self.journal.record(command, request_id, warnings=["BACKUP_FAILED: test"])
        replay = self.journal.record(command, request_id)

        self.assertEqual(first, replay)
        self.assertEqual(replay["warnings"], ["BACKUP_FAILED: test"])

    def test_changed_command_with_same_request_is_conflict(self) -> None:
        self.register()
        request_id = self.request_id()
        self.add(request_id=request_id)
        conflict = self.add(request_id=request_id, reps=9)

        self.assertEqual(conflict["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(self.rows("SELECT * FROM set_performances")), 1)

    def test_equivalent_normalized_command_reuses_receipt(self) -> None:
        self.register()
        request_id = self.request_id()
        first = self.add(request_id=request_id, weight=60)
        replay = self.add(request_id=request_id, weight=60.0)

        self.assertEqual(first, replay)
        self.assertEqual(len(self.rows("SELECT * FROM set_performances")), 1)

    def test_identical_sets_with_new_requests_are_both_recorded(self) -> None:
        self.register()
        self.add()
        self.add()

        self.assertEqual(len(self.rows("SELECT * FROM set_performances WHERE status = 'active'")), 2)

    def test_sets_within_four_hours_share_training(self) -> None:
        self.register()
        first = self.add(performed_at="2026-08-23T10:00:00+09:00")
        second = self.add(performed_at="2026-08-23T13:59:00+09:00", reps=8)
        third = self.add(performed_at="2026-08-23T18:01:00+09:00", reps=6)

        self.assertEqual(first["training_id"], second["training_id"])
        self.assertNotEqual(second["training_id"], third["training_id"])

    def test_correction_keeps_source_and_linear_successor(self) -> None:
        self.register()
        original = self.add()
        request_id = self.request_id()
        command = {
            "type": "correct_set",
            "target_set_id": original["set_id"],
            "changes": {"reps": 9},
            "source_text": "今のは9回だった",
        }
        corrected = self.journal.record(command, request_id)
        replay = self.journal.record(command, request_id)

        self.assertEqual(corrected, replay)
        self.assertEqual(corrected["normalized_set"]["reps"], 9)
        rows = self.rows("SELECT id, status, supersedes_id, source_text FROM set_performances ORDER BY id")
        self.assertEqual(rows[0]["status"], "superseded")
        self.assertEqual(rows[0]["source_text"], "ベンチプレス60キロ10回")
        self.assertEqual(rows[1]["supersedes_id"], rows[0]["id"])
        self.assertEqual(rows[1]["source_text"], "今のは9回だった")

    def test_old_set_id_returns_active_successor(self) -> None:
        self.register()
        original = self.add()
        corrected = self.journal.record(
            {
                "type": "correct_set",
                "target_set_id": original["set_id"],
                "changes": {"reps": 9},
                "source_text": "9回に訂正",
            },
            self.request_id(),
        )
        stale = self.journal.record(
            {
                "type": "void_set",
                "target_set_id": original["set_id"],
                "source_text": "古い方を取り消し",
            },
            self.request_id(),
        )

        self.assertEqual(stale["error"]["code"], "TARGET_NOT_ACTIVE")
        self.assertEqual(stale["error"]["current_set_id"], corrected["set_id"])

    def test_performed_at_correction_reassigns_training_and_rejects_naive_time(self) -> None:
        self.register()
        target = self.add(performed_at="2026-08-23T10:00:00+09:00")
        anchor = self.add(performed_at="2026-08-23T20:00:00+09:00", reps=8)

        joined = self.journal.record(
            {
                "type": "correct_set",
                "target_set_id": target["set_id"],
                "changes": {"performed_at": "2026-08-23T20:30:00+09:00"},
                "source_text": "夜8時半に訂正",
            },
            self.request_id(),
        )
        moved = self.journal.record(
            {
                "type": "correct_set",
                "target_set_id": joined["set_id"],
                "changes": {"performed_at": "2026-08-24T06:00:00+09:00"},
                "source_text": "翌朝6時に訂正",
            },
            self.request_id(),
        )
        naive = self.add(performed_at="2026-08-24T10:00:00")

        self.assertNotEqual(target["training_id"], anchor["training_id"])
        self.assertEqual(joined["training_id"], anchor["training_id"])
        self.assertNotEqual(moved["training_id"], anchor["training_id"])
        self.assertEqual(naive["error"]["code"], "INVALID_VALUE")
        statuses = self.rows(
            "SELECT id, status FROM set_performances WHERE id IN (?, ?) ORDER BY id",
            (target["set_id"], joined["set_id"]),
        )
        self.assertEqual([row["status"] for row in statuses], ["superseded", "superseded"])

    def test_void_removes_set_from_context_and_progress(self) -> None:
        self.register()
        recorded = self.add()
        request_id = self.request_id()
        self.journal.record(
            {"type": "void_set", "target_set_id": recorded["set_id"], "source_text": "今のを取り消し"},
            request_id,
        )

        context = self.journal.get_training_context()
        progress = self.journal.get_progress("ベンチプレス")
        request = self.rows("SELECT command_json FROM record_requests WHERE request_id = ?", (request_id,))[0]
        self.assertIsNone(context["current_training"])
        self.assertEqual(context["exercises"][0]["recent_trainings"], [])
        self.assertEqual(progress["points"], [])
        self.assertEqual(json.loads(request["command_json"])["source_text"], "今のを取り消し")

    def test_context_has_current_set_ids_and_recent_trainings(self) -> None:
        self.register()
        recorded = self.add()

        context = self.journal.get_training_context()
        self.assertEqual(context["current_training"]["sets"][0]["set_id"], recorded["set_id"])
        self.assertEqual(context["exercises"][0]["days_since_last"], 0)
        self.assertEqual(context["exercises"][0]["recent_trainings"][0]["sets"][0]["reps"], 10)

    def test_progress_uses_top_weight_and_reps_for_tie(self) -> None:
        self.register()
        self.add(weight=60, reps=8)
        self.add(weight=60, reps=10)
        self.add(weight=55, reps=12)

        progress = self.journal.get_progress("ベンチプレス")
        self.assertEqual(len(progress["points"]), 1)
        self.assertEqual(progress["points"][0]["weight_kg"], 60.0)
        self.assertEqual(progress["points"][0]["reps"], 10)

    def test_bodyweight_set_uses_zero_external_weight(self) -> None:
        self.register("プッシュアップ")

        receipt = self.add(
            exercise="プッシュアップ",
            weight=0,
            reps=20,
            source_text="プッシュアップ20回",
        )

        self.assertEqual(receipt["status"], "recorded")
        self.assertEqual(receipt["normalized_set"]["weight_kg"], 0.0)
        self.assertEqual(receipt["normalized_set"]["reps"], 20)

    def test_existing_database_is_migrated_to_allow_bodyweight(self) -> None:
        legacy_db = Path(self.temp.name) / "legacy.sqlite3"
        legacy_schema = SCHEMA.replace(
            "CHECK(weight_kg >= 0 AND weight_kg <= 500)",
            "CHECK(weight_kg > 0 AND weight_kg <= 500)",
        )
        con = sqlite3.connect(legacy_db)
        con.executescript(legacy_schema)
        con.execute(
            "INSERT INTO exercises(id, name, name_key, created_at) VALUES (1, 'ベンチプレス', 'ベンチプレス', ?)",
            (self.now.isoformat(),),
        )
        con.execute("INSERT INTO trainings(id, created_at) VALUES (1, ?)", (self.now.isoformat(),))
        con.execute(
            """
            INSERT INTO record_requests(
              request_id, command_fingerprint, command_json, receipt_json, received_at
            ) VALUES ('legacy', 'fingerprint', '{}', '{}', ?)
            """,
            (self.now.isoformat(),),
        )
        con.execute(
            """
            INSERT INTO set_performances(
              id, training_id, exercise_id, performed_at, weight_kg, reps, status,
              supersedes_id, request_id, source_text, reported_at
            ) VALUES (1, 1, 1, ?, 60, 10, 'active', NULL, 'legacy', 'ベンチプレス60キロ10回', ?)
            """,
            (self.now.isoformat(), self.now.isoformat()),
        )
        con.commit()
        con.close()

        migrated = TrainingJournal(legacy_db, clock=lambda: self.now)
        preserved = (
            sqlite3.connect(legacy_db).execute("SELECT weight_kg, reps FROM set_performances WHERE id = 1").fetchone()
        )
        migrated.record(
            {
                "type": "register_exercise",
                "name": "プッシュアップ",
                "aliases": [],
                "source_text": "プッシュアップを登録",
            },
            self.request_id(),
        )
        receipt = migrated.record(
            {
                "type": "add_set",
                "exercise": "プッシュアップ",
                "weight_kg": 0,
                "reps": 20,
                "source_text": "プッシュアップ20回",
            },
            self.request_id(),
        )

        self.assertEqual(preserved, (60.0, 10))
        self.assertEqual(receipt["status"], "recorded")

    def test_invalid_values_are_not_saved(self) -> None:
        self.register()
        cases = [
            {"weight": -0.1, "reps": 10},
            {"weight": 500.1, "reps": 10},
            {"weight": 60, "reps": 0},
            {"weight": 60, "reps": 101},
            {"weight": 60.1234, "reps": 10},
        ]
        for case in cases:
            result = self.add(weight=case["weight"], reps=case["reps"])
            self.assertEqual(result["error"]["code"], "INVALID_VALUE")
        self.assertEqual(self.rows("SELECT * FROM set_performances"), [])


class BackupAndRenderTests(unittest.TestCase):
    def test_backup_copies_database_and_skips_fresh_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.sqlite3"
            backup = Path(directory) / "backup.sqlite3"
            con = sqlite3.connect(source)
            con.execute("CREATE TABLE sample(value TEXT)")
            con.execute("INSERT INTO sample VALUES ('saved')")
            con.commit()
            con.close()

            self.assertIsNone(maybe_backup(source, backup, now=2_000_000_000))
            first_mtime = backup.stat().st_mtime
            self.assertIsNone(maybe_backup(source, backup, now=first_mtime + 60))
            copied = sqlite3.connect(backup).execute("SELECT value FROM sample").fetchone()[0]
            self.assertEqual(copied, "saved")
            self.assertEqual(backup.stat().st_mtime, first_mtime)

    def test_backup_failure_returns_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.sqlite3"
            sqlite3.connect(source).close()
            blocker = Path(directory) / "blocker"
            blocker.write_text("file", encoding="utf-8")
            warning = maybe_backup(source, blocker / "backup.sqlite3")
            self.assertIsNotNone(warning)
            self.assertTrue(warning.startswith("BACKUP_FAILED:"))

    def test_render_creates_mobile_html_without_external_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "progress.html"
            render_progress(
                {
                    "exercise": "ベンチプレス",
                    "points": [
                        {"training_id": 1, "date": "2026-08-20", "weight_kg": 60, "reps": 10},
                        {"training_id": 2, "date": "2026-08-23", "weight_kg": 62.5, "reps": 8},
                    ],
                },
                output,
            )
            page = output.read_text(encoding="utf-8")
            self.assertIn('name="viewport"', page)
            self.assertIn("<svg", page)
            self.assertIn("62.5kg・8回", page)
            self.assertNotIn("https://", page)

    def test_render_labels_zero_weight_as_bodyweight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "progress.html"
            render_progress(
                {
                    "exercise": "プッシュアップ",
                    "points": [
                        {"training_id": 1, "date": "2026-08-23", "weight_kg": 0, "reps": 20},
                    ],
                },
                output,
            )

            page = output.read_text(encoding="utf-8")
            self.assertIn("自重・20回", page)
            self.assertNotIn("0kg・20回", page)


class CliTests(unittest.TestCase):
    def test_cli_record_and_context_return_one_json_object(self) -> None:
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env["WORKOUT_DB_PATH"] = str(Path(directory) / "workout.sqlite3")
            register = subprocess.run(
                [str(project / "bin" / "workout"), "record", "--request-id", str(uuid.uuid4())],
                input=json.dumps(
                    {
                        "type": "register_exercise",
                        "name": "ベンチプレス",
                        "aliases": ["ベンチ"],
                        "source_text": "登録して",
                    }
                ),
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(register.returncode, 0, register.stderr)
            self.assertEqual(json.loads(register.stdout)["status"], "recorded")

            record = subprocess.run(
                [str(project / "bin" / "workout"), "record", "--request-id", str(uuid.uuid4())],
                input=json.dumps(
                    {
                        "type": "add_set",
                        "exercise": "ベンチ",
                        "weight_kg": 60,
                        "reps": 10,
                        "source_text": "ベンチ60キロ10回",
                    }
                ),
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(record.returncode, 0, record.stderr)
            self.assertEqual(json.loads(record.stdout)["normalized_set"]["reps"], 10)

            context = subprocess.run(
                [str(project / "bin" / "workout"), "context", "--json"],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(context.returncode, 0, context.stderr)
            self.assertEqual(len(json.loads(context.stdout)["current_training"]["sets"]), 1)


if __name__ == "__main__":
    unittest.main()
