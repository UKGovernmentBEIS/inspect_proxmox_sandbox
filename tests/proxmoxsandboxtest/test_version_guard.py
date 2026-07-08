"""Unit tests for the PVE version guard on guest file-read.

These don't need a live Proxmox: they exercise version parsing and the
decode=1 legacy fallback decoding directly.
"""

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


def test_decode_legacy_recovers_latin1_bytes() -> None:
    # decode=1 returns each raw byte as a Latin-1 codepoint; "é" (0xE9) round-trips.
    raw, truncated = _api("9.1.5")._decode_legacy_file_read(
        content="café", data={"truncated": False}, count=1024
    )
    assert raw == "café".encode("utf-8")[:3] + b"\xe9"  # 'caf' + 0xe9
    assert truncated is False


def test_decode_legacy_honours_count_cap() -> None:
    raw, truncated = _api("9.1.5")._decode_legacy_file_read(
        content="abcdef", data={"truncated": False}, count=3
    )
    assert raw == b"abc"
    assert truncated is True


def test_decode_legacy_propagates_server_truncation() -> None:
    _, truncated = _api("9.1.5")._decode_legacy_file_read(
        content="abc", data={"truncated": True}, count=1024
    )
    assert truncated is True
