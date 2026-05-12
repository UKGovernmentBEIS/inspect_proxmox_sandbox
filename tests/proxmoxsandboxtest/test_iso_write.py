"""Unit tests for iso_write module.

Covers the pure-logic bits (ISO build/round-trip, per-VM locking) without
needing a real Proxmox instance. The end-to-end ISO path is exercised
incidentally by test_write_file_large with a >1 MiB payload.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pycdlib
import pytest

from proxmoxsandbox._impl.iso_write import (
    _ISO_PAYLOAD_NAME,
    _WRITE_SLOT,
    _build_iso,
    _vm_lock,
    _vm_locks,
)
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment


class TestBuildIso:
    def test_roundtrip_small_payload(self, tmp_path):
        payload = b"hello world\n"
        iso_path = _build_iso(payload)
        try:
            iso = pycdlib.PyCdlib()
            iso.open(str(iso_path))
            try:
                extracted = tmp_path / "out"
                iso.get_file_from_iso(str(extracted), joliet_path="/PAYLOAD")
                assert extracted.read_bytes() == payload
            finally:
                iso.close()
        finally:
            iso_path.unlink(missing_ok=True)

    def test_roundtrip_binary_payload(self, tmp_path):
        payload = bytes(range(256)) * 1024
        iso_path = _build_iso(payload)
        try:
            iso = pycdlib.PyCdlib()
            iso.open(str(iso_path))
            try:
                extracted = tmp_path / "out.bin"
                iso.get_file_from_iso(str(extracted), joliet_path="/PAYLOAD")
                assert extracted.read_bytes() == payload
            finally:
                iso.close()
        finally:
            iso_path.unlink(missing_ok=True)

    def test_rock_ridge_name_is_payload(self, tmp_path):
        payload = b"x" * 100
        iso_path = _build_iso(payload)
        try:
            iso = pycdlib.PyCdlib()
            iso.open(str(iso_path))
            try:
                extracted = tmp_path / "out"
                iso.get_file_from_iso(str(extracted), rr_path=f"/{_ISO_PAYLOAD_NAME}")
                assert extracted.read_bytes() == payload
            finally:
                iso.close()
        finally:
            iso_path.unlink(missing_ok=True)


class TestVmLock:
    def setup_method(self):
        _vm_locks.clear()

    def test_same_vm_returns_same_lock(self):
        lock_a = _vm_lock(42)
        lock_b = _vm_lock(42)
        assert lock_a is lock_b

    def test_different_vms_return_different_locks(self):
        assert _vm_lock(1) is not _vm_lock(2)


def test_write_slot_is_sata5():
    # qemu_commands.other_config_json cold-adds this exact slot on every
    # is_sandbox VM. Keep the constant in sync with that.
    assert _WRITE_SLOT == "sata5"


def _make_env() -> ProxmoxSandboxEnvironment:
    """Build a minimal env with mocked collaborators.

    Just enough to drive write_file's branching logic in unit tests.
    """
    infra = MagicMock()
    return ProxmoxSandboxEnvironment(
        infra_commands=infra,
        agent_commands=MagicMock(),
        ipam_mappings=(),
        vm_id=100,
        all_vm_ids=(100,),
        sdn_zone_id=None,
        instance=None,
        pool_id=None,
        os_type="l26",
    )


class TestFastPathMemoisation:
    """Memoise iso_write fast-path failures per VM.

    Once the fast path fails on a VM, subsequent calls should skip
    straight to chunked QGA without re-trying the ISO path.
    """

    @pytest.mark.asyncio
    async def test_fast_path_disabled_after_first_failure(self):
        env = _make_env()
        # ISO_WRITE_THRESHOLD_BYTES = 1 MiB; use 2 MiB so we hit the branch.
        payload = b"x" * (2 * 1024 * 1024)

        # Stub out the chunked-QGA fallback's exec/_write_file_only so the
        # test focuses purely on the ISO branch's gating behaviour.
        env.exec = AsyncMock(return_value=MagicMock(returncode=0))
        env._write_file_only = AsyncMock()

        assert env._iso_fast_path_disabled is False

        # First call: IsoWriter raises → flag flips, fallback runs.
        with patch(
            "proxmoxsandbox._proxmox_sandbox_environment.IsoWriter"
        ) as mock_iso_writer_cls:
            mock_iso_writer_cls.return_value.write_file = AsyncMock(
                side_effect=RuntimeError("fast path broken")
            )
            await env.write_file("/tmp/x", payload)
            assert mock_iso_writer_cls.called, (
                "IsoWriter should be tried on the first call"
            )

        assert env._iso_fast_path_disabled is True

        # Second call: IsoWriter must NOT be touched.
        with patch(
            "proxmoxsandbox._proxmox_sandbox_environment.IsoWriter"
        ) as mock_iso_writer_cls:
            await env.write_file("/tmp/y", payload)
            assert not mock_iso_writer_cls.called, (
                "IsoWriter should be skipped once the flag is set"
            )
