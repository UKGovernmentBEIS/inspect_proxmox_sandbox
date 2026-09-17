"""Unit tests for the `.aisiN` host contract stamp. No live Proxmox needed.

The stamp is written into pvecfg.pm's version_info hash by the provisioning
scripts, so it arrives on the API's `version` field, not `release`.
"""

import pytest

from .proxmox_sandbox_utils import host_contract


@pytest.mark.parametrize(
    "version, expected",
    [
        ("9.2.7.aisi2", 2),
        ("9.2.7.aisi1", 1),
        ("9.2.7.aisi10", 10),  # a number, so aisi10 outranks aisi2
        # A stock host, and one whose pve-manager upgrade replaced the patched
        # pvecfg.pm, are indistinguishable here and both read 0.
        ("9.2.7", 0),
        ("", 0),
        ("9.2.7.aisi", 0),
    ],
)
def test_host_contract(version: str, expected: int) -> None:
    assert host_contract(version) == expected
