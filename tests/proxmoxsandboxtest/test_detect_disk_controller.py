import pytest

from proxmoxsandbox._impl.qemu_commands import QemuCommands


def test_detects_scsi():
    config = {
        "scsi0": "local-lvm:vm-100-disk-0,size=32G",
        "sata5": "none,media=cdrom",
        "net0": "virtio,bridge=vmbr0",
    }
    assert QemuCommands._detect_disk_controller(config) == "scsi"


def test_detects_ide():
    config = {
        "ide0": "local-lvm:vm-100-disk-0,size=32G",
        "ide1": "local-lvm:vm-100-disk-1,size=8G",
        "sata5": "none,media=cdrom",
    }
    assert QemuCommands._detect_disk_controller(config) == "ide"


def test_ignores_cdrom_on_scsi_ide_buses():
    # A CD-ROM attached to ide2 must not be mistaken for a data disk.
    config = {
        "ide2": "none,media=cdrom",
        "scsi0": "local-lvm:vm-100-disk-0,size=32G",
    }
    assert QemuCommands._detect_disk_controller(config) == "scsi"


def test_ignores_efidisk():
    config = {
        "efidisk0": "local-lvm:vm-100-disk-1,efitype=4m,pre-enrolled-keys=0",
        "scsi0": "local-lvm:vm-100-disk-0,size=32G",
    }
    assert QemuCommands._detect_disk_controller(config) == "scsi"


def test_raises_on_mixed_controllers():
    config = {
        "scsi0": "local-lvm:vm-100-disk-0,size=32G",
        "ide0": "local-lvm:vm-100-disk-1,size=8G",
    }
    with pytest.raises(ValueError, match="span multiple controllers") as exc_info:
        QemuCommands._detect_disk_controller(config)
    # The user's way out of an unverifiable template is to stop requesting a
    # specific controller, so the error must say so.
    assert "leave disk_controller unspecified" in str(exc_info.value)


def test_raises_when_no_data_disks():
    # e.g. a virtio-backed template, plus only a CD-ROM on a scsi/ide bus.
    config = {
        "virtio0": "local-lvm:vm-100-disk-0,size=32G",
        "ide2": "none,media=cdrom",
    }
    with pytest.raises(ValueError, match="no scsi or ide data disks") as exc_info:
        QemuCommands._detect_disk_controller(config)
    assert "leave disk_controller unspecified" in str(exc_info.value)
