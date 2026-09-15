from ipaddress import ip_address

import pytest
from pydantic import ValidationError
from pydantic_extra_types.mac_address import MacAddress

from proxmoxsandbox.schema import (
    DependencyEdge,
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


# --- HealthCheck ---------------------------------------------------------------

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


# --- VmConfig.requires_guest_agent ----------------------------------------------


def test_requires_guest_agent_for_sandbox():
    assert _vm(is_sandbox=True).requires_guest_agent is True


def test_requires_guest_agent_for_healthcheck_on_non_sandbox():
    vm = _vm(is_sandbox=False, healthcheck=HealthCheck(test=("true",)))
    assert vm.requires_guest_agent is True


def test_requires_guest_agent_false_for_plain_non_sandbox():
    assert _vm(is_sandbox=False).requires_guest_agent is False


# --- VM names -------------------------------------------------------------------


def test_duplicate_vm_names_rejected():
    with pytest.raises(
        ValidationError,
        match=r"Duplicate VM name 'x' at vms_config\[0\] and vms_config\[1\]",
    ):
        _config(_vm("x"), _vm("x"))


def test_unnamed_vms_do_not_collide():
    cfg = _config(_vm(), _vm())
    assert len(cfg.vms_config) == 2


def test_vm_labels_fall_back_to_position():
    cfg = _config(_vm("a"), _vm())
    assert cfg.vm_labels() == ("'a'", "vms_config[1]")


# --- depends_on -----------------------------------------------------------------


def test_depends_on_unknown_name():
    with pytest.raises(
        ValidationError,
        match=r"'a' depends on unknown VM 'nope'\. Known names: \['a', 'b'\]",
    ):
        _config(_vm("a", depends_on=("nope",)), _vm("b"))


def test_depends_on_unknown_name_hints_about_unnamed_vms():
    with pytest.raises(
        ValidationError, match=r"1 VM\(s\) have no name and cannot be depended on"
    ):
        _config(_vm("a", depends_on=("nope",)), _vm())


def test_depends_on_duplicate_entry():
    with pytest.raises(ValidationError, match="'a' lists a duplicate dependency"):
        _config(_vm("a", depends_on=("b", "b")), _vm("b"))


def test_depends_on_self():
    with pytest.raises(ValidationError, match="'a' depends on itself"):
        _config(_vm("a", depends_on=("a",)))


def test_depends_on_forward_reference_is_legal():
    cfg = _config(_vm("a", depends_on=("b",)), _vm("b"))
    assert cfg.dependency_edges() == (DependencyEdge(0, 1, "depends_on"),)


def test_dependency_cycle_rejected():
    with pytest.raises(
        ValidationError,
        match="VM dependency cycle: 'a' waits for 'b' -> 'b' waits for 'a'",
    ):
        _config(_vm("a", depends_on=("b",)), _vm("b", depends_on=("a",)))


def test_dependency_cycle_through_await_before_next_vm_is_attributed():
    """An implied edge in a cycle must say where it came from."""
    with pytest.raises(
        ValidationError,
        match=r"\(implied by await_before_next_vm on 'a'\)",
    ):
        _config(_vm("a", await_before_next_vm=True, depends_on=("b",)), _vm("b"))


def test_await_before_next_vm_implies_edges_from_every_later_vm():
    cfg = _config(_vm("vm0", await_before_next_vm=True), _vm("vm1"), _vm("vm2"))
    assert set(cfg.dependency_edges()) == {
        DependencyEdge(1, 0, "await_before_next_vm"),
        DependencyEdge(2, 0, "await_before_next_vm"),
    }


def test_explicit_edge_wins_over_implied_on_dedupe():
    cfg = _config(
        _vm("vm0", await_before_next_vm=True),
        _vm("vm1", depends_on=("vm0",)),
        _vm("vm2"),
    )
    edges = {(e.dependant, e.dependency): e.origin for e in cfg.dependency_edges()}
    assert edges == {(1, 0): "depends_on", (2, 0): "await_before_next_vm"}


def test_no_dependencies_yields_no_edges():
    cfg = _config(_vm("a"), _vm("b"))
    assert cfg.dependency_edges() == ()


def test_dependency_edge_describe():
    labels = ("'a'", "'b'")
    assert DependencyEdge(0, 1, "depends_on").describe(labels) == "'a' waits for 'b'"
    assert (
        DependencyEdge(0, 1, "await_before_next_vm").describe(labels)
        == "'a' waits for 'b' (implied by await_before_next_vm on 'b')"
    )
