"""EC2 caps instance user data at 16384 bytes, gzipped and base64-encoded.

scripts/ec2/userdata.sh is passed that way, so it has a size budget. Overrunning
it fails at RunInstances, which is only reachable by launching a real instance.
"""

import base64
import gzip
import json
from pathlib import Path

BUDGET = 13500

SCRIPT = (
    Path(__file__).parents[2]
    / "src"
    / "proxmoxsandbox"
    / "scripts"
    / "ec2"
    / "userdata.sh"
)


def test_userdata_fits_ec2_limit() -> None:
    encoded = base64.b64encode(gzip.compress(json.dumps(SCRIPT.read_text()).encode()))
    assert len(encoded) <= BUDGET
