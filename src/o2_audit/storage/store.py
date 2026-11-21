"""SQLite-backed single-process state store.

Scope of this stage (foundation): transactions, rule registrations, runs,
task steps with checkpoints (recovery without duplication), attempt history,
immutable observations and active artifact pointers. The database file lives
in the writable workspace (default ``workspace/state.sqlite3``), never inside
frozen build/artifact directories.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from o2_audit.domain.models import Observation, ObservationSource

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS rule_registrations (
    rule_id       TEXT NOT NULL,
    rule_version  TEXT NOT NULL,
    plan_json     TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    registered_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (rule_id, rule_version)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    run_kind         TEXT NOT NULL,
    artifact_version TEXT NOT NULL,
    config_json      TEXT NOT NULL,
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id       TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    audit_unit_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_unit ON tasks(audit_unit_id);

CREATE TABLE IF NOT EXISTS steps (
    run_id         TEXT NOT NULL,
    task_id        TEXT NOT NULL,
    step_name      TEXT NOT NULL,
    input_digest   TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    status         TEXT NOT NULL,
    output_json    TEXT,
    checkpoint_json TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, task_id, step_name, input_digest)
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    step_name   TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail_json TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS observations (
    obs_digest       TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    audit_unit_id    TEXT NOT NULL,
    entity_id        TEXT NOT NULL,
    predicate        TEXT NOT NULL,
    status           TEXT NOT NULL,
    raw_value        TEXT,
    normalized_value TEXT,
    scope_complete   INTEGER NOT NULL,
    source_json      TEXT,
    recorded_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS artifact_pointers (
    name         TEXT PRIMARY KEY,
    artifact_id  TEXT NOT NULL,
    artifact_version TEXT NOT NULL,
    path         TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    is_active    INTEGER NOT NULL DEFAULT 0,
    activated_at TEXT
);

-- Stage 02b: immutable artifact version registry (lifecycle).
CREATE TABLE IF NOT EXISTS artifact_versions (
    name               TEXT PRIMARY KEY,
    build_dir          TEXT NOT NULL,
    artifact_version   TEXT NOT NULL,
    root_sha256        TEXT NOT NULL,
    parent_name        TEXT,
    parent_root_sha256 TEXT,
    is_active          INTEGER NOT NULL DEFAULT 0,
    activated_at       TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS manual_overrides (
    override_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    version_name TEXT NOT NULL,
    who          TEXT NOT NULL,
    reason       TEXT NOT NULL,
    field        TEXT NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS version_events (
    event_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    event     TEXT NOT NULL,
    version_name TEXT NOT NULL,
    detail_json  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Stage 03a: content-addressed extraction cache for the observation gateway
-- (lives in the workspace SQLite, NEVER inside frozen artifacts).
CREATE TABLE IF NOT EXISTS extraction_cache (
    cache_key    TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class TaskStore:
    """Thin transactional wrapper around one SQLite file. Single process."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # executescript() manages its own commit; do not wrap it in a manual
        # transaction (BEGIN IMMEDIATE + COMMIT would conflict).
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -- transactions ----------------------------------------------------

    class _Tx:
        def __init__(self, conn: sqlite3.Connection, lock: threading.Lock) -> None:
            self.conn = conn
            self.lock = lock

        def __enter__(self) -> sqlite3.Cursor:
            self.lock.acquire()
            self.conn.execute("BEGIN IMMEDIATE")
            return self.conn.cursor()

        def __exit__(self, exc_type, exc, tb) -> None:
            try:
                if exc_type is None:
                    self.conn.execute("COMMIT")
                else:
                    self.conn.execute("ROLLBACK")
            finally:
                self.lock.release()

    def _transaction(self) -> "TaskStore._Tx":
        return TaskStore._Tx(self._conn, self._lock)

    # -- rule registrations ------------------------------------------------

    def register_rule(self, rule_id: str, rule_version: str, plan: dict[str, Any], source_sha256: str) -> None:
        """Idempotent import keyed by (rule_id, rule_version)."""
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO rule_registrations(rule_id, rule_version, plan_json, source_sha256)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(rule_id, rule_version) DO UPDATE SET"
                " plan_json=excluded.plan_json, source_sha256=excluded.source_sha256",
                (rule_id, rule_version, json.dumps(plan, ensure_ascii=False), source_sha256),
            )

    def get_rule_plan(self, rule_id: str, rule_version: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT plan_json FROM rule_registrations WHERE rule_id=? AND rule_version=?",
            (rule_id, rule_version),
        ).fetchone()
        return json.loads(row["plan_json"]) if row else None

    # -- runs / tasks ------------------------------------------------------

    def create_run(self, run_id: str, run_kind: str, artifact_version: str, config: dict[str, Any]) -> None:
        with self._transaction() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO runs(run_id, run_kind, artifact_version, config_json, status)"
                " VALUES(?,?,?,?, 'running')",
                (run_id, run_kind, artifact_version, json.dumps(config, ensure_ascii=False)),
            )

    def ensure_task(self, task_id: str, run_id: str, audit_unit_id: str, idempotency_key: str) -> bool:
        """Create the task row unless an identical idempotency key exists.

        Returns True when a new task was created; False when an existing
        completed/running task covers this key (retry keeps history, no
        duplicate final IDs).
        """
        with self._transaction() as cur:
            row = cur.execute(
                "SELECT status FROM tasks WHERE audit_unit_id=? AND idempotency_key=?",
                (audit_unit_id, idempotency_key),
            ).fetchone()
            if row is not None:
                return False
            cur.execute(
                "INSERT INTO tasks(task_id, run_id, audit_unit_id, idempotency_key, status)"
                " VALUES(?,?,?,?, 'running')",
                (task_id, run_id, audit_unit_id, idempotency_key),
            )
            return True

    def finish_task(self, task_id: str, status: str) -> None:
        with self._transaction() as cur:
            cur.execute("UPDATE tasks SET status=? WHERE task_id=?", (status, task_id))

    # -- steps / checkpoints -------------------------------------------------

    def completed_step_output(self, run_id: str, task_id: str, step_name: str, input_digest: str) -> dict[str, Any] | None:
        """Return the checkpointed output of a completed identical step.

        Recovery: a rerun of the same (step, input digest) reuses the stored
        output instead of executing and storing a duplicate.
        """
        row = self._conn.execute(
            "SELECT output_json FROM steps WHERE run_id=? AND task_id=? AND step_name=?"
            " AND input_digest=? AND status='completed'",
            (run_id, task_id, step_name, input_digest),
        ).fetchone()
        return json.loads(row["output_json"]) if row and row["output_json"] else None

    def completed_step_checkpoint(self, run_id: str, task_id: str, step_name: str, input_digest: str) -> dict[str, Any] | None:
        """Return the checkpoint payload of a completed identical step."""
        row = self._conn.execute(
            "SELECT checkpoint_json FROM steps WHERE run_id=? AND task_id=? AND step_name=?"
            " AND input_digest=? AND status='completed'",
            (run_id, task_id, step_name, input_digest),
        ).fetchone()
        return json.loads(row["checkpoint_json"]) if row and row["checkpoint_json"] else None

    def record_step(self, run_id: str, task_id: str, step_name: str, input_digest: str, seq: int,
                    status: str, output: dict[str, Any] | None, checkpoint: dict[str, Any] | None) -> None:
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO steps(run_id, task_id, step_name, input_digest, seq, status, output_json, checkpoint_json)"
                " VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(run_id, task_id, step_name, input_digest) DO UPDATE SET"
                " status=excluded.status, output_json=excluded.output_json, checkpoint_json=excluded.checkpoint_json",
                (run_id, task_id, step_name, input_digest, seq, status,
                 json.dumps(output, ensure_ascii=False) if output is not None else None,
                 json.dumps(checkpoint, ensure_ascii=False) if checkpoint is not None else None),
            )

    def record_attempt(self, run_id: str, task_id: str, step_name: str, status: str,
                       detail: dict[str, Any] | None) -> None:
        """Append-only attempt history (used by retries/recovery)."""
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO attempts(run_id, task_id, step_name, status, detail_json) VALUES(?,?,?,?,?)",
                (run_id, task_id, step_name, status,
                 json.dumps(detail, ensure_ascii=False) if detail is not None else None),
            )

    def attempts_for(self, run_id: str, task_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT step_name, status, detail_json, created_at FROM attempts"
            " WHERE run_id=? AND task_id=? ORDER BY attempt_id",
            (run_id, task_id),
        ).fetchall()
        return [
            {"step_name": r["step_name"], "status": r["status"],
             "detail": json.loads(r["detail_json"]) if r["detail_json"] else None,
             "created_at": r["created_at"]}
            for r in rows
        ]

    # -- observations (immutable) --------------------------------------------

    def add_observation(self, run_id: str, audit_unit_id: str, obs: Observation) -> bool:
        """Insert one observation keyed by its content digest.

        Returns True when inserted, False when an identical observation
        already exists (idempotent accumulation; rows are never updated).
        """
        digest = _observation_digest(run_id, audit_unit_id, obs)
        with self._transaction() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO observations("
                " obs_digest, run_id, audit_unit_id, entity_id, predicate, status,"
                " raw_value, normalized_value, scope_complete, source_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    digest, run_id, audit_unit_id, obs.entity_id, obs.predicate,
                    obs.status.value,
                    json.dumps(obs.raw_value, ensure_ascii=False) if obs.raw_value is not None else None,
                    json.dumps(obs.normalized_value, ensure_ascii=False) if obs.normalized_value is not None else None,
                    int(obs.scope_complete),
                    obs.source.model_dump_json() if obs.source else None,
                ),
            )
            return cur.rowcount > 0

    def observations_for(self, audit_unit_id: str, run_id: str | None = None) -> list[Observation]:
        """Observations recorded for one audit unit.

        ``run_id=None`` returns all runs (diagnostics); pass an explicit
        ``run_id`` so separate runs (baseline vs candidate) never collide or
        leak observations into each other. Observation rows are immutable;
        digests include the run id.
        """
        if run_id is None:
            rows = self._conn.execute(
                "SELECT * FROM observations WHERE audit_unit_id=? ORDER BY recorded_at, obs_digest",
                (audit_unit_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM observations WHERE audit_unit_id=? AND run_id=?"
                " ORDER BY recorded_at, obs_digest",
                (audit_unit_id, run_id),
            ).fetchall()
        out = []
        for r in rows:
            out.append(Observation(
                entity_id=r["entity_id"],
                predicate=r["predicate"],
                status=r["status"],
                raw_value=json.loads(r["raw_value"]) if r["raw_value"] is not None else None,
                normalized_value=json.loads(r["normalized_value"]) if r["normalized_value"] is not None else None,
                scope_complete=bool(r["scope_complete"]),
                source=ObservationSource.model_validate_json(r["source_json"]) if r["source_json"] else None,
            ))
        return out

    # -- extraction cache (Stage 03a) -----------------------------------------

    def cache_get(self, key: str) -> dict[str, Any] | None:
        """Content-addressed gateway extraction cache lookup (workspace DB)."""
        row = self._conn.execute(
            "SELECT payload_json FROM extraction_cache WHERE cache_key=?", (key,)
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def cache_put(self, key: str, payload: dict[str, Any]) -> None:
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO extraction_cache(cache_key, payload_json) VALUES(?,?)"
                " ON CONFLICT(cache_key) DO UPDATE SET payload_json=excluded.payload_json",
                (key, json.dumps(payload, ensure_ascii=False)),
            )

    # -- active artifact pointer ----------------------------------------------

    def register_artifact(self, name: str, artifact_id: str, artifact_version: str, path: str, sha256: str) -> None:
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO artifact_pointers(name, artifact_id, artifact_version, path, sha256, is_active)"
                " VALUES(?,?,?,?,?,0)"
                " ON CONFLICT(name) DO UPDATE SET artifact_id=excluded.artifact_id,"
                " artifact_version=excluded.artifact_version, path=excluded.path, sha256=excluded.sha256",
                (name, artifact_id, artifact_version, path, sha256),
            )

    def activate_artifact(self, name: str) -> None:
        """Atomically move the active pointer to the registered artifact.

        The target is verified to exist BEFORE the previous active pointer is
        cleared; activating a missing name raises ``KeyError`` inside the
        transaction so ROLLBACK preserves the previously active pointer.
        """
        with self._transaction() as cur:
            cur.execute(
                "UPDATE artifact_pointers SET is_active=1, activated_at=datetime('now')"
                " WHERE name=?",
                (name,),
            )
            if cur.rowcount != 1:
                raise KeyError(f"artifact not registered: {name!r}")
            cur.execute(
                "UPDATE artifact_pointers SET is_active=0 WHERE is_active=1 AND name<>?",
                (name,),
            )

    def active_artifact(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT name, artifact_id, artifact_version, path, sha256 FROM artifact_pointers WHERE is_active=1"
        ).fetchone()
        return dict(row) if row else None

    # -- artifact version registry (Stage 02b lifecycle) ----------------------

    def record_artifact_version(
        self,
        name: str,
        build_dir: str,
        artifact_version: str,
        root_sha256: str,
        parent_name: str | None = None,
        parent_root_sha256: str | None = None,
    ) -> None:
        """Register one immutable version record.

        Re-recording the SAME identity is idempotent; recording a DIFFERENT
        digest/path under an existing name raises ValueError (version records
        are immutable — a changed build must take a new name).
        """
        with self._transaction() as cur:
            row = cur.execute(
                "SELECT build_dir, artifact_version, root_sha256 FROM artifact_versions WHERE name=?",
                (name,),
            ).fetchone()
            if row is not None:
                if (row["build_dir"], row["artifact_version"], row["root_sha256"]) != (
                        str(build_dir), artifact_version, root_sha256):
                    raise ValueError(
                        f"artifact version {name!r} is immutable; refusing to overwrite "
                        f"(registered root={row['root_sha256']}, new root={root_sha256})")
                return
            cur.execute(
                "INSERT INTO artifact_versions(name, build_dir, artifact_version, root_sha256,"
                " parent_name, parent_root_sha256) VALUES(?,?,?,?,?,?)",
                (name, str(build_dir), artifact_version, root_sha256,
                 parent_name, parent_root_sha256),
            )

    def get_artifact_version(self, name: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT name, build_dir, artifact_version, root_sha256, parent_name,"
            " parent_root_sha256, is_active, created_at"
            " FROM artifact_versions WHERE name=?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["is_active"] = bool(out["is_active"])
        return out

    def list_artifact_versions(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT name, build_dir, artifact_version, root_sha256, parent_name,"
            " parent_root_sha256, is_active, created_at"
            " FROM artifact_versions ORDER BY created_at, name",
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["is_active"] = bool(d["is_active"])
            out.append(d)
        return out

    def set_active_version(self, name: str) -> dict[str, Any]:
        """Atomically move the active pointer to a REGISTERED version.

        The target must exist (KeyError inside the transaction otherwise, so
        ROLLBACK preserves the previous pointer). Digest verification against
        the actual directory is the CALLER's duty (lifecycle.activate).
        """
        with self._transaction() as cur:
            row = cur.execute(
                "SELECT artifact_version FROM artifact_versions WHERE name=?", (name,)
            ).fetchone()
            if row is None:
                raise KeyError(f"artifact version not registered: {name!r}")
            cur.execute("UPDATE artifact_versions SET is_active=0 WHERE is_active=1")
            cur.execute(
                "UPDATE artifact_versions SET is_active=1, activated_at=datetime('now')"
                " WHERE name=?",
                (name,),
            )
        return {"active": name, "artifact_version": row["artifact_version"]}

    def active_version_name(self) -> str | None:
        row = self._conn.execute(
            "SELECT name FROM artifact_versions WHERE is_active=1"
        ).fetchone()
        return row["name"] if row else None

    def record_manual_override(self, version_name: str, who: str, reason: str,
                               field: str, old_value: Any, new_value: Any) -> int:
        """Record an explicit manual diff/override (who/reason/field diff)."""
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO manual_overrides(version_name, who, reason, field, old_value, new_value)"
                " VALUES(?,?,?,?,?,?)",
                (version_name, who, reason, field,
                 None if old_value is None else json.dumps(old_value, ensure_ascii=False),
                 None if new_value is None else json.dumps(new_value, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def manual_overrides_for(self, version_name: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT override_id, who, reason, field, old_value, new_value, created_at"
            " FROM manual_overrides WHERE version_name=? ORDER BY override_id",
            (version_name,),
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "override_id": r["override_id"],
                "who": r["who"],
                "reason": r["reason"],
                "field": r["field"],
                "old_value": json.loads(r["old_value"]) if r["old_value"] is not None else None,
                "new_value": json.loads(r["new_value"]) if r["new_value"] is not None else None,
                "created_at": r["created_at"],
            })
        return out

    def record_version_event(self, event: str, version_name: str,
                             detail: dict[str, Any] | None) -> None:
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO version_events(event, version_name, detail_json) VALUES(?,?,?)",
                (event, version_name,
                 json.dumps(detail, ensure_ascii=False) if detail is not None else None),
            )

    def version_events_for(self, version_name: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT event, detail_json, created_at FROM version_events"
            " WHERE version_name=? ORDER BY event_id",
            (version_name,),
        ).fetchall()
        return [
            {"event": r["event"],
             "detail": json.loads(r["detail_json"]) if r["detail_json"] else None,
             "created_at": r["created_at"]}
            for r in rows
        ]


def _observation_digest(run_id: str, audit_unit_id: str, obs: Observation) -> str:
    import hashlib

    payload = json.dumps(
        {
            "run": run_id,
            "unit": audit_unit_id,
            "entity": obs.entity_id,
            "predicate": obs.predicate,
            "status": obs.status.value,
            "raw": obs.raw_value,
            "normalized": obs.normalized_value,
            "scope": obs.scope_complete,
            "source": obs.source.model_dump(mode="json") if obs.source else None,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
