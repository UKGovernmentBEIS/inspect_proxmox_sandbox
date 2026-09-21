from ipaddress import ip_address

import pytest
from pydantic import ValidationError
from pydantic_extra_types.mac_address import MacAddress

from proxmoxsandbox.schema import (
    HealthCheck,
    ProxmoxSandboxEnvironmentConfig,
    VmConfig,
    VmNicConfig,
    VmSourceConfig,
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


_SOURCE = VmSourceConfig(built_in="ubuntu24.04")


def _vm(name: str | None = None, **kwargs) -> VmConfig:
    return VmConfig(vm_source_config=_SOURCE, name=name, **kwargs)


def _config(*vms: VmConfig) -> ProxmoxSandboxEnvironmentConfig:
    return ProxmoxSandboxEnvironmentConfig(vms_config=vms)


def test_healthcheck_defaults_follow_compose_names_not_values():
    hc = HealthCheck(test=("true",))
    assert hc.interval == 5
    assert hc.timeout == 30
    assert hc.retries == 60
    assert hc.start_period == 0


def test_healthcheck_test_must_be_nonempty():
    with pytest.raises(ValidationError):
        HealthCheck(test=())


def test_healthcheck_test_needs_executable():
    with pytest.raises(ValidationError, match="test needs an executable"):
        HealthCheck(test=("", "arg"))


def test_healthcheck_test_rejects_nul():
    with pytest.raises(ValidationError, match="must not contain NUL"):
        HealthCheck(test=("sh", "-c", "echo\x00hi"))


def test_healthcheck_interval_must_be_positive():
    with pytest.raises(ValidationError):
        HealthCheck(test=("true",), interval=0)


def test_healthcheck_rejects_unknown_fields():
    """Typos in a healthcheck should fail loudly, not silently no-op."""
    with pytest.raises(ValidationError):
        HealthCheck.model_validate({"test": ["true"], "intervall": 5})


def test_requires_guest_agent_for_sandbox():
    assert _vm(is_sandbox=True).requires_guest_agent is True


def test_requires_guest_agent_for_healthcheck_on_non_sandbox():
    vm = _vm(is_sandbox=False, healthcheck=HealthCheck(test=("true",)))
    assert vm.requires_guest_agent is True


def test_requires_guest_agent_false_for_plain_non_sandbox():
    assert _vm(is_sandbox=False).requires_guest_agent is False


def test_duplicate_vm_names_rejected():
    with pytest.raises(
        ValidationError,
        match=r"Duplicate VM name 'x' at vms_config\[0\] and vms_config\[1\]",
    ):
        _config(_vm("x"), _vm("x"))


def test_unnamed_first_sandbox_vm_is_named_default():
    cfg = _config(_vm())
    assert cfg.vm_names() == ("default",)
    cfg = _config(_vm("router", is_sandbox=False), _vm(), _vm("web"))
    assert cfg.vm_names() == ("router", "default", "web")


def test_default_config_vm_is_named_default():
    assert ProxmoxSandboxEnvironmentConfig().vm_names() == ("default",)


def test_unnamed_vm_named_default_from_dict_input():
    cfg = ProxmoxSandboxEnvironmentConfig(
        vms_config=[{"vm_source_config": {"built_in": "ubuntu24.04"}}]
    )
    assert cfg.vm_names() == ("default",)


def test_second_unnamed_vm_rejected():
    with pytest.raises(
        ValidationError, match=r"vms_config\[1\] is named 'default' \(the default"
    ):
        _config(_vm(), _vm())


def test_unnamed_non_sandbox_vm_rejected_even_before_the_default():
    with pytest.raises(
        ValidationError, match=r"vms_config\[0\] is named 'default' \(the default"
    ):
        _config(_vm(is_sandbox=False), _vm("web"))


def test_explicit_none_name_means_default():
    assert _vm(None).name == "default"


def test_no_sandbox_vm_rejected():
    with pytest.raises(ValidationError, match="No default sandbox found"):
        _config(_vm("a", is_sandbox=False), _vm("b", is_sandbox=False))


def test_unnamed_first_sandbox_with_default_taken_elsewhere_is_reserved_error():
    with pytest.raises(ValidationError, match="reserved for the first is_sandbox"):
        _config(_vm(), _vm("default"))


def test_depends_on_empty_name_is_a_validation_error():
    with pytest.raises(ValidationError, match="unknown VM ''"):
        _config(_vm("a", depends_on=("",)), _vm("b"))


def test_default_name_allowed_on_first_sandbox_vm():
    cfg = _config(_vm("default"), _vm("web"))
    assert cfg.vms_config[0].name == "default"
    cfg = _config(_vm("router", is_sandbox=False), _vm("default"), _vm("web"))
    assert cfg.vms_config[1].name == "default"


@pytest.mark.parametrize(
    "vms",
    [
        (_vm("web"), _vm("default", is_sandbox=False)),
        (_vm("default", is_sandbox=False), _vm("web")),
        (_vm("web"), _vm("default")),
    ],
    ids=["non-sandbox-after", "non-sandbox-before", "second-sandbox"],
)
def test_default_name_reserved_for_first_sandbox_vm(vms):
    with pytest.raises(ValidationError, match="reserved for the first is_sandbox"):
        _config(*vms)


def test_depends_on_unknown_name():
    with pytest.raises(
        ValidationError,
        match=r"'a' depends on unknown VM 'nope'\. Known names: \['a', 'b'\]",
    ):
        _config(_vm("a", depends_on=("nope",)), _vm("b"))


def test_depends_on_default_vm_by_its_implicit_name():
    cfg = _config(_vm(), _vm("web", depends_on=("default",)))
    assert cfg.vm_names() == ("default", "web")


def test_depends_on_duplicate_entry():
    with pytest.raises(ValidationError, match="'a' lists a duplicate dependency"):
        _config(_vm("a", depends_on=("b", "b")), _vm("b"))


def test_depends_on_self():
    with pytest.raises(ValidationError, match="'a' depends on itself"):
        _config(_vm("a", depends_on=("a",)))


def test_depends_on_forward_reference_is_legal():
    _config(_vm("a", depends_on=("b",)), _vm("b"))


def test_dependency_cycle_rejected():
    with pytest.raises(
        ValidationError,
        match="VM dependency cycle: 'a' -> 'b' -> 'a'",
    ):
        _config(_vm("a", depends_on=("b",)), _vm("b", depends_on=("a",)))


@pytest.mark.parametrize(
    "name",
    ["romeo", "web-1", "Kali2025", "a.b.c", "x", "0pointer", "x" * 100],
)
def test_vmconfig_name_valid_dns_name(name):
    vm = VmConfig(vm_source_config=VmSourceConfig(built_in="ubuntu24.04"), name=name)
    assert vm.name == name


@pytest.mark.parametrize(
    "name",
    ["bad_name", "has space", "", "-leading", "trailing-", "a..b", "a.", ".a", "é"],
)
def test_vmconfig_name_invalid_dns_name(name):
    with pytest.raises(ValidationError, match="is not a valid DNS name"):
        VmConfig(vm_source_config=VmSourceConfig(built_in="ubuntu24.04"), name=name)
