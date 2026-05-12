"""Unit tests for iso_write module.

These tests cover the pure-logic bits (ISO build/round-trip, slot
selection, per-VM locking) without needing a real Proxmox instance.
The end-to-end ISO hot-plug path is exercised incidentally by
test_write_file_large with a >1 MiB payload.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pycdlib
import pytest

from proxmoxsandbox._impl.iso_write import (
    _ATTACH_SLOTS,
    _ISO_PAYLOAD_NAME,
    IsoWriter,
    _build_iso,
    _vm_lock,
    _vm_locks,
)


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


class TestPickFreeSlot:
    @pytest.fixture
    def writer(self):
        async_proxmox = MagicMock()
        async_proxmox.request = AsyncMock()
        return IsoWriter(
            async_proxmox=async_proxmox,
            agent_commands=MagicMock(),
            storage_commands=MagicMock(),
            node="pve",
        )

    @pytest.mark.asyncio
    async def test_picks_ide2_when_all_free(self, writer):
        # Empty config = no IDE slots in use at all.
        writer.async_proxmox.request.return_value = {}
        slot = await writer._pick_free_slot(100)
        # _ATTACH_SLOTS preference order: ide2 first.
        assert slot == _ATTACH_SLOTS[0] == "ide2"

    @pytest.mark.asyncio
    async def test_empty_cdrom_form_is_free(self, writer):
        # The canonical "empty CD drive" form left after detach.
        writer.async_proxmox.request.return_value = {"ide2": "none,media=cdrom"}
        slot = await writer._pick_free_slot(100)
        assert slot == "ide2"

    @pytest.mark.asyncio
    async def test_skips_busy_slot(self, writer):
        # ide2 is genuinely in use (cloud-init drive backed by storage).
        writer.async_proxmox.request.return_value = {
            "ide2": "local:iso/some.iso,media=cdrom",
        }
        slot = await writer._pick_free_slot(100)
        # Falls through to next preference: ide1.
        assert slot == "ide1"

    @pytest.mark.asyncio
    async def test_raises_when_all_slots_busy(self, writer):
        writer.async_proxmox.request.return_value = {
            slot: "local:iso/x.iso,media=cdrom" for slot in _ATTACH_SLOTS
        }
        with pytest.raises(RuntimeError, match="no free IDE slot"):
            await writer._pick_free_slot(100)

    @pytest.mark.asyncio
    async def test_non_ide_keys_ignored(self, writer):
        # Real VM configs contain dozens of unrelated keys.
        writer.async_proxmox.request.return_value = {
            "name": "vm-foo",
            "cores": 2,
            "scsi0": "local-lvm:vm-100-disk-0",
            "ide2": "none,media=cdrom",
        }
        slot = await writer._pick_free_slot(100)
        assert slot == "ide2"
