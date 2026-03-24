# tests/conftest.py
import logging
import random
from typing import AsyncGenerator

import pytest

# httpcore and httpx emit extremely verbose DEBUG logs (every TCP connect,
# TLS handshake, header send/receive, body send/receive, etc.) that drown
# out application-level debug output.  Rather than disabling DEBUG globally,
# we raise the level on just these loggers so our own DEBUG messages remain
# visible.
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.built_in_vm import BuiltInVM
from proxmoxsandbox._impl.infra_commands import InfraCommands, ProxmoxTarget
from proxmoxsandbox._impl.qemu_commands import QemuCommands, VnetAliases
from proxmoxsandbox._impl.sdn_commands import SdnCommands
from proxmoxsandbox._impl.storage_commands import LocalStorageCommands
from proxmoxsandbox._impl.task_wrapper import TaskWrapper
from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironment,
    ProxmoxSandboxEnvironmentConfig,
)


@pytest.fixture
async def sandbox_env_config() -> ProxmoxSandboxEnvironmentConfig:
    return ProxmoxSandboxEnvironmentConfig()


@pytest.fixture
async def async_proxmox_api(
    sandbox_env_config: ProxmoxSandboxEnvironmentConfig,
) -> AsyncGenerator[AsyncProxmoxAPI, None]:
    yield AsyncProxmoxAPI(
        host=f"{sandbox_env_config.host}:{sandbox_env_config.port}",
        user=f"{sandbox_env_config.user}@{sandbox_env_config.user_realm}",
        password=sandbox_env_config.password,
        verify_tls=sandbox_env_config.verify_tls,
    )


@pytest.fixture
async def infra_commands(
    async_proxmox_api: AsyncProxmoxAPI,
    sandbox_env_config: ProxmoxSandboxEnvironmentConfig,
) -> InfraCommands:
    target = ProxmoxTarget(
        host=sandbox_env_config.host,
        port=sandbox_env_config.port,
        node=sandbox_env_config.node,
    )
    instance = InfraCommands.build(
        async_proxmox_api, sandbox_env_config.node, sandbox_env_config.image_storage
    )
    InfraCommands.set_instance(target, instance)
    return instance


@pytest.fixture
async def sdn_commands(infra_commands: InfraCommands) -> SdnCommands:
    return infra_commands.sdn_commands


@pytest.fixture
async def storage_commands(
    async_proxmox_api: AsyncProxmoxAPI,
    sandbox_env_config: ProxmoxSandboxEnvironmentConfig,
) -> LocalStorageCommands:
    task_wrapper = TaskWrapper(async_proxmox_api)
    return LocalStorageCommands(
        async_proxmox_api, sandbox_env_config.node, task_wrapper
    )


@pytest.fixture
async def qemu_commands(infra_commands: InfraCommands) -> QemuCommands:
    return infra_commands.qemu_commands


@pytest.fixture
async def built_in_vm(infra_commands: InfraCommands) -> BuiltInVM:
    return infra_commands.built_in_vm


@pytest.fixture(scope="function")
async def ids_start() -> str:
    # this could definitely be improved to go and check
    # proxmox and find a non-conflicting ID
    ids_start = f"cts{random.randint(100, 999)}"
    return ids_start


@pytest.fixture
async def auto_sdn_vnet_aliases(
    ids_start: str, sdn_commands: SdnCommands
) -> AsyncGenerator[VnetAliases, None]:
    sdn_zone_id, vnet_aliases = await sdn_commands.create_sdn(ids_start, "auto")
    assert sdn_zone_id is not None
    yield vnet_aliases
    await sdn_commands.tear_down_sdn_zone_and_vnet(sdn_zone_id, ())


@pytest.fixture(scope="function")
async def proxmox_sandbox_environment(
    sandbox_env_config: ProxmoxSandboxEnvironmentConfig,
) -> AsyncGenerator[ProxmoxSandboxEnvironment, None]:
    task_name = "from_conftest"
    await ProxmoxSandboxEnvironment.task_init(task_name=task_name, config=None)
    envs_dict = await ProxmoxSandboxEnvironment.sample_init(
        task_name=task_name,
        config=sandbox_env_config,
        metadata={},
    )
    default_env = envs_dict["default"]
    assert isinstance(default_env, ProxmoxSandboxEnvironment)
    yield default_env
    await ProxmoxSandboxEnvironment.sample_cleanup(
        task_name=task_name,
        config=sandbox_env_config,
        environments=envs_dict,
        interrupted=False,
    )
