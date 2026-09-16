"""Fixtures for the malicious-QGA suite.

Self-contained (the shared `proxmox_sandbox_environment` fixture lives in the
sibling `proxmoxsandboxtest` conftest, out of scope here). Each test gets a
fresh sandbox VM built from the single-instance env vars, so the destructive
agent swap in one scenario never leaks into the next.
"""

from typing import AsyncGenerator

import pytest

from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironment,
    ProxmoxSandboxEnvironmentConfig,
)


@pytest.fixture(autouse=True)
def reset_global_pool_state():
    ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()
    InfraCommands._instances.clear()
    yield
    ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()
    InfraCommands._instances.clear()


@pytest.fixture(scope="function")
async def proxmox_sandbox_environment() -> AsyncGenerator[
    ProxmoxSandboxEnvironment, None
]:
    config = ProxmoxSandboxEnvironmentConfig()
    task_name = "malicious_qga"
    await ProxmoxSandboxEnvironment.task_init(task_name=task_name, config=None)
    envs_dict = await ProxmoxSandboxEnvironment.sample_init(
        task_name=task_name,
        config=config,
        metadata={},
    )
    default_env = envs_dict["default"]
    assert isinstance(default_env, ProxmoxSandboxEnvironment)

    yield default_env

    await ProxmoxSandboxEnvironment.sample_cleanup(
        task_name=task_name,
        config=config,
        environments=envs_dict,
        interrupted=False,
    )
