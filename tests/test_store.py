"""SQLite TaskStore: transactions, idempotency, checkpoints, run isolation,
artifact pointer rollback."""
from __future__ import annotations

import pytest

from o2_audit.domain.models import Observation
from o2_audit.storage.store import TaskStore

from conftest import obs


@pytest.fixture()
def store(tmp_path):
    s = TaskStore(tmp_path / "state" / "state.sqlite3")
    yield s
    s.close()


def _mk_obs(entity_id="L1", predicate="model", value="LED-100", status="readable",
            scope_complete=False, raw=None):
    return obs(entity_id, predicate, value, status=status, raw=raw,
               scope_complete=scope_complete, source=("m1", 1))


# -- transactions / rules ------------------------------------------------------


def test_rule_registration_idempotent(store):
    plan = {"rule_id": "B001", "rule_version": "3.0"}
    store.register_rule("B001", "3.0", plan, "sha-x")
    store.register_rule("B001", "3.0", plan, "sha-x")  # same key again
    assert store.get_rule_plan("B001", "3.0") == plan
    store.register_rule("B001", "3.1", {"rule_id": "B001", "rule_version": "3.1"}, "sha-y")
    assert store.get_rule_plan("B001", "3.0")["rule_version"] == "3.0"
    assert store.get_rule_plan("B001", "3.1")["rule_version"] == "3.1"


# -- runs / tasks ---------------------------------------------------------------


def test_task_idempotency_by_unit_and_key(store):
    store.create_run("r1", "prototype", "v1", {})
    assert store.ensure_task("t1", "r1", "u-1", "u-1:B001:3.0") is True
    # same unit + idempotency key -> no duplicate task
    assert store.ensure_task("t1", "r1", "u-1", "u-1:B001:3.0") is False
    # different unit -> new task
    assert store.ensure_task("t2", "r1", "u-2", "u-2:B001:3.0") is True


def test_task_retry_preserves_history_status(store):
    store.create_run("r1", "prototype", "v1", {})
    store.ensure_task("t1", "r1", "u-1", "k1")
    store.finish_task("t1", "completed")
    store.ensure_task("t1", "r1", "u-1", "k1")  # retry of the same key
    with store._transaction() as cur:
        n = cur.execute("SELECT COUNT(*) c FROM tasks WHERE audit_unit_id='u-1'").fetchone()["c"]
    assert n == 1


# -- steps / checkpoints ----------------------------------------------------------


def test_step_checkpoint_recovery_without_duplicate_execution(store):
    store.create_run("r1", "prototype", "v1", {})
    store.ensure_task("t1", "r1", "u-1", "k1")
    assert store.completed_step_output("r1", "t1", "inspect", "d1") is None
    store.record_step("r1", "t1", "inspect", "d1", 0, "completed",
                      {"out": 42}, {"checkpoint": True})
    assert store.completed_step_output("r1", "t1", "inspect", "d1") == {"out": 42}
    assert store.completed_step_checkpoint("r1", "t1", "inspect", "d1") == {"checkpoint": True}
    # a different input digest is a different step and must NOT match
    assert store.completed_step_output("r1", "t1", "inspect", "d2") is None


def test_attempt_history_preserved(store):
    store.create_run("r1", "prototype", "v1", {})
    store.ensure_task("t1", "r1", "u-1", "k1")
    store.record_attempt("r1", "t1", "inspect", "error", {"error_code": "boom"})
    store.record_attempt("r1", "t1", "inspect", "completed", {"observations": 3})
    history = store.attempts_for("r1", "t1")
    assert [h["status"] for h in history] == ["error", "completed"]
    assert history[0]["detail"]["error_code"] == "boom"


# -- observations: immutable, run-isolated -----------------------------------------


def test_observations_immutable_and_idempotent(store):
    store.create_run("r1", "prototype", "v1", {})
    o = _mk_obs()
    assert store.add_observation("r1", "u-1", o) is True
    # identical content -> ignored, no duplicate row, no update
    assert store.add_observation("r1", "u-1", _mk_obs()) is False
    got = store.observations_for("u-1", run_id="r1")
    assert len(got) == 1
    assert got[0].entity_id == "L1"
    assert got[0].normalized_value == "LED-100"


def test_observations_isolated_between_runs(store):
    o = _mk_obs()
    store.create_run("baseline", "prototype", "v1", {})
    store.create_run("candidate", "prototype", "v2", {})
    assert store.add_observation("baseline", "u-1", o) is True
    assert store.add_observation("candidate", "u-1", _mk_obs()) is True
    assert len(store.observations_for("u-1", run_id="baseline")) == 1
    assert len(store.observations_for("u-1", run_id="candidate")) == 1
    assert len(store.observations_for("u-1")) == 2  # diagnostics see both
    # run id participates in the digest: identical content in two runs = 2 rows
    rows = store.observations_for("u-1")
    assert rows[0].entity_id == rows[1].entity_id


def test_observations_restore_typed_content(store):
    store.create_run("r1", "prototype", "v1", {})
    store.add_observation("r1", "u-1", _mk_obs(raw="原始字符"))
    store.add_observation("r1", "u-1", _mk_obs(entity_id="L2", status="confirmed_missing",
                                               scope_complete=True, value=None))
    got = store.observations_for("u-1", run_id="r1")
    assert got[0].raw_value == "原始字符"
    assert got[1].status.value == "confirmed_missing"
    assert got[1].scope_complete is True
    assert got[1].source is not None and got[1].source.material_id == "m1"


# -- artifact pointers ---------------------------------------------------------------


def test_activate_artifact_rollback_on_missing_target(store):
    store.register_artifact("model", "a1", "v1", "/tmp/a1", "sha1")
    store.register_artifact("model", "a2", "v2", "/tmp/a2", "sha2")
    store.activate_artifact("model")  # pointer stays on the (updated) name row
    store.register_artifact("candidate", "b1", "v1", "/tmp/b1", "sha3")
    store.activate_artifact("candidate")
    assert store.active_artifact()["name"] == "candidate"

    # activating an unregistered name must fail WITHOUT losing the pointer
    with pytest.raises(KeyError):
        store.activate_artifact("does_not_exist")
    assert store.active_artifact()["name"] == "candidate"
