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

# Dedicated CD-ROM slot, cold-added to every is_sandbox VM in
# qemu_commands.other_config_json. We always media-change this slot; never
# attach a new one. Hot-attach of a previously-absent sataN/ideN is silently
# dropped by Proxmox regardless of machine type, so the slot must exist at
# boot for QEMU to enumerate the AHCI controller.
_WRITE_SLOT = "sata5"

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
        attached = False
        timings: dict[str, float] = {}
        t_start = time.monotonic()
        try:
            t0 = time.monotonic()
            local_iso = await asyncio.to_thread(_build_iso, contents)
            timings["build"] = time.monotonic() - t0

            t0 = time.monotonic()
            iso_name = f"wf-{vm_id}-{time.time_ns()}-{_rand()}.iso"
            # Bypass storage_commands.upload_file_to_storage to skip the
            # task_wrapper wait. For a new random-named ISO, the curl POST
            # returns when the file is on disk; we don't need to wait for
            # Proxmox's content reindex task to complete before we can
            # reference it as a volid.
            await self.async_proxmox.upload_file_with_curl(
                self.node, LOCAL_STORAGE, local_iso, "iso", filename=iso_name
            )
            iso_volid = f"{LOCAL_STORAGE}:iso/{iso_name}"
            timings["upload"] = time.monotonic() - t0

            # On a freshly-booted VM the kernel sometimes refuses every
            # open() on /dev/sr* after the first media-change ("Can't open
            # blockdev"). Detaching and re-attaching emits a fresh
            # media-change event which the kernel then handles cleanly.
            t0 = time.monotonic()
            for attempt in range(2):
                await self._attach(vm_id, iso_volid)
                attached = True
                try:
                    await self._copy_in_guest(vm_id, filepath)
                    break
                except IOError as ex:
                    if attempt == 1:
                        raise
                    logger.warning(
                        f"iso_write copy failed (attempt {attempt + 1}/2) "
                        f"on vm {vm_id}; re-attaching: {ex}"
                    )
                    await self._detach(vm_id)
                    attached = False
            timings["attach_copy"] = time.monotonic() - t0
        finally:
            if attached:
                t0 = time.monotonic()
                try:
                    await self._detach(vm_id)
                except Exception as ex:
                    logger.warning(f"detach on vm {vm_id} failed: {ex}")
                timings["detach"] = time.monotonic() - t0
            if iso_volid is not None:
                t0 = time.monotonic()
                try:
                    await self._delete_iso(iso_volid)
                except Exception as ex:
                    logger.warning(f"delete iso {iso_volid} failed: {ex}")
                timings["delete"] = time.monotonic() - t0
            if local_iso is not None and local_iso.exists():
                try:
                    os.unlink(local_iso)
                except OSError as ex:
                    logger.warning(f"unlink {local_iso} failed: {ex}")
            total = time.monotonic() - t_start
            parts = " ".join(f"{k}={v:.2f}s" for k, v in timings.items())
            logger.info(
                f"iso_write vm={vm_id} size={len(contents)} "
                f"total={total:.2f}s {parts}"
            )

    @tenacity.retry(
        wait=tenacity.wait_exponential(min=0.5, exp_base=1.5),
        stop=tenacity.stop_after_delay(30),
    )
    async def _attach(self, vm_id: int, iso_volid: str) -> None:
        await self.async_proxmox.request(
            "POST",
            f"/nodes/{self.node}/qemu/{vm_id}/config",
            json={_WRITE_SLOT: f"{iso_volid},media=cdrom"},
        )

    @tenacity.retry(
        wait=tenacity.wait_exponential(min=0.5, exp_base=1.5),
        stop=tenacity.stop_after_delay(30),
    )
    async def _detach(self, vm_id: int) -> None:
        # Set back to empty; leaves the device in place for next write.
        await self.async_proxmox.request(
            "POST",
            f"/nodes/{self.node}/qemu/{vm_id}/config",
            json={_WRITE_SLOT: "none,media=cdrom"},
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

        # Wait for the kernel to notice the hot-plugged CDROM. 3s budget:
        # if the mount succeeds, it's usually first try (~50ms). If it
        # keeps failing, we're in the "Can't open blockdev" failure mode
        # and waiting longer won't help — the caller re-attaches and
        # tries again, which clears the failure.
        script = f"""set -e
mkdir -p -- "$(dirname -- {target_q})"
mkdir -p {mount_q}
dev=
for i in $(seq 1 6); do
  for cand in /dev/sr0 /dev/sr1 /dev/sr2 /dev/sr3; do
    if [ -b "$cand" ] && mount -o ro "$cand" {mount_q} 2>/dev/null; then
      if [ -f {mount_q}/{payload_q} ]; then
        dev="$cand"
        break 2
      fi
      umount {mount_q} 2>/dev/null || true
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
