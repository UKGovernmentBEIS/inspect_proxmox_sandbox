"""Keep compressed userdata within EC2's limit, with room for launch wrappers."""

import gzip
import json
from pathlib import Path

import pytest

COMPRESSED_USERDATA_BUDGET = 13500

SCRIPT = (
    Path(__file__).parents[2]
    / "src"
    / "proxmoxsandbox"
    / "scripts"
    / "ec2"
    / "userdata.sh"
)


@pytest.mark.parametrize("json_wrapped", [False, True])
def test_userdata_fits_ec2_limit(json_wrapped: bool) -> None:
    userdata = SCRIPT.read_text()
    if json_wrapped:
        userdata = json.dumps(userdata)
    compressed = gzip.compress(userdata.encode(), mtime=0)
    assert len(compressed) <= COMPRESSED_USERDATA_BUDGET
