"""EC2 caps instance user data at 16384 bytes.

Overrunning it fails at RunInstances, which is only reachable by launching a
real instance.

launch.sh sends scripts/ec2/userdata.sh the way this measures it: `gzip -c` to a
file, then `--user-data fileb://`, which the AWS CLI base64-encodes. The 16384
applies to the raw form, so this asserts against the post-base64 size, the
stricter of the two.
"""

import base64
import gzip
from pathlib import Path

# Well under EC2's 16384, so a growing script trips this before RunInstances does.
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
    encoded = base64.b64encode(gzip.compress(SCRIPT.read_bytes()))
    assert len(encoded) <= BUDGET
