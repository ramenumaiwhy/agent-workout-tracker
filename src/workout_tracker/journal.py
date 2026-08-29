from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable


SCHEMA = """
CREATE TABLE IF NOT EXISTS exercises (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  name_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exercise_aliases (
  id INTEGER PRIMARY KEY,
  exercise_id INTEGER NOT NULL REFERENCES exercises(id) ON DELETE CASCADE,
  alias TEXT NOT NULL,
  alias_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trainings (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS record_requests (
  request_id TEXT PRIMARY KEY,
  command_fingerprint TEXT NOT NULL,
  command_json TEXT NOT NULL,
  receipt_json TEXT,
  received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS set_performances (
  id INTEGER PRIMARY KEY,
  training_id INTEGER NOT NULL REFERENCES trainings(id),
  exercise_id INTEGER NOT NULL REFERENCES exercises(id),
  performed_at TEXT NOT NULL,
  weight_kg REAL NOT NULL CHECK(weight_kg >= 0 AND weight_kg <= 500),
  reps INTEGER NOT NULL CHECK(typeof(reps) = 'integer' AND reps >= 1 AND reps <= 100),
  status TEXT NOT NULL CHECK(status IN ('active', 'superseded', 'voided')),
  supersedes_id INTEGER UNIQUE REFERENCES set_performances(id),
  request_id TEXT NOT NULL REFERENCES record_requests(request_id),
  source_text TEXT NOT NULL,
  reported_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sets_active_time
  ON set_performances(status, performed_at);
CREATE INDEX IF NOT EXISTS idx_sets_exercise_active_time
  ON set_performances(exercise_id, status, performed_at);
CREATE INDEX IF NOT EXISTS idx_sets_training_active_time
  ON set_performances(training_id, status, performed_at);
"""

FOUR_HOURS = dt.timedelta(hours=4)


class StorageUnavailable(RuntimeError):
    pass


class CommandError(ValueError):
    def __init__(self, code: str, *, field: str | None = None, current_set_id: int | None = None):
        super().__init__(code)
        self.code = code
        self.field = field
        self.current_set_id = current_set_id

    def receipt(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code}
        if self.field is not None:
            error["field"] = self.field
        if self.current_set_id is not None:
            error["current_set_id"] = self.current_set_id
        return {"status": "not_recorded", "error": error}


def local_now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso_seconds(value: dt.datetime) -> str:
    return value.isoformat(timespec="seconds")


def display_name(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise CommandError("INVALID_VALUE", field=field)
    cleaned = " ".join(value.strip().split())
    if not cleaned or len(cleaned) > 100:
        raise CommandError("INVALID_VALUE", field=field)
    return cleaned


def name_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.strip().split()).casefold()


def source_text(command: dict[str, Any]) -> str:
    value = command.get("source_text")
    if not isinstance(value, str) or not value.strip():
        raise CommandError("MISSING_FIELD", field="source_text")
    return value


def parse_weight(value: Any, field: str = "weight_kg") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CommandError("INVALID_VALUE", field=field)
    try:
        decimal = Decimal(str(value))
    except InvalidOperation as error:
        raise CommandError("INVALID_VALUE", field=field) from error
    if decimal < 0 or decimal > 500 or decimal.as_tuple().exponent < -3:
        raise CommandError("INVALID_VALUE", field=field)
    return float(decimal)


