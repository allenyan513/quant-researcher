"""Envelope contract — shape + JSON roundtrip."""

from __future__ import annotations

import json

from quant_researcher.contract import SCHEMA_VERSION, Envelope


def test_envelope_success_shape() -> None:
    env = Envelope.success(
        data={"foo": 1},
        data_freshness={"prices": "2026-05-19"},
    )
    payload = json.loads(env.to_json())

    assert payload["ok"] is True
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["data"] == {"foo": 1}
    assert payload["data_freshness"] == {"prices": "2026-05-19"}
    assert "as_of" in payload and payload["as_of"]
    assert "code_version" in payload and payload["code_version"]
    assert payload["error"] is None
    assert payload["snapshot_id"] is None


def test_envelope_success_defaults() -> None:
    env = Envelope.success()
    payload = json.loads(env.to_json())

    assert payload["ok"] is True
    assert payload["data"] == {}
    assert payload["data_freshness"] == {}


def test_envelope_failure_shape() -> None:
    env = Envelope.failure("bad_thing", "something broke", details={"x": 42})
    payload = json.loads(env.to_json())

    assert payload["ok"] is False
    assert payload["error"]["code"] == "bad_thing"
    assert payload["error"]["message"] == "something broke"
    assert payload["error"]["details"] == {"x": 42}
    assert payload["data"] is None


def test_envelope_keys_are_stable() -> None:
    """Top-level keys are the documented contract. Any addition is a schema_version bump."""
    env = Envelope.success()
    payload = json.loads(env.to_json())
    assert set(payload.keys()) == {
        "ok",
        "schema_version",
        "as_of",
        "data_freshness",
        "snapshot_id",
        "code_version",
        "data",
        "error",
    }
