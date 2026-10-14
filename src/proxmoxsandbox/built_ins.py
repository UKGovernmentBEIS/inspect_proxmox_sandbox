"""Create the built-in VM templates on a Proxmox host ahead of time.

The provider creates a built-in template the first time an eval asks for it,
which downloads an upstream image and installs packages inside the guest. This
does the same for every built-in without running an eval.

Also runnable as ``python -m proxmoxsandbox.built_ins`` against the host named
by the ``PROXMOX_*`` environment variables, which is how the EC2 provisioner
bakes the templates on the host itself (see ``scripts/ec2/README.md``).
"""

import asyncio
from typing import get_args

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox.schema import (
    ProxmoxInstanceConfig,
    VmSourceConfig,
    _load_single_instance_from_env,
)

BUILT_IN_NAMES: tuple[str, ...] = get_args(
    get_args(VmSourceConfig.model_fields["built_in"].annotation)[0]
)


async def ensure_built_in_templates(instance: ProxmoxInstanceConfig) -> dict[str, int]:
    """Create any built-in templates missing from ``instance``.

    Templates that already exist are left alone. The host needs internet access
    for any that don't. Deletes the downloaded upstream images afterwards:
    templates keep their own copy of the disk.

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
    for name in BUILT_IN_NAMES:
        await built_in_vm.ensure_exists(name)
    await built_in_vm.remove_source_images()
    return await built_in_vm.known_builtins()


def main() -> None:
    """Create every built-in on the host named by the ``PROXMOX_*`` variables."""
    templates = asyncio.run(ensure_built_in_templates(_load_single_instance_from_env()))
    for name, vm_id in templates.items():
        print(f"{name}: VM {vm_id}")


if __name__ == "__main__":
    main()