def migrate_bodyweight_support(con: sqlite3.Connection) -> None:
    row = con.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'set_performances'").fetchone()
    if row is None or not isinstance(row["sql"], str):
        return
    normalized_sql = "".join(row["sql"].lower().split())
    if "check(weight_kg>0andweight_kg<=500)" not in normalized_sql:
        return

    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("ALTER TABLE set_performances RENAME TO set_performances_legacy")
        con.execute(
            """
            CREATE TABLE set_performances (
              id INTEGER PRIMARY KEY,
              training_id INTEGER NOT NULL REFERENCES trainings(id),
              exercise_id INTEGER NOT NULL REFERENCES exercises(id),
              performed_at TEXT NOT NULL,
              weight_kg REAL NOT NULL CHECK(weight_kg >= 0 AND weight_kg <= 500),
              reps INTEGER NOT NULL CHECK(typeof(reps) = 'integer' AND reps >= 1 AND reps <= 100),
              status TEXT NOT NULL CHECK(status IN ('active', 'superseded', 'voided')),
              supersedes_id INTEGER UNIQUE REFERENCES set_performances(id),
              request_id TEXT NOT NULL REFERENCES record_requests(request_id),
              source_text TEXT NOT NULL,
              reported_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            INSERT INTO set_performances(
              id, training_id, exercise_id, performed_at, weight_kg, reps, status,
              supersedes_id, request_id, source_text, reported_at
            )
            SELECT
              id, training_id, exercise_id, performed_at, weight_kg, reps, status,
              supersedes_id, request_id, source_text, reported_at
            FROM set_performances_legacy
            """
        )
        con.execute("DROP TABLE set_performances_legacy")
        con.execute("CREATE INDEX idx_sets_active_time ON set_performances(status, performed_at)")
        con.execute("CREATE INDEX idx_sets_exercise_active_time ON set_performances(exercise_id, status, performed_at)")
        con.execute("CREATE INDEX idx_sets_training_active_time ON set_performances(training_id, status, performed_at)")
        con.commit()
    except sqlite3.Error:
        con.rollback()
        raise


def parse_reps(value: Any, field: str = "reps") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise CommandError("INVALID_VALUE", field=field)
    return value


def parse_set_id(value: Any, field: str = "target_set_id") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CommandError("INVALID_VALUE", field=field)
    return value


def parse_performed_at(value: Any, now: dt.datetime, field: str = "performed_at") -> tuple[dt.datetime, str]:
    if value is None:
        return now, iso_seconds(now)
    if not isinstance(value, str):
        raise CommandError("INVALID_VALUE", field=field)
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as error:
        raise CommandError("INVALID_VALUE", field=field) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CommandError("INVALID_VALUE", field=field)
    return parsed, iso_seconds(parsed)


