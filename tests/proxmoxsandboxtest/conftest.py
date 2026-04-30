# tests/conftest.py
import logging
import os
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

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI  # noqa: E402
from proxmoxsandbox._impl.built_in_vm import BuiltInVM  # noqa: E402
from proxmoxsandbox._impl.infra_commands import (  # noqa: E402
    InfraCommands,
    ProxmoxTarget,
)
from proxmoxsandbox._impl.qemu_commands import (  # noqa: E402
    QemuCommands,
    VnetAliases,
)
from proxmoxsandbox._impl.sdn_commands import SdnCommands  # noqa: E402
from proxmoxsandbox._impl.storage_commands import LocalStorageCommands  # noqa: E402
from proxmoxsandbox._impl.task_wrapper import TaskWrapper  # noqa: E402
from proxmoxsandbox._proxmox_sandbox_environment import (  # noqa: E402
    ProxmoxSandboxEnvironment,
    ProxmoxSandboxEnvironmentConfig,
)
from proxmoxsandbox.schema import VmConfig, VmSourceConfig  # noqa: E402

# Set PROXMOX_WINDOWS_TEMPLATE_TAG to run tests against a Windows VM.
# The value should match the tag on an existing Proxmox VM template.
# The template must be tagged with "inspect;<tag>" and have the QEMU
# guest agent installed. When unset, Windows test variants are skipped.
WINDOWS_TEMPLATE_TAG = os.getenv("PROXMOX_WINDOWS_TEMPLATE_TAG")


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


def _get_os_params() -> list[str]:
    params = ["linux"]
    if WINDOWS_TEMPLATE_TAG:
        params.append("windows")
    return params


def _get_os_config(os_name: str) -> ProxmoxSandboxEnvironmentConfig:
    if os_name == "linux":
        return ProxmoxSandboxEnvironmentConfig()

    if os_name == "windows":
        return ProxmoxSandboxEnvironmentConfig(
            vms_config=(
                VmConfig(
                    vm_source_config=VmSourceConfig(
                        existing_vm_template_tag=WINDOWS_TEMPLATE_TAG
                    ),
                    os_type="win11",
                    uefi_boot=True,
                    is_sandbox=True,
                    ram_mb=8192,
                ),
            )
        )

    raise ValueError(f"Unknown OS: {os_name}")


@pytest.fixture(params=_get_os_params())
def sandbox_env_config_by_os(request) -> ProxmoxSandboxEnvironmentConfig:
    return _get_os_config(request.param)


@pytest.fixture(scope="function")
async def proxmox_sandbox_environment(
    sandbox_env_config_by_os: ProxmoxSandboxEnvironmentConfig,
) -> AsyncGenerator[ProxmoxSandboxEnvironment, None]:
    task_name = "from_conftest"
    await ProxmoxSandboxEnvironment.task_init(task_name=task_name, config=None)
    envs_dict = await ProxmoxSandboxEnvironment.sample_init(
        task_name=task_name,
        config=sandbox_env_config_by_os,
        metadata={},
    )
    default_env = envs_dict["default"]
    assert isinstance(default_env, ProxmoxSandboxEnvironment)

    yield default_env

    await ProxmoxSandboxEnvironment.sample_cleanup(
        task_name=task_name,
        config=sandbox_env_config_by_os,
        environments=envs_dict,
        interrupted=False,
    )
