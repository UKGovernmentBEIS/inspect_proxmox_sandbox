"""ISO9660 hot-plug write_file path.

Bypasses the QGA `guest-file-write` per-call cap (~60 KiB through Proxmox's
Agent.pm wrapper) by writing the payload into a single-file ISO9660 image,
uploading it to Proxmox storage, hot-attaching as a CD-ROM, then having
the guest agent mount + cp + unmount, before detaching and deleting.

Linux only for now. Caller is expected to size-gate (this path has fixed
per-call overhead from ISO upload/attach/mount, so QGA wins for small files).
"""

from __future__ import annotations

import asyncio
import os
import random
import shlex
import string
import tempfile
import time
from io import BytesIO
from logging import getLogger
from pathlib import Path
from typing import BinaryIO, cast

import httpx
import pycdlib
import tenacity
from inspect_ai.util import trace_action

from proxmoxsandbox._impl.agent_commands import AgentCommands
from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.storage_commands import LOCAL_STORAGE, LocalStorageCommands

logger = getLogger(__name__)

# Filename on the ISO (Rock Ridge long name visible to Linux guest)
_ISO_PAYLOAD_NAME = "payload"
_ISO_PAYLOAD_JOLIET = "/PAYLOAD"
_ISO_PAYLOAD_ISO9660 = "/PAYLOAD.;1"

# Preferred attach slots. ide2 is the cloud-init slot kept around as
# `none,media=cdrom` by the built-in VM template — swapping media into an
# existing CDROM slot triggers a QEMU monitor `change` command, which the
# guest kernel sees as a media-change event. Hot-*adding* a brand-new IDE
# slot (e.g. ide3 that wasn't in the original VM config) does not trigger
# a rescan, so the guest never sees /dev/sr1 even though Proxmox accepts
# the config update.
_ATTACH_SLOTS = ("ide2", "ide1", "ide0", "ide3")

# Per-VM serialization. ISO attach uses a single IDE slot; concurrent writes
# to the same VM would clobber each other otherwise. Module-level so all
# IsoWriter instances share locks per vm_id.
_vm_locks: dict[int, asyncio.Lock] = {}


def _vm_lock(vm_id: int) -> asyncio.Lock:
    lock = _vm_locks.get(vm_id)
    if lock is None:
        lock = asyncio.Lock()
        _vm_locks[vm_id] = lock
    return lock


