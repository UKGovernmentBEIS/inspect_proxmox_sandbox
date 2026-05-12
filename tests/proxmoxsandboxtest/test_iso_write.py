"""Unit tests for iso_write module.

Covers the pure-logic bits (ISO build/round-trip, per-VM locking) without
needing a real Proxmox instance. The end-to-end ISO path is exercised
incidentally by test_write_file_large with a >1 MiB payload.
"""

from __future__ import annotations

import pycdlib

from proxmoxsandbox._impl.iso_write import (
    _ISO_PAYLOAD_NAME,
    _WRITE_SLOT,
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


def test_write_slot_is_sata5():
    # qemu_commands.other_config_json cold-adds this exact slot on every
    # is_sandbox VM. Keep the constant in sync with that.
    assert _WRITE_SLOT == "sata5"
