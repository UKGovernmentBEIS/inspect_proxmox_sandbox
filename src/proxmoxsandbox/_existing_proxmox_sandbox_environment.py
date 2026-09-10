import json
from pathlib import Path
from typing import Any, Dict, cast, get_args

from inspect_ai.util import (
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
    sandboxenv,
)
from pydantic import BaseModel
from typing_extensions import override

from proxmoxsandbox._impl.agent_commands import AgentCommands
from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.infra_commands import InfraCommands, ProxmoxTarget
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import (
    ExistingProxmoxSandboxEnvironmentConfig,
    OsType,
)


@sandboxenv(name="proxmox-existing")
class ExistingProxmoxSandboxEnvironment(ProxmoxSandboxEnvironment):
    """A non-owning Inspect sandbox attached to an existing Proxmox VM."""

    @classmethod
    @override
    async def sample_init(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        metadata: dict[str, str],
    ) -> dict[str, SandboxEnvironment]:
        if isinstance(config, str):
            config_path = Path(config)
            try:
                config = cls.config_deserialize(
                    json.loads(config_path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError) as ex:
                raise ValueError(
                    f"Unable to load proxmox-existing config from {config_path}: {ex}"
                ) from ex
        if not isinstance(config, ExistingProxmoxSandboxEnvironmentConfig):
            raise ValueError(
                "config must be an ExistingProxmoxSandboxEnvironmentConfig"
            )

        pool_id = config.instance_pool_id
        instance = await cls.proxmox_pool.acquire_instance(pool_id)

        try:
            async_proxmox_api = AsyncProxmoxAPI.from_instance_config(instance)
            target = ProxmoxTarget(
                host=instance.host, port=instance.port, node=instance.node
            )
            try:
                infra_commands = InfraCommands.get_instance(target)
            except LookupError:
                infra_commands = InfraCommands.build(
                    async_proxmox_api, instance.node, instance.image_storage
                )
                InfraCommands.set_instance(target, infra_commands)

            vm_config = await infra_commands.qemu_commands.read_vm(config.vm_id)
            vm_status = await infra_commands.async_proxmox.request(
                "GET", f"/nodes/{instance.node}/qemu/{config.vm_id}/status/current"
            )
            if vm_status.get("status") != "running":
                raise ValueError(
                    f"Existing VM {config.vm_id} is not running "
                    f"(status={vm_status.get('status')!r})"
                )

            # The inherited sandbox operations all use QGA. await_vm performs a
            # bounded, retrying agent ping now that the status check has established
            # that this provider must not start the VM itself.
            await infra_commands.qemu_commands.await_vm(
                vm_id=config.vm_id, is_sandbox=True
            )

            os_type_value = vm_config.get("ostype", "l26")
            os_type = (
                cast(OsType, os_type_value)
                if os_type_value in get_args(OsType)
                else None
            )
            environment = cls(
                infra_commands=infra_commands,
                agent_commands=AgentCommands(
                    async_proxmox=infra_commands.async_proxmox,
                    node=instance.node,
                ),
                ipam_mappings=(),
                vm_id=config.vm_id,
                all_vm_ids=(),
                sdn_zone_id=None,
                instance=instance,
                pool_id=pool_id,
                os_type=os_type,
            )
            cls.logger.info(
                f"Attached non-owning sandbox to existing VM {config.vm_id} on "
                f"instance {instance.instance_id}"
            )
            return {"default": environment}
        except BaseException:
            await cls.proxmox_pool.release_instance(pool_id, instance)
            raise

    @classmethod
    @override
    async def sample_cleanup(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        environments: Dict[str, SandboxEnvironment],
        interrupted: bool,
    ) -> None:
        for environment in environments.values():
            if isinstance(environment, ExistingProxmoxSandboxEnvironment):
                if environment.instance is not None and environment.pool_id is not None:
                    await cls.proxmox_pool.release_instance(
                        environment.pool_id, environment.instance
                    )
                break

    @classmethod
    @override
    async def task_cleanup(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        cleanup: bool,
    ) -> None:
        # This provider owns no infrastructure. In particular, do not sweep the
        # shared InfraCommands registry: it may contain the attached range.
        return None

    @classmethod
    @override
    async def cli_cleanup(cls, id: str | None) -> None:
        print("proxmox-existing owns no resources; nothing to clean up.")

    @classmethod
    @override
    def config_deserialize(cls, config: dict[str, Any]) -> BaseModel:
        return ExistingProxmoxSandboxEnvironmentConfig(**config)
