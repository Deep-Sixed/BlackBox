import hashlib

from blackbox.migrations import v001
from blackbox.models import canonical


def test_frozen_v1_is_exact_release(released_v1):
    assert v001.DDL == released_v1[1]["ddl"]
    assert v001.DIGEST == released_v1[1]["digest"]
    assert hashlib.sha256(canonical(v001.DDL).encode()).hexdigest() == v001.DIGEST
