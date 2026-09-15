from ipaddress import ip_address

import pytest
from inspect_ai.util import SandboxEnvironmentSpec
from pydantic import ValidationError
from pydantic_extra_types.mac_address import MacAddress

from proxmoxsandbox.schema import (
    ProxmoxSandboxEnvironmentConfig,
    VmConfig,
    VmNicConfig,
    VmSourceConfig,
)


def vm_config(
    *,
    depends_on: tuple[str, ...] = (),
    await_before_next_vm: bool = False,
) -> VmConfig:
    return VmConfig(
        vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
        depends_on=depends_on,
        await_before_next_vm=await_before_next_vm,
    )


def test_vmnicconfig_ipv4_requires_mac():
    """IPv4 requires MAC address."""
    with pytest.raises(ValidationError, match="ipv4 address requires a mac address"):
        VmNicConfig(vnet_alias="test", ipv4=ip_address("192.168.1.10"))


def test_vmnicconfig_ipv4_with_mac():
    """IPv4 works with MAC address."""
    nic = VmNicConfig(
        vnet_alias="test",
        mac=MacAddress("52:54:00:12:34:56"),
        ipv4=ip_address("192.168.1.10"),
    )
    assert nic.ipv4 == ip_address("192.168.1.10")
    assert str(nic.mac) == "52:54:00:12:34:56"


def test_vmnicconfig_mac_only():
    """MAC without IPv4 is valid."""
    nic = VmNicConfig(vnet_alias="test", mac=MacAddress("52:54:00:12:34:56"))
    assert nic.ipv4 is None
    assert str(nic.mac) == "52:54:00:12:34:56"


def test_vmnicconfig_minimal():
    """Minimal config without MAC or IPv4."""
    nic = VmNicConfig(vnet_alias="test")
    assert nic.mac is None
    assert nic.ipv4 is None


def test_tuple_vms_config_keeps_legacy_startup_barriers():
    config = ProxmoxSandboxEnvironmentConfig(
        vms_config=(vm_config(await_before_next_vm=True), vm_config())
    )

    assert isinstance(config.vms_config, tuple)
    assert config.vms_config[0].await_before_next_vm is True


def test_tuple_vms_config_rejects_dependencies():
    with pytest.raises(ValidationError, match="depends_on requires vms_config"):
        ProxmoxSandboxEnvironmentConfig(
            vms_config=(vm_config(depends_on=("database",)),)
        )


def test_dictionary_vms_config_rejects_legacy_startup_barriers():
    with pytest.raises(ValidationError, match="await_before_next_vm is not valid"):
        ProxmoxSandboxEnvironmentConfig(
            vms_config={"database": vm_config(await_before_next_vm=True)}
        )


def test_dictionary_vms_config_rejects_unknown_dependencies():
    with pytest.raises(ValidationError, match=r"depends on unknown VMs: \['dns'\]"):
        ProxmoxSandboxEnvironmentConfig(
            vms_config={"application": vm_config(depends_on=("dns",))}
        )


def test_dictionary_vms_config_rejects_duplicate_dependencies():
    with pytest.raises(ValidationError, match="contains duplicate dependencies"):
        ProxmoxSandboxEnvironmentConfig(
            vms_config={
                "database": vm_config(),
                "application": vm_config(depends_on=("database", "database")),
            }
        )


def test_dictionary_vms_config_rejects_cycles():
    with pytest.raises(ValidationError, match="dependency graph contains a cycle"):
        ProxmoxSandboxEnvironmentConfig(
            vms_config={
                "database": vm_config(depends_on=("application",)),
                "application": vm_config(depends_on=("database",)),
            }
        )


def test_dictionary_vms_config_accepts_dependency_graph():
    config = ProxmoxSandboxEnvironmentConfig(
        vms_config={
            "dns": vm_config(),
            "database": vm_config(),
            "application": vm_config(depends_on=("dns", "database")),
        }
    )

    assert isinstance(config.vms_config, dict)
    assert config.vms_config["application"].depends_on == ("dns", "database")


def test_dictionary_vms_config_is_hashable_for_inspect():
    config = ProxmoxSandboxEnvironmentConfig(
        vms_config={
            "database": vm_config(),
            "application": vm_config(depends_on=("database",)),
        }
    )
    equivalent = config.model_copy(deep=True)

    assert config == equivalent
    assert hash(config) == hash(equivalent)
    specs = {
        SandboxEnvironmentSpec("proxmox", config),
        SandboxEnvironmentSpec("proxmox", equivalent),
    }
    assert len(specs) == 1


def test_dictionary_vms_config_identity_preserves_declaration_order():
    database = vm_config()
    application = vm_config(depends_on=("database",))
    config = ProxmoxSandboxEnvironmentConfig(
        vms_config={"database": database, "application": application}
    )
    reordered = ProxmoxSandboxEnvironmentConfig(
        vms_config={"application": application, "database": database}
    )

    assert config != reordered
    assert len({config, reordered}) == 2
