"""Unit tests for iso_write module.

Covers the pure-logic bits (ISO build/round-trip, per-VM locking) without
needing a real Proxmox instance. The end-to-end ISO path is exercised
incidentally by test_write_file_large with a >1 MiB payload.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pycdlib
import pytest

from proxmoxsandbox._impl.iso_write import (
    _ISO_PAYLOAD_NAME,
    _WRITE_SLOT,
    IsoWriter,
    _build_iso,
)
from proxmoxsandbox._impl.qga_responses import ExecStatus
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import OsType, VmConfig, VmSourceConfig


class TestBuildIso:
    def test_roundtrip_small_payload(self, tmp_path) -> None:
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

    def test_roundtrip_binary_payload(self, tmp_path) -> None:
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

    def test_rock_ridge_name_is_payload(self, tmp_path) -> None:
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


def test_write_slot_is_sata5() -> None:
    # qemu_commands.other_config_json cold-adds this exact slot on every
    # is_sandbox VM. Keep the constant in sync with that.
    assert _WRITE_SLOT == "sata5"


@pytest.fixture
async def guest_copy_script(tmp_path: Path) -> tuple[str, Path]:
    agent = MagicMock()
    agent.exec_command = AsyncMock(return_value=123)
    agent.get_agent_exec_status = AsyncMock(
        return_value=ExecStatus(exited=1, exitcode=0)
    )
    writer = IsoWriter(MagicMock(), agent, MagicMock(), "test-node")
    await writer._copy_in_guest(100, str(tmp_path / "target with 'quotes'"))
    command = agent.exec_command.await_args.kwargs["command"]
    assert command[:2] == ["sh", "-c"]
    script = command[-1]
    mount_line = next(
        line
        for line in script.splitlines()
        if line.startswith("mkdir ") and "/tmp/_wf_iso_" in line
    )
    original_parent = shlex.split(mount_line)[-1]
    parent = tmp_path / "iso"
    return script.replace(original_parent, str(parent)), parent


def test_guest_mount_uses_a_private_parent_outside_the_iso(guest_copy_script):
    script, parent = guest_copy_script
    prepare = script.split("dev=\n", 1)[0]
    subprocess.run(["sh", "-c", prepare], check=True, timeout=5, umask=0)

    metadata = parent.stat()
    assert metadata.st_uid == os.geteuid()
    assert stat.S_IMODE(metadata.st_mode) == 0o700
    mount = parent / "mnt"
    assert mount.is_dir()
    assert f'mount -o ro "$cand" {shlex.quote(str(mount))}' in script
    assert f"cp -f {shlex.quote(str(mount))}/payload " in script
    assert f"rmdir {shlex.quote(str(mount))} {shlex.quote(str(parent))}" in script


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling_symlink"])
def test_guest_mount_rejects_any_existing_parent(guest_copy_script, tmp_path, kind):
    script, parent = guest_copy_script
    target = tmp_path / "unowned"
    if kind == "directory":
        parent.mkdir(mode=0o700)
    elif kind == "file":
        parent.write_bytes(b"unowned state")
    else:
        if kind == "symlink":
            target.mkdir()
        parent.symlink_to(target, target_is_directory=True)
    before = parent.lstat()

    completed = subprocess.run(
        ["sh", "-c", script.split("dev=\n", 1)[0]],
        capture_output=True,
        timeout=5,
    )

    assert completed.returncode != 0
    after = parent.lstat()
    assert (after.st_ino, after.st_mode, after.st_uid) == (
        before.st_ino,
        before.st_mode,
        before.st_uid,
    )
    assert not (target / "mnt").exists()
    if kind == "file":
        assert parent.read_bytes() == b"unowned state"


@pytest.mark.skipif(os.geteuid() != 0, reason="verifies another guest uid")
def test_private_mount_parent_prevents_payload_substitution(
    guest_copy_script, tmp_path
):
    script, parent = guest_copy_script
    subprocess.run(["sh", "-c", script.split("dev=\n", 1)[0]], check=True, timeout=5)
    payload = parent / "mnt" / "payload"
    payload.write_bytes(b"original")
    tmp_path.chmod(0o1777)

    completed = subprocess.run(
        ["sh", "-c", "ln -sf /untrusted iso/mnt/payload"],
        cwd=tmp_path,
        user=65534,
        group=65534,
        extra_groups=(),
        capture_output=True,
        timeout=5,
    )

    assert completed.returncode != 0
    assert not payload.is_symlink()
    assert payload.read_bytes() == b"original"


def _make_env(
    *,
    iso_write_threshold_bytes: int | None = None,
    os_type: OsType | None = "l26",
) -> ProxmoxSandboxEnvironment:
    """Build a minimal env with mocked collaborators.

    Just enough to drive write_file's branching logic in unit tests.
    """
    infra = MagicMock()
    return ProxmoxSandboxEnvironment(
        infra_commands=infra,
        agent_commands=MagicMock(),
        ipam_mappings=(),
        vm_id=100,
        name="test-sandbox",
        all_vms={
            100: VmConfig(
                vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
                name="test-sandbox",
            )
        },
        sdn_zone_id=None,
        instance=None,
        pool_id=None,
        os_type=os_type,
        iso_write_threshold_bytes=iso_write_threshold_bytes,
    )


class TestIsoWriteThreshold:
    @pytest.mark.parametrize(
        "size,use_iso", [(40 * 1024, False), (40 * 1024 + 1, True)]
    )
    async def test_default_switches_after_single_qga_chunk(self, size, use_iso):
        env = _make_env()
        env.exec = AsyncMock()
        env._write_file_only = AsyncMock()
        contents = b"x" * size

        with patch("proxmoxsandbox._proxmox_sandbox_environment.IsoWriter") as writer:
            writer.return_value.write_file = AsyncMock()
            await env.write_file("/tmp/output", contents)

            if use_iso:
                writer.return_value.write_file.assert_awaited_once_with(
                    100, "/tmp/output", contents
                )
                env._write_file_only.assert_not_awaited()
            else:
                writer.assert_not_called()
                env._write_file_only.assert_awaited_once_with("/tmp/output", contents)

    def test_default_follows_class_override(self, monkeypatch):
        monkeypatch.setattr(ProxmoxSandboxEnvironment, "ISO_WRITE_THRESHOLD_BYTES", 8)
        env = _make_env()
        assert env.ISO_WRITE_THRESHOLD_BYTES == 8

        monkeypatch.setattr(ProxmoxSandboxEnvironment, "ISO_WRITE_THRESHOLD_BYTES", 16)
        assert env.ISO_WRITE_THRESHOLD_BYTES == 16

    @pytest.mark.parametrize("threshold", [0, 8])
    def test_override_only_changes_one_instance(self, monkeypatch, threshold):
        monkeypatch.setattr(ProxmoxSandboxEnvironment, "ISO_WRITE_THRESHOLD_BYTES", 16)
        configured = _make_env(iso_write_threshold_bytes=threshold)
        inherited = _make_env()

        assert configured.ISO_WRITE_THRESHOLD_BYTES == threshold
        assert inherited.ISO_WRITE_THRESHOLD_BYTES == 16
        assert ProxmoxSandboxEnvironment.ISO_WRITE_THRESHOLD_BYTES == 16

        monkeypatch.setattr(ProxmoxSandboxEnvironment, "ISO_WRITE_THRESHOLD_BYTES", 32)
        assert configured.ISO_WRITE_THRESHOLD_BYTES == threshold
        assert inherited.ISO_WRITE_THRESHOLD_BYTES == 32

    @pytest.mark.parametrize(
        "contents,use_iso",
        [
            pytest.param(b"x" * 7, False, id="bytes-below"),
            pytest.param(b"x" * 8, True, id="bytes-equal"),
            pytest.param(b"x" * 9, True, id="bytes-above"),
            pytest.param("é" * 3, False, id="utf8-below"),
            pytest.param("é" * 4, True, id="utf8-equal"),
            pytest.param("é" * 5, True, id="utf8-above"),
        ],
    )
    async def test_selects_by_encoded_bytes(self, contents, use_iso):
        env = _make_env(iso_write_threshold_bytes=8)
        env.exec = AsyncMock()
        env._write_file_only = AsyncMock()
        expected = contents if isinstance(contents, bytes) else contents.encode("utf-8")

        with patch("proxmoxsandbox._proxmox_sandbox_environment.IsoWriter") as writer:
            writer.return_value.write_file = AsyncMock()
            await env.write_file("/tmp/output", contents)

            if use_iso:
                writer.return_value.write_file.assert_awaited_once_with(
                    100, "/tmp/output", expected
                )
                env.exec.assert_not_awaited()
                env._write_file_only.assert_not_awaited()
                if isinstance(contents, bytes):
                    assert writer.return_value.write_file.await_args.args[2] is contents
            else:
                writer.assert_not_called()
                env._write_file_only.assert_awaited_once_with("/tmp/output", expected)

    @pytest.mark.parametrize("contents", [b"", b"x", "é"])
    async def test_zero_tries_iso_for_every_supported_write(self, contents):
        env = _make_env(iso_write_threshold_bytes=0)
        expected = contents if isinstance(contents, bytes) else contents.encode("utf-8")

        with patch("proxmoxsandbox._proxmox_sandbox_environment.IsoWriter") as writer:
            writer.return_value.write_file = AsyncMock()
            await env.write_file("/tmp/output", contents)
            writer.return_value.write_file.assert_awaited_once_with(
                100, "/tmp/output", expected
            )

    @pytest.mark.parametrize("threshold", [0, 8])
    async def test_windows_ignores_iso_threshold(self, threshold):
        env = _make_env(iso_write_threshold_bytes=threshold, os_type="win11")
        env.exec = AsyncMock()
        env._write_file_only = AsyncMock()
        contents = "é" * 8

        with patch("proxmoxsandbox._proxmox_sandbox_environment.IsoWriter") as writer:
            await env.write_file("C:\\output.txt", contents)
            writer.assert_not_called()

        env._write_file_only.assert_awaited_once_with(
            "C:\\output.txt", contents.encode("utf-8")
        )


class TestPerEnvWriteLock:
    """The ISO write lock is per-env (per-VM), not module-level per-vm_id."""

    def test_lock_is_an_asyncio_lock(self) -> None:
        import asyncio

        assert isinstance(_make_env()._iso_write_lock, asyncio.Lock)

    def test_two_envs_sharing_a_vm_id_have_distinct_locks(self) -> None:
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
        # Comfortably above ISO_WRITE_THRESHOLD_BYTES so we hit the fast path.
        payload = b"x" * (env.ISO_WRITE_THRESHOLD_BYTES + 1024)

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