def normalize_command(command: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    if not isinstance(command, dict):
        raise CommandError("INVALID_VALUE", field="command")
    command_type = command.get("type")
    if not isinstance(command_type, str):
        raise CommandError("MISSING_FIELD", field="type")
    text = source_text(command)

    if command_type == "add_set":
        if "weight_kg" not in command:
            raise CommandError("MISSING_FIELD", field="weight_kg")
        if "reps" not in command:
            raise CommandError("MISSING_FIELD", field="reps")
        normalized: dict[str, Any] = {
            "type": command_type,
            "exercise": name_key(display_name(command.get("exercise"), "exercise")),
            "weight_kg": parse_weight(command["weight_kg"]),
            "reps": parse_reps(command["reps"]),
            "source_text": text,
        }
        if "performed_at" in command:
            normalized["performed_at"] = parse_performed_at(command["performed_at"], now)[1]
        return normalized

    if command_type == "correct_set":
        changes = command.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise CommandError("MISSING_FIELD", field="changes")
        allowed = {"exercise", "weight_kg", "reps", "performed_at"}
        if set(changes) - allowed:
            raise CommandError("INVALID_VALUE", field="changes")
        normalized_changes: dict[str, Any] = {}
        if "exercise" in changes:
            normalized_changes["exercise"] = name_key(display_name(changes["exercise"], "exercise"))
        if "weight_kg" in changes:
            normalized_changes["weight_kg"] = parse_weight(changes["weight_kg"])
        if "reps" in changes:
            normalized_changes["reps"] = parse_reps(changes["reps"])
        if "performed_at" in changes:
            normalized_changes["performed_at"] = parse_performed_at(changes["performed_at"], now)[1]
        return {
            "type": command_type,
            "target_set_id": parse_set_id(command.get("target_set_id")),
            "changes": normalized_changes,
            "source_text": text,
        }

    if command_type == "void_set":
        return {
            "type": command_type,
            "target_set_id": parse_set_id(command.get("target_set_id")),
            "source_text": text,
        }

    if command_type == "register_exercise":
        name = display_name(command.get("name"), "name")
        aliases_value = command.get("aliases", [])
        if not isinstance(aliases_value, list):
            raise CommandError("INVALID_VALUE", field="aliases")
        return {
            "type": command_type,
            "name": name,
            "aliases": [display_name(alias, "aliases") for alias in aliases_value],
            "source_text": text,
        }

    raise CommandError("INVALID_VALUE", field="type")


def canonical_command_json(command: dict[str, Any]) -> str:
    try:
        return json.dumps(
            command,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise CommandError("INVALID_VALUE", field="command") from error


class TrainingJournal:
    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], dt.datetime] = local_now,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self.clock = clock
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = self._connect()
        try:
            try:
                con.executescript(SCHEMA)
                migrate_bodyweight_support(con)
            except sqlite3.Error as error:
                raise StorageUnavailable(str(error)) from error
        finally:
            con.close()

    def _connect(self) -> sqlite3.Connection:
        try:
            con = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            con.execute("PRAGMA busy_timeout = 5000")
            if con.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                con.close()
                raise StorageUnavailable("foreign keys are disabled")
            return con
        except (OSError, sqlite3.Error) as error:
            raise StorageUnavailable(str(error)) from error

    def record(
        self,
        command: dict[str, Any],
        request_id: str,
        *,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 100:
            return CommandError("INVALID_VALUE", field="request_id").receipt()
        con = self._connect()
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            con.close()
            raise StorageUnavailable("clock must return an offset-aware datetime")
        try:
            command = normalize_command(command, now)
            command_json = canonical_command_json(command)
        except CommandError as error:
            con.close()
            return error.receipt()
        fingerprint = hashlib.sha256(command_json.encode("utf-8")).hexdigest()
        reported_at = iso_seconds(now)
        try:
            con.execute("BEGIN IMMEDIATE")
            previous = con.execute(
                "SELECT command_fingerprint, receipt_json FROM record_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if previous is not None:
                con.rollback()
                if previous["command_fingerprint"] != fingerprint:
                    return CommandError("IDEMPOTENCY_CONFLICT").receipt()
                return json.loads(previous["receipt_json"])

            con.execute(
                """
                INSERT INTO record_requests(
                  request_id, command_fingerprint, command_json, receipt_json, received_at
                ) VALUES (?, ?, ?, NULL, ?)
                """,
                (request_id, fingerprint, command_json, reported_at),
            )
            try:
                receipt = self._dispatch(con, command, request_id, now, reported_at)
            except CommandError as error:
                receipt = error.receipt()
            if warnings:
                receipt["warnings"] = list(warnings)
            receipt_json = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            con.execute(
                "UPDATE record_requests SET receipt_json = ? WHERE request_id = ?",
                (receipt_json, request_id),
            )
            con.commit()
            return receipt
        except (OSError, sqlite3.Error) as error:
            try:
                con.rollback()
            except sqlite3.Error:
                pass
            raise StorageUnavailable(str(error)) from error
        finally:
            con.close()

    def _dispatch(
        self,
        con: sqlite3.Connection,
        command: dict[str, Any],
        request_id: str,
        now: dt.datetime,
        reported_at: str,
    ) -> dict[str, Any]:
        handlers = {
            "add_set": self._add_set,
            "correct_set": self._correct_set,
            "void_set": self._void_set,
            "register_exercise": self._register_exercise,
        }
        return handlers[command["type"]](con, command, request_id, now, reported_at)

    def _exercise_by_name(self, con: sqlite3.Connection, value: Any) -> sqlite3.Row:
        display = display_name(value, "exercise")
        key = name_key(display)
        row = con.execute(
            "SELECT id, name FROM exercises WHERE name_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            row = con.execute(
                """
                SELECT exercises.id, exercises.name
                FROM exercise_aliases
                JOIN exercises ON exercises.id = exercise_aliases.exercise_id
                WHERE exercise_aliases.alias_key = ?
                """,
                (key,),
            ).fetchone()
        if row is None:
            raise CommandError("UNKNOWN_EXERCISE", field="exercise")
        return row

    def _register_exercise(
        self,
        con: sqlite3.Connection,
        command: dict[str, Any],
        request_id: str,
        now: dt.datetime,
        reported_at: str,
    ) -> dict[str, Any]:
        del request_id, now
        name = command["name"]
        aliases = command["aliases"]
        keys = [name_key(name), *(name_key(alias) for alias in aliases)]
        if len(set(keys)) != len(keys):
            raise CommandError("EXERCISE_CONFLICT", field="aliases")
        placeholders = ",".join("?" for _ in keys)
        if con.execute(
            f"SELECT 1 FROM exercises WHERE name_key IN ({placeholders}) LIMIT 1",
            keys,
        ).fetchone() or con.execute(
            f"SELECT 1 FROM exercise_aliases WHERE alias_key IN ({placeholders}) LIMIT 1",
            keys,
        ).fetchone():
            raise CommandError("EXERCISE_CONFLICT", field="name")
        cursor = con.execute(
            "INSERT INTO exercises(name, name_key, created_at) VALUES (?, ?, ?)",
            (name, keys[0], reported_at),
        )
        exercise_id = cursor.lastrowid
        con.executemany(
            """
            INSERT INTO exercise_aliases(exercise_id, alias, alias_key, created_at)
            VALUES (?, ?, ?, ?)
            """,
            [(exercise_id, alias, key, reported_at) for alias, key in zip(aliases, keys[1:])],
        )
        return {
            "status": "recorded",
            "registered_exercise": {"name": name, "aliases": aliases},
        }

    def _add_set(
        self,
        con: sqlite3.Connection,
        command: dict[str, Any],
        request_id: str,
        now: dt.datetime,
        reported_at: str,
    ) -> dict[str, Any]:
        text = command["source_text"]
        exercise = self._exercise_by_name(con, command["exercise"])
        weight = command["weight_kg"]
        reps = command["reps"]
        performed, performed_at = parse_performed_at(command.get("performed_at"), now)
        training_id = self._training_for(con, performed, created_at=reported_at)
        cursor = con.execute(
            """
            INSERT INTO set_performances(
              training_id, exercise_id, performed_at, weight_kg, reps, status,
              supersedes_id, request_id, source_text, reported_at
            ) VALUES (?, ?, ?, ?, ?, 'active', NULL, ?, ?, ?)
            """,
            (training_id, exercise["id"], performed_at, weight, reps, request_id, text, reported_at),
        )
        return self._set_receipt(cursor.lastrowid, training_id, exercise["name"], weight, reps, performed_at)

    def _correct_set(
        self,
        con: sqlite3.Connection,
        command: dict[str, Any],
        request_id: str,
        now: dt.datetime,
        reported_at: str,
    ) -> dict[str, Any]:
        text = command["source_text"]
        target_id = command["target_set_id"]
        changes = command["changes"]
        target = self._active_target(con, target_id)

        if "exercise" in changes:
            exercise = self._exercise_by_name(con, changes["exercise"])
        else:
            exercise = {"id": target["exercise_id"], "name": target["exercise_name"]}
        weight = changes.get("weight_kg", target["weight_kg"])
        reps = changes.get("reps", target["reps"])
        if "performed_at" in changes:
            performed, performed_at = parse_performed_at(changes["performed_at"], now)
            training_id = self._training_for(con, performed, created_at=reported_at, exclude_set_id=target_id)
        else:
            performed_at = target["performed_at"]
            training_id = target["training_id"]

        updated = con.execute(
            "UPDATE set_performances SET status = 'superseded' WHERE id = ? AND status = 'active'",
            (target_id,),
        )
        if updated.rowcount != 1:
            raise CommandError("TARGET_NOT_ACTIVE", field="target_set_id")
        cursor = con.execute(
            """
            INSERT INTO set_performances(
              training_id, exercise_id, performed_at, weight_kg, reps, status,
              supersedes_id, request_id, source_text, reported_at
            ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                training_id,
                exercise["id"],
                performed_at,
                weight,
                reps,
                target_id,
                request_id,
                text,
                reported_at,
            ),
        )
        return self._set_receipt(cursor.lastrowid, training_id, exercise["name"], weight, reps, performed_at)

    def _void_set(
        self,
        con: sqlite3.Connection,
        command: dict[str, Any],
        request_id: str,
        now: dt.datetime,
        reported_at: str,
    ) -> dict[str, Any]:
        del request_id, now, reported_at
        target_id = command["target_set_id"]
        target = self._active_target(con, target_id)
        updated = con.execute(
            "UPDATE set_performances SET status = 'voided' WHERE id = ? AND status = 'active'",
            (target_id,),
        )
        if updated.rowcount != 1:
            raise CommandError("TARGET_NOT_ACTIVE", field="target_set_id")
        return self._set_receipt(
            target_id,
            target["training_id"],
            target["exercise_name"],
            target["weight_kg"],
            target["reps"],
            target["performed_at"],
        )

    def _active_target(self, con: sqlite3.Connection, set_id: int) -> sqlite3.Row:
        target = con.execute(
            """
            SELECT set_performances.*, exercises.name AS exercise_name
            FROM set_performances
            JOIN exercises ON exercises.id = set_performances.exercise_id
            WHERE set_performances.id = ?
            """,
            (set_id,),
        ).fetchone()
        if target is None:
            raise CommandError("TARGET_NOT_FOUND", field="target_set_id")
        if target["status"] != "active":
            raise CommandError(
                "TARGET_NOT_ACTIVE",
                field="target_set_id",
                current_set_id=self._active_successor(con, set_id),
            )
        return target

    def _active_successor(self, con: sqlite3.Connection, set_id: int) -> int | None:
        current = set_id
        while True:
            successor = con.execute(
                "SELECT id, status FROM set_performances WHERE supersedes_id = ?",
                (current,),
            ).fetchone()
            if successor is None:
                return None
            if successor["status"] == "active":
                return successor["id"]
            current = successor["id"]

    def _training_for(
        self,
        con: sqlite3.Connection,
        performed: dt.datetime,
        *,
        created_at: str,
        exclude_set_id: int | None = None,
    ) -> int:
        query = "SELECT id, training_id, performed_at FROM set_performances WHERE status = 'active'"
        params: tuple[Any, ...] = ()
        if exclude_set_id is not None:
            query += " AND id != ?"
            params = (exclude_set_id,)
        nearest: tuple[float, int] | None = None
        for row in con.execute(query, params):
            candidate = dt.datetime.fromisoformat(row["performed_at"])
            distance = abs((candidate.astimezone(dt.timezone.utc) - performed.astimezone(dt.timezone.utc)).total_seconds())
            if distance <= FOUR_HOURS.total_seconds():
                choice = (distance, row["id"])
                if nearest is None or choice < nearest:
                    nearest = choice
                    training_id = row["training_id"]
        if nearest is not None:
            return training_id
        return con.execute("INSERT INTO trainings(created_at) VALUES (?)", (created_at,)).lastrowid

    @staticmethod
    def _set_receipt(
        set_id: int,
        training_id: int,
        exercise: str,
        weight: float,
        reps: int,
        performed_at: str,
    ) -> dict[str, Any]:
        return {
            "status": "recorded",
            "set_id": set_id,
            "training_id": training_id,
            "normalized_set": {
                "exercise": exercise,
                "weight_kg": weight,
                "reps": reps,
                "performed_at": performed_at,
            },
        }

    def get_training_context(self) -> dict[str, Any]:
        con = self._connect()
        try:
            now = self.clock()
            active = con.execute(
                """
                SELECT s.id AS set_id, s.training_id, s.performed_at, s.weight_kg, s.reps,
                       e.id AS exercise_id, e.name AS exercise
                FROM set_performances s
                JOIN exercises e ON e.id = s.exercise_id
                WHERE s.status = 'active'
                """
            ).fetchall()
            active.sort(
                key=lambda row: (
                    dt.datetime.fromisoformat(row["performed_at"]).astimezone(dt.timezone.utc),
                    row["set_id"],
                )
            )
            current_training_id = self._current_training_id(active, now)
            current_sets = [self._row_set(row) for row in active if row["training_id"] == current_training_id]
            exercises = []
            for exercise in con.execute("SELECT id, name FROM exercises ORDER BY name_key"):
                rows = [row for row in active if row["exercise_id"] == exercise["id"]]
                training_ids: list[int] = []
                for row in reversed(rows):
                    if row["training_id"] not in training_ids:
                        training_ids.append(row["training_id"])
                    if len(training_ids) == 5:
                        break
                recent = []
                for training_id in reversed(training_ids):
                    sets = [self._row_set(row) for row in rows if row["training_id"] == training_id]
                    recent.append(
                        {
                            "training_id": training_id,
                            "performed_on": min(
                                sets,
                                key=lambda item: dt.datetime.fromisoformat(item["performed_at"]).astimezone(
                                    dt.timezone.utc
                                ),
                            )["performed_at"][:10],
                            "sets": sets,
                        }
                    )
                latest = max((dt.datetime.fromisoformat(row["performed_at"]) for row in rows), default=None)
                days_since_last = None
                if latest is not None:
                    days_since_last = max(0, (now.date() - latest.astimezone(now.tzinfo).date()).days)
                exercises.append(
                    {
                        "exercise": exercise["name"],
                        "days_since_last": days_since_last,
                        "recent_trainings": recent,
                    }
                )
            return {
                "generated_at": iso_seconds(now),
                "current_training": (
                    {"training_id": current_training_id, "sets": current_sets}
                    if current_training_id is not None
                    else None
                ),
                "exercises": exercises,
            }
        finally:
            con.close()

    @staticmethod
    def _current_training_id(rows: list[sqlite3.Row], now: dt.datetime) -> int | None:
        candidates: list[tuple[dt.datetime, int]] = []
        now_utc = now.astimezone(dt.timezone.utc)
        for row in rows:
            performed = dt.datetime.fromisoformat(row["performed_at"])
            if abs(now_utc - performed.astimezone(dt.timezone.utc)) <= FOUR_HOURS:
                candidates.append((performed.astimezone(dt.timezone.utc), row["training_id"]))
        return max(candidates)[1] if candidates else None

    @staticmethod
    def _row_set(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "set_id": row["set_id"],
            "performed_at": row["performed_at"],
            "exercise": row["exercise"],
            "weight_kg": row["weight_kg"],
            "reps": row["reps"],
        }

    def get_progress(self, exercise: str | None = None) -> dict[str, Any]:
        con = self._connect()
        try:
            if exercise is None:
                names = [row["name"] for row in con.execute("SELECT name FROM exercises ORDER BY name_key")]
                return {"exercises": names}
            resolved = self._exercise_by_name(con, exercise)
            rows = con.execute(
                """
                SELECT s.id, s.training_id, s.performed_at, s.weight_kg, s.reps
                FROM set_performances s
                WHERE s.exercise_id = ? AND s.status = 'active'
                """,
                (resolved["id"],),
            ).fetchall()
            rows.sort(
                key=lambda row: (
                    dt.datetime.fromisoformat(row["performed_at"]).astimezone(dt.timezone.utc),
                    row["id"],
                )
            )
            grouped: dict[int, list[sqlite3.Row]] = {}
            for row in rows:
                grouped.setdefault(row["training_id"], []).append(row)
            points = []
            for training_rows in grouped.values():
                top = max(training_rows, key=lambda row: (row["weight_kg"], row["reps"], row["performed_at"], row["id"]))
                points.append(
                    {
                        "training_id": top["training_id"],
                        "date": min(
                            training_rows,
                            key=lambda row: dt.datetime.fromisoformat(row["performed_at"]).astimezone(
                                dt.timezone.utc
                            ),
                        )["performed_at"][:10],
                        "weight_kg": top["weight_kg"],
                        "reps": top["reps"],
                    }
                )
            points.sort(key=lambda point: (point["date"], point["training_id"]))
            return {"exercise": resolved["name"], "points": points}
        finally:
            con.close()


def maybe_backup(db_path: str | Path, backup_path: str | Path, *, now: float | None = None) -> str | None:
    source_path = Path(db_path).expanduser()
    destination = Path(backup_path).expanduser()
    current_time = now if now is not None else dt.datetime.now().timestamp()
    try:
        if destination.exists() and current_time - destination.stat().st_mtime < 24 * 60 * 60:
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.unlink(missing_ok=True)
        source = sqlite3.connect(source_path, timeout=5)
        target = sqlite3.connect(temporary)
        try:
            with target:
                source.backup(target)
        finally:
            target.close()
            source.close()
        os.replace(temporary, destination)
        return None
    except (OSError, sqlite3.Error) as error:
        try:
            destination.with_name(destination.name + ".tmp").unlink(missing_ok=True)
        except OSError:
            pass
        return f"BACKUP_FAILED: {error}"
