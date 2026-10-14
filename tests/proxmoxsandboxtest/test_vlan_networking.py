from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox._impl.sdn_commands import SdnCommands
from proxmoxsandbox._impl.storage_commands import LocalStorageCommands
from proxmoxsandbox._impl.task_wrapper import TaskWrapper
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import (
    ProxmoxSandboxEnvironmentConfig,
    SdnConfig,
    VmConfig,
    VmNicConfig,
    VmSourceConfig,
    VnetConfig,
)


@pytest.fixture
def api():
    mock = MagicMock(spec=AsyncProxmoxAPI)
    mock.request = AsyncMock(return_value=[])
    return mock


@pytest.fixture
def task_wrapper():
    async def run_action(action):
        await action()

    mock = MagicMock(spec=TaskWrapper)
    mock.do_action_and_wait_for_tasks = AsyncMock(side_effect=run_action)
    return mock


def test_vlan_config_deserializes():
    config = ProxmoxSandboxEnvironmentConfig(
        vms_config=(
            VmConfig(
                vm_source_config=VmSourceConfig(existing_vm_template_tag="guest"),
                nics=(VmNicConfig(vnet_alias="fabric", vlan_tag=12),),
            ),
        ),
        sdn_config=SdnConfig(
            vnet_configs=(VnetConfig(alias="fabric", vlan_aware=True),),
            use_pve_ipam_dnsnmasq=False,
        ),
    )

    restored = ProxmoxSandboxEnvironment.config_deserialize(
        config.model_dump(mode="json")
    )

    assert restored == config
    assert isinstance(restored, ProxmoxSandboxEnvironmentConfig)
    assert restored.vms_config[0].nics is not None
    assert restored.vms_config[0].nics[0].vlan_tag == 12
    assert isinstance(restored.sdn_config, SdnConfig)
    assert restored.sdn_config.vnet_configs[0].vlan_aware is True


@pytest.mark.parametrize("vlan_aware", [False, True])
async def test_create_vlan_aware_vnet(api, vlan_aware):
    commands = SdnCommands(api)
    zone, aliases = await commands.create_sdn(
        "vln123",
        SdnConfig(
            vnet_configs=(VnetConfig(alias="fabric", vlan_aware=vlan_aware),),
            use_pve_ipam_dnsnmasq=False,
        ),
    )

    expected: dict[str, str | bool] = {
        "vnet": "vln123v0",
        "zone": "vln123z",
        "alias": "fabric",
    }
    if vlan_aware:
        expected["vlanaware"] = True
    api.request.assert_any_await("POST", "/cluster/sdn/vnets", json=expected)
    api.request.assert_any_await("PUT", "/cluster/sdn")
    assert (zone, aliases) == ("vln123z", [("vln123v0", "fabric")])


@pytest.mark.parametrize("vlan_tag", [None, 12, 4094])
@pytest.mark.parametrize("existing_vnet", [False, True])
async def test_nic_vlan_tag_keeps_mac_firewall_and_order(
    api, task_wrapper, vlan_tag, existing_vnet
):
    commands = QemuCommands(
        api, "pve", "local", task_wrapper, MagicMock(spec=LocalStorageCommands)
    )
    aliases = [("uplink0", "uplink"), ("fabric0", "fabric")]
    if existing_vnet:
        api.request.return_value = [
            {"vnet": vnet, "alias": alias} for vnet, alias in aliases
        ]

    config = VmConfig(
        vm_source_config=VmSourceConfig(existing_vm_template_tag="guest"),
        firewall=True,
        nics=(
            VmNicConfig(vnet_alias="uplink", mac="bc:24:11:00:00:01"),
            VmNicConfig(
                vnet_alias="fabric", mac="02:00:00:00:00:02", vlan_tag=vlan_tag
            ),
        ),
    )
    old_nics = {"net0": "old0", "net1": "old1"}
    with patch.object(commands, "read_vm", AsyncMock(return_value=old_nics)):
        await commands.configure_network_and_tags(
            config, [] if existing_vnet else aliases, vm_id=101
        )

    tag = "" if vlan_tag is None else f",tag={vlan_tag}"
    api.request.assert_any_await(
        "POST",
        "/nodes/pve/qemu/101/config",
        json={
            "net0": "virtio,bridge=uplink0,macaddr=BC:24:11:00:00:01,firewall=1",
            "net1": f"virtio,bridge=fabric0,macaddr=02:00:00:00:00:02{tag},firewall=1",
        },
    )
    for name in old_nics:
        api.request.assert_any_await(
            "PUT",
            "/nodes/pve/qemu/101/config",
            body_content=f"delete={name}",
            content_type="application/x-www-form-urlencoded",
        )


async def test_unspecified_template_nics_are_not_rewritten(api, task_wrapper):
    commands = QemuCommands(
        api, "pve", "local", task_wrapper, MagicMock(spec=LocalStorageCommands)
    )
    config = VmConfig(vm_source_config=VmSourceConfig(existing_vm_template_tag="guest"))

    await commands.configure_network_and_tags(config, [], vm_id=101)

    assert api.request.await_args_list == [
        call("GET", "/cluster/sdn/vnets"),
        call("POST", "/nodes/pve/qemu/101/config", json={"tags": "inspect"}),
    ]
