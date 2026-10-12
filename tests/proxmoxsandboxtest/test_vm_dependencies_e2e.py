"""End-to-end: depends_on + healthcheck against a live Proxmox instance."""

from typing import Dict

import pytest
from inspect_ai.util import SandboxEnvironment

from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import (
    HealthCheck,
    ProxmoxSandboxEnvironmentConfig,
    VmConfig,
    VmSourceConfig,
)

from .proxmox_sandbox_utils import setup_sandbox

pytestmark = pytest.mark.req_proxmox


async def test_depends_on_with_healthcheck() -> None:
    """`b` waits for `a`'s healthcheck; both are reachable by name afterwards."""
    task_name = "test_depends_on"
    envs_dict: Dict[str, SandboxEnvironment] = {}
    sandbox_env_config = ProxmoxSandboxEnvironmentConfig(
        vms_config=(
            VmConfig(
                name="a",
                vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
                ram_mb=512,
                vcpus=1,
                # `--wait` blocks until systemd has settled (or degraded);
                # exit 0 only when fully running.
                healthcheck=HealthCheck(
                    test=("systemctl", "is-system-running", "--wait"),
                    timeout=120,
                    retries=10,
                ),
            ),
            VmConfig(
                name="b",
                vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
                ram_mb=512,
                vcpus=1,
                depends_on=("a",),
            ),
        ),
    )

    try:
        _, envs_dict = await setup_sandbox(task_name, sandbox_env_config)
        assert set(envs_dict) == {"default", "a", "b"}
        assert envs_dict["default"] is envs_dict["a"]

        # `a` satisfied its healthcheck before `b` was created, so it is settled.
        result = await envs_dict["a"].exec(["systemctl", "is-system-running"])
        assert result.stdout.strip() == "running", result

        result = await envs_dict["b"].exec(["echo", "hello"])
        assert result.success and result.stdout.strip() == "hello", result
    finally:
        if envs_dict:
            await ProxmoxSandboxEnvironment.sample_cleanup(
                task_name=task_name,
                config=sandbox_env_config,
                environments=envs_dict,
                interrupted=False,
            )
