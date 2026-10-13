"""Create the built-in VM templates on a Proxmox host ahead of time.

The provider creates a built-in template the first time an eval asks for it,
which downloads an upstream image and installs packages inside the guest. This
does the same for any or all built-ins without running an eval.
"""

from typing import Iterable, get_args

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox.schema import ProxmoxInstanceConfig, VmSourceConfig

BUILT_IN_NAMES: tuple[str, ...] = get_args(
    get_args(VmSourceConfig.model_fields["built_in"].annotation)[0]
)


async def ensure_built_in_templates(
    instance: ProxmoxInstanceConfig,
    names: Iterable[str] = BUILT_IN_NAMES,
    remove_source_images: bool = False,
) -> dict[str, int]:
    """Create any of the named built-in templates missing from ``instance``.

    Templates that already exist are left alone. The host needs internet access
    for any that don't.

    Args:
        instance: The Proxmox host to create templates on.
        names: Which built-ins to create; all of them by default.
        remove_source_images: Delete the downloaded upstream images afterwards.
            Templates keep their own copy of the disk, so this only saves space.

    Returns:
        Every built-in template on the host, as name to VM ID.
    """
    infra_commands = InfraCommands.build(
        AsyncProxmoxAPI.from_instance_config(instance),
        instance.node,
        instance.image_storage,
    )
    built_in_vm = infra_commands.built_in_vm
    # One at a time: each picks the next free VM ID when it starts.
    for name in names:
        await built_in_vm.ensure_exists(name)
    if remove_source_images:
        await built_in_vm.remove_source_images()
    return await built_in_vm.known_builtins()
