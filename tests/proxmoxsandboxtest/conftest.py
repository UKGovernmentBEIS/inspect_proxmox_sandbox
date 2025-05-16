# tests/conftest.py
import random
from typing import AsyncGenerator

import pytest

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.built_in_vm import BuiltInVM
from proxmoxsandbox._impl.qemu_commands import QemuCommands, VnetAliases
from proxmoxsandbox._impl.sdn_commands import SdnCommands
from proxmoxsandbox._impl.storage_commands import StorageCommands
from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironment,
    ProxmoxSandboxEnvironmentConfig,
)


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
async def node(
    sandbox_env_config: ProxmoxSandboxEnvironmentConfig,
) -> str:
    return sandbox_env_config.node


@pytest.fixture
async def sandbox_env_config() -> ProxmoxSandboxEnvironmentConfig:
    return ProxmoxSandboxEnvironmentConfig()


@pytest.fixture
async def sdn_commands(async_proxmox_api: AsyncProxmoxAPI) -> SdnCommands:
    return SdnCommands(async_proxmox_api)


@pytest.fixture
async def qemu_commands(async_proxmox_api: AsyncProxmoxAPI, node: str) -> QemuCommands:
    return QemuCommands(async_proxmox_api, node=node)


@pytest.fixture
async def built_in_vm(async_proxmox_api: AsyncProxmoxAPI, node: str) -> BuiltInVM:
    return BuiltInVM(async_proxmox_api, node=node)


@pytest.fixture
async def storage_commands(
    async_proxmox_api: AsyncProxmoxAPI, node: str
) -> StorageCommands:
    return StorageCommands(async_proxmox_api, node=node, storage="local")


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
    await sdn_commands.tear_down_sdn_zone_and_vnet(sdn_zone_id)


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
