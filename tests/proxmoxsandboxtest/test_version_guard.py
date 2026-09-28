"""Version selection for the guest file-read protocol."""

import pytest

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI, ProxmoxVersionInfo


def _api(release: str) -> AsyncProxmoxAPI:
    api = AsyncProxmoxAPI("localhost:8006", "root@pam", "secret", verify_tls=False)
    api.discovered_proxmox_version = ProxmoxVersionInfo(
        release=release, repoid="deadbeef", version=release
    )
    return api


@pytest.mark.parametrize(
    "release, expected",
    [
        ("9.2.0", True),
        ("9.2", True),
        ("10.0.1", True),
        ("9.2.1.aisi1", True),
        # qemu-server 9.1.5 has the feature, but we gate on the pve-manager release
        ("9.1.5", False),
        ("9.1", False),
        ("9.0", False),
        ("8.4.1", False),
    ],
)
def test_release_at_least_9_2(release: str, expected: bool) -> None:
    assert _api(release).release_at_least(9, 2) is expected
