import http.client as http_client
import logging
import re
from typing import Dict, Tuple

from inspect_ai.util import SandboxEnvironment

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironment,
    ProxmoxSandboxEnvironmentConfig,
)

HOST_CONTRACT = 3


def host_contract(version: str) -> int:
    """The `.aisiN` stamp inspect-proxmox-host writes into pvecfg.pm, else 0.

    N is that package's version. A stock host reads 0, as does one where a patch
    the stamp guards has gone missing.
    """
    match = re.search(r"\.aisi(\d+)", version)
    return int(match.group(1)) if match else 0


async def require_host_contract(
    api: AsyncProxmoxAPI, want: int = HOST_CONTRACT
) -> None:
    await api.request("GET", "/version")  # ensure a ticket, which caches the version
    version = api.get_discovered_proxmox_version().version
    found = host_contract(version)
    assert found >= want, (
        f"host reports {version}, i.e. contract aisi{found}, but this test asserts"
        f" behaviour only aisi{want} hosts have. Install inspect-proxmox-host"
        f" {want} or later on the host, or rebuild it (see host/README.md)."
    )


def setup_requests_logging() -> None:
    http_client.HTTPConnection.debuglevel = 1
    logging.basicConfig()
    logging.getLogger().setLevel(logging.DEBUG)
    requests_log = logging.getLogger("requests.packages.urllib3")
    requests_log.setLevel(logging.DEBUG)
    requests_log.propagate = True


async def setup_sandbox(
    task_name: str, config: ProxmoxSandboxEnvironmentConfig
) -> Tuple[str, Dict[str, SandboxEnvironment]]:
    await ProxmoxSandboxEnvironment.task_init(task_name=task_name, config=config)
    envs_dict = await ProxmoxSandboxEnvironment.sample_init(
        task_name=task_name,
        config=config,
        metadata={},
    )
    return task_name, envs_dict
