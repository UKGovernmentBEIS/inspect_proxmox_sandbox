from ipaddress import ip_address, ip_network

import pytest

from proxmoxsandbox._impl.isolated_network import (
    IsolatedNetworkPlan,
    check_info_network,
    plan_isolated_network,
)
from proxmoxsandbox.schema import (
    DhcpRange,
    ProxmoxSandboxEnvironmentConfig,
    SdnConfig,
    SubnetConfig,
    VmConfig,
    VmNicConfig,
    VmSourceConfig,
    VnetConfig,
)


def _vnet(alias: str, n: int, snat: bool = False) -> VnetConfig:
    return VnetConfig(
        alias=alias,
        subnets=(
            SubnetConfig(
                cidr=ip_network(f"10.{n}.0.0/24"),
                gateway=ip_address(f"10.{n}.0.1"),
                snat=snat,
                dhcp_ranges=(
                    DhcpRange(
                        start=ip_address(f"10.{n}.0.50"),
                        end=ip_address(f"10.{n}.0.99"),
                    ),
                ),
            ),
        ),
    )


def _vm(name: str, *aliases: str, **kwargs) -> VmConfig:
    return VmConfig(
        vm_source_config=VmSourceConfig(built_in="debian13"),
        name=name,
        nics=tuple(VmNicConfig(vnet_alias=a) for a in aliases),
        **kwargs,
    )


def _plan(*vms: VmConfig, vnets=("a", "b")) -> IsolatedNetworkPlan:
    return plan_isolated_network(
        ProxmoxSandboxEnvironmentConfig(
            vms_config=vms,
            sdn_config=SdnConfig(
                isolated=True,
                vnet_configs=tuple(_vnet(v, i + 1) for i, v in enumerate(vnets)),
            ),
        )
    )


def _wires(args: str) -> set[tuple[str, str]]:
    return {
        tuple(pair.split(":"))  # type: ignore[misc]
        for pair in IsolatedNetworkPlan.expected_sockets(args)
    }


def test_every_guest_port_is_cross_wired_to_its_switch() -> None:
    plan = _plan(_vm("default", "a"), _vm("v2", "a", "b"))
    switch_wires = set().union(*(_wires(s.args) for s in plan.switches))
    for name in ("default", "v2"):
        for local, remote in _wires(plan.guest_args(name)):
            assert (remote, local) in switch_wires


def test_switches_are_meshed_with_routes_both_ways() -> None:
    plan = _plan(_vm("default", "a"), vnets=("a", "b", "c"))
    for switch in plan.switches:
        assert len(switch.links) == 2
        assert {cidr for cidr, _ in switch.routes} == {
            str(s.subnet.cidr) for s in plan.switches if s is not switch
        }
    a, b = plan.switches[0], plan.switches[1]
    assert (_wires(a.args) & {(r, lo) for lo, r in _wires(b.args)}) != set()


def test_guest_macs_are_unique_without_explicit_macs() -> None:
    plan = _plan(_vm("default", "a"), *(_vm(f"v{i}", "a") for i in range(5)))
    macs = [p.mac for ports in plan.guest_ports.values() for p in ports]
    assert len(set(macs)) == len(macs)


def test_static_ipv4_becomes_dhcp_host_on_the_right_switch() -> None:
    vm = VmConfig(
        vm_source_config=VmSourceConfig(built_in="debian13"),
        nics=(
            VmNicConfig(
                vnet_alias="b", mac="02:aa:bb:cc:dd:ee", ipv4=ip_address("10.2.0.10")
            ),
        ),
    )
    plan = _plan(vm)
    assert plan.switches[1].dhcp_hosts == [("02:aa:bb:cc:dd:ee", "10.2.0.10")]
    assert "--dhcp-host=02:aa:bb:cc:dd:ee,10.2.0.10" in (
        plan.switches[1].configure_script()
    )


def test_unknown_alias_is_rejected() -> None:
    with pytest.raises(ValueError, match="not in the isolated sdn_config"):
        _plan(_vm("default", "nope"))


def test_snat_is_rejected_when_isolated() -> None:
    with pytest.raises(ValueError, match="snat=True"):
        SdnConfig(isolated=True, vnet_configs=(_vnet("a", 1, snat=True),))


def test_subnet_overlapping_link_network_is_rejected() -> None:
    vnet = VnetConfig(
        alias="a",
        subnets=(
            SubnetConfig(
                cidr=ip_network("100.64.1.0/24"),
                gateway=ip_address("100.64.1.1"),
                snat=False,
                dhcp_ranges=(),
            ),
        ),
    )
    with pytest.raises(ValueError, match="reserved for links"):
        SdnConfig(isolated=True, vnet_configs=(vnet,))


_PAIR = "/run/inspect-isonet-x-0-0-a.sock:/run/inspect-isonet-x-0-0-b.sock"


def test_info_network_accepts_exactly_the_expected_dgram() -> None:
    check_info_network(
        "virtio-net-pci.0: index=0,type=nic,model=virtio-net-pci,"
        "macaddr=02:00:00:00:00:01\n"
        f" \\ iso0: index=0,type=dgram,udp={_PAIR}\n",
        [_PAIR],
    )


@pytest.mark.parametrize(
    "netdev",
    [
        " \\ net0: index=0,type=tap,ifname=tap100i0,script=/usr/libexec/pve-bridge",
        " \\ user0: index=0,type=user,net=10.0.2.0,restrict=off",
        " \\ iso0: index=0,type=dgram,udp=127.0.0.1:1234/127.0.0.1:1235",
    ],
)
def test_info_network_rejects_other_netdevs(netdev: str) -> None:
    with pytest.raises(RuntimeError):
        check_info_network(
            f" \\ iso0: index=0,type=dgram,udp={_PAIR}\n{netdev}\n", [_PAIR]
        )


def test_info_network_rejects_missing_netdev() -> None:
    with pytest.raises(RuntimeError, match="do not match"):
        check_info_network("", [_PAIR])
