"""The host runs the pve-qemu-kvm the provisioning scripts build.

Every other test passes on stock QEMU, so a host that skipped the rebuild looks
healthy without this one.
"""

import re
from pathlib import Path

import pytest

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox.schema import ProxmoxInstanceConfig

pytestmark = pytest.mark.req_proxmox

USERDATA = (
    Path(__file__).parents[2]
    / "src"
    / "proxmoxsandbox"
    / "scripts"
    / "ec2"
    / "userdata.sh"
)


def _patched_version() -> str:
    script = USERDATA.read_text()
    stock = re.search(r'^STOCK_VERSION="([^"]+)"$', script, flags=re.M)
    patched = re.search(r'^PATCHED_VERSION="([^"]+)"$', script, flags=re.M)
    assert stock and patched
    return patched.group(1).replace("${STOCK_VERSION}", stock.group(1))


async def test_host_runs_rebuilt_pve_qemu_kvm(
    async_proxmox_api: AsyncProxmoxAPI, instance_config: ProxmoxInstanceConfig
) -> None:
    packages = await async_proxmox_api.request(
        "GET", f"/nodes/{instance_config.node}/apt/versions"
    )
    installed = {p["Package"]: p.get("OldVersion") for p in packages}
    assert installed.get("pve-qemu-kvm") == _patched_version(), (
        "host runs pve-qemu-kvm"
        f" {installed.get('pve-qemu-kvm')}, not the provisioning scripts' rebuild."
        " Rebuild the host, or apply patch-pve-qemu.sh from scripts/ec2/userdata.sh."
    )
