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


class TestPerEnvWriteLock:
    """The ISO write lock is per-env (per-VM), not module-level per-vm_id."""

    def test_lock_is_an_asyncio_lock(self):
        import asyncio

        assert isinstance(_make_env()._iso_write_lock, asyncio.Lock)

    def test_two_envs_sharing_a_vm_id_have_distinct_locks(self):
        # Regression: VM IDs are only unique within one singleton Proxmox
        # host (each starts numbering ~100), so two envs that share a vm_id
        # represent two different machines on two different hosts. They must
        # NOT share a write lock — a module-level dict keyed on bare vm_id
        # would falsely serialise them.
        env_a = _make_env()
        env_b = _make_env()
        assert env_a.vm_id == env_b.vm_id == 100
        assert env_a._iso_write_lock is not env_b._iso_write_lock


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