def _rand(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _build_iso(contents: bytes) -> Path:
    """Build a single-file ISO9660 image and return the local path."""
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.12", vol_ident="WRITEFILE")
    buf = BytesIO(contents)
    iso.add_fp(
        buf,
        len(contents),
        _ISO_PAYLOAD_ISO9660,
        joliet_path=_ISO_PAYLOAD_JOLIET,
        rr_name=_ISO_PAYLOAD_NAME,
    )
    with tempfile.NamedTemporaryFile(delete=False, suffix=".iso") as tmp:
        iso.write_fp(cast(BinaryIO, tmp))
        return Path(tmp.name)


class IsoWriter:
    """Write a file into a Linux guest VM via ISO9660 hot-plug."""

    def __init__(
        self,
        async_proxmox: AsyncProxmoxAPI,
        agent_commands: AgentCommands,
        storage_commands: LocalStorageCommands,
        node: str,
    ) -> None:
        self.async_proxmox = async_proxmox
        self.agent_commands = agent_commands
        self.storage_commands = storage_commands
        self.node = node

    async def write_file(
        self, vm_id: int, filepath: str, contents: bytes
    ) -> None:
        """Write `contents` to `filepath` inside the guest VM."""
        async with _vm_lock(vm_id):
            with trace_action(
                logger,
                "iso_write_file",
                f"vm={vm_id} target={filepath} size={len(contents)}",
            ):
                await self._do_write(vm_id, filepath, contents)

    async def _do_write(
        self, vm_id: int, filepath: str, contents: bytes
    ) -> None:
        local_iso: Path | None = None
        iso_volid: str | None = None
        slot: str | None = None
        try:
            local_iso = await asyncio.to_thread(_build_iso, contents)
            iso_name = f"wf-{vm_id}-{time.time_ns()}-{_rand()}.iso"
            await self.storage_commands.upload_file_to_storage(
                file=local_iso, content_type="iso", filename=iso_name
            )
            iso_volid = f"{LOCAL_STORAGE}:iso/{iso_name}"

            slot = await self._pick_free_slot(vm_id)
            await self._attach(vm_id, slot, iso_volid)

            await self._copy_in_guest(vm_id, filepath)
        finally:
            if slot is not None:
                try:
                    await self._detach(vm_id, slot)
                except Exception as ex:
                    logger.warning(
                        f"detach {slot} on vm {vm_id} failed: {ex}"
                    )
            if iso_volid is not None:
                try:
                    await self._delete_iso(iso_volid)
                except Exception as ex:
                    logger.warning(f"delete iso {iso_volid} failed: {ex}")
            if local_iso is not None and local_iso.exists():
                try:
                    os.unlink(local_iso)
                except OSError as ex:
                    logger.warning(f"unlink {local_iso} failed: {ex}")

    async def _pick_free_slot(self, vm_id: int) -> str:
        config = await self.async_proxmox.request(
            "GET", f"/nodes/{self.node}/qemu/{vm_id}/config"
        )
        for slot in _ATTACH_SLOTS:
            value = config.get(slot)
            if value is None:
                return slot
            # `none,media=cdrom` is the canonical "empty CD drive" form left
            # behind after detach — treat it as free.
            if isinstance(value, str) and value.startswith("none,"):
                return slot
        raise RuntimeError(
            f"VM {vm_id} has no free IDE slot for write_file ISO attach; "
            f"existing config keys: {sorted(k for k in config if k.startswith('ide'))}"
        )

    @tenacity.retry(
        wait=tenacity.wait_exponential(min=0.5, exp_base=1.5),
        stop=tenacity.stop_after_delay(30),
    )
    async def _attach(self, vm_id: int, slot: str, iso_volid: str) -> None:
        await self.async_proxmox.request(
            "POST",
            f"/nodes/{self.node}/qemu/{vm_id}/config",
            json={slot: f"{iso_volid},media=cdrom"},
        )

    @tenacity.retry(
        wait=tenacity.wait_exponential(min=0.5, exp_base=1.5),
        stop=tenacity.stop_after_delay(30),
    )
    async def _detach(self, vm_id: int, slot: str) -> None:
        # Setting `none,media=cdrom` leaves the empty drive in place rather
        # than removing the device altogether — matches what the built-in
        # template code does post-install and avoids QEMU rejecting a hot
        # device removal if the guest hasn't released it yet.
        await self.async_proxmox.request(
            "POST",
            f"/nodes/{self.node}/qemu/{vm_id}/config",
            json={slot: "none,media=cdrom"},
        )

    async def _delete_iso(self, iso_volid: str) -> None:
        try:
            await self.async_proxmox.request(
                "DELETE",
                f"/nodes/{self.node}/storage/{LOCAL_STORAGE}/content/{iso_volid}",
            )
        except httpx.HTTPStatusError as ex:
            # 500 with "does not exist" = already cleaned, fine
            if ex.response.status_code == 500 and "does not exist" in ex.response.text:
                return
            raise

    async def _copy_in_guest(self, vm_id: int, target: str) -> None:
        """Find the freshly attached CDROM in the guest, mount, cp, unmount.

        Runs as a single shell script via QGA exec to minimise round-trips
        and inherit shell-level error handling.
        """
        mount_dir = f"/tmp/_wf_iso_{_rand()}_{time.time_ns()}"
        target_q = shlex.quote(target)
        mount_q = shlex.quote(mount_dir)
        payload_q = shlex.quote(_ISO_PAYLOAD_NAME)

        # Wait for the kernel to notice the hot-plugged CDROM (sr0 is
        # conventional; on busier setups it might be sr1+ — match any sr*).
        # We try a brief loop because the device file may not appear for a
        # second or two after the API attach call returns.
        script = f"""set -e
mkdir -p -- "$(dirname -- {target_q})"
mkdir -p {mount_q}
dev=
for i in $(seq 1 60); do
  for cand in /dev/sr0 /dev/sr1 /dev/sr2 /dev/sr3; do
    if [ -b "$cand" ]; then
      # Probe whether THIS device has our payload file before committing.
      if mount -o ro "$cand" {mount_q} 2>/dev/null; then
        if [ -f {mount_q}/{payload_q} ]; then
          dev="$cand"
          break 2
        fi
        umount {mount_q} 2>/dev/null || true
      fi
    fi
  done
  sleep 0.5
done
if [ -z "$dev" ]; then
  echo "iso_write: no cdrom with payload found after wait" 1>&2
  echo "--- diag: ls /dev/sr* ---" 1>&2
  ls -la /dev/sr* 2>&1 1>&2 || true
  echo "--- diag: lsblk ---" 1>&2
  lsblk 2>&1 1>&2 || true
  echo "--- diag: dmesg tail ---" 1>&2
  dmesg 2>/dev/null | tail -20 1>&2 || true
  rmdir {mount_q} 2>/dev/null || true
  exit 2
fi
cp -f {mount_q}/{payload_q} {target_q}
umount {mount_q}
rmdir {mount_q}
"""
        exec_resp = await self.agent_commands.exec_command(
            vm_id=vm_id, command=["sh", "-c", script]
        )
        pid = exec_resp["pid"]

        @tenacity.retry(
            wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
            stop=tenacity.stop_after_delay(120),
            retry=tenacity.retry_if_result(lambda r: r is False),
        )
        async def wait() -> bool | dict:
            status = await self.agent_commands.get_agent_exec_status(
                vm_id=vm_id, pid=pid
            )
            if status.get("exited") != 1:
                return False
            return status

        status = await wait()
        assert isinstance(status, dict)
        exitcode = status.get("exitcode", 1)
        if exitcode != 0:
            stderr = status.get("err-data", "")
            stdout = status.get("out-data", "")
            raise IOError(
                f"iso_write guest copy failed (exitcode={exitcode}): "
                f"stderr={stderr!r} stdout={stdout!r}"
            )
