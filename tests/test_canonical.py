import hashlib
import sqlite3

import pytest

import blackbox as bb
from blackbox.models import canonical

# Frozen BlackBox canonical JSON v1 vectors (docs/canonical-json.md). The expected
# text was produced by the pre-0.5.0 encoder; every stored digest depends on it.
VECTORS = [
    (
        {"b": 1, "a": [True, False, None], "c": {"z": "", "y": -7}},
        '{"a":[true,false,null],"b":1,"c":{"y":-7,"z":""}}',
        "5e54a62526e8157052a583f09d4a7ae62874ce98e8b7b4e7b2796685c80724a6",
    ),
    ("é", '"\\u00e9"', None),
    ("\U0001f600", '"\\ud83d\\ude00"', None),
    ('\x7f\x1f\n\t"\\/', '"\\u007f\\u001f\\n\\t\\"\\\\/"', None),
    ("\udcff", '"\\udcff"', None),
    # Code point key order, which differs from RFC 8785's UTF-16 order here.
    (
        {"é": 1, "z": 2, "Z": 3, "\U0001f600": 4, "￿": 5},
        '{"Z":3,"z":2,"\\u00e9":1,"\\uffff":5,"\\ud83d\\ude00":4}',
        "0aec5680fd1cc0c3025387b7bd344ce0892a839d4c46386dc590a4a10eeb3277",
    ),
    (2**63, "9223372036854775808", None),
    (("tuple", 1), '["tuple",1]', None),
]


@pytest.mark.parametrize("value,text,sha256", VECTORS)
def test_canonical_vectors_are_frozen(value, text, sha256):
    assert canonical(value) == text
    assert canonical(value).isascii()
    if sha256 is not None:
        assert hashlib.sha256(text.encode()).hexdigest() == sha256


@pytest.mark.parametrize(
    "value",
    [
        1.5,
        1.0,
        float("nan"),
        float("inf"),
        {"nested": [0.1]},
        {1: "integer key"},
        {True: "boolean key"},
        {None: "null key"},
        {("tuple",): "tuple key"},
        {"set": {1}},
        b"bytes",
    ],
)
def test_values_outside_canonical_json_are_rejected(value):
    with pytest.raises(TypeError):
        canonical(value)


def test_stored_float_is_an_integrity_finding_not_a_crash(tmp_path):
    database = tmp_path / "float.sqlite3"
    bb.capture(
        database,
        {
            "request_id": "float",
            "producer": "test",
            "observations": [
                {"source": "caller", "kind": "test", "name": "n", "duration_ms": 5}
            ],
        },
    )
    with sqlite3.connect(database) as db:
        (ddl,) = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='observations_no_update'"
        ).fetchone()
        db.execute("DROP TRIGGER observations_no_update")
        db.execute("UPDATE observations SET data=json_set(data,'$.duration_ms',5.0)")
        db.execute(ddl)
    result = bb.check_integrity(database)
    assert not result.ok
    assert {"receipt_integrity", "record_integrity"} <= set(result.errors)
