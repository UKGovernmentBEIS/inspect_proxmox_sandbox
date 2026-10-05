"""Isolated networking: guest NICs wired to per-vnet switch VMs over unix sockets.

No guest NIC is a Proxmox netX. Each one is a QEMU `-netdev dgram` passed via
the VM's `args`, wired point-to-point to a port on its vnet's switch VM through
a pair of unix datagram sockets on the host (one frame per datagram). Guest
frames therefore never reach a host netdev, bridge or IP stack. Inside each
switch VM the guest-facing ports are bridged, dnsmasq serves DHCP, and
point-to-point links to the other switch VMs carry routed traffic between vnets.
No switch VM has a default route, so anything outside the range's subnets is
unreachable.
"""

import re
import secrets
import shlex
from dataclasses import dataclass, field
from ipaddress import ip_network
from typing import Dict, List, Sequence, Tuple

from pydantic.networks import IPvAnyAddress
from pydantic_extra_types.mac_address import MacAddress

from proxmoxsandbox.schema import (
    ISOLATED_LINK_NETWORK,
    HealthCheck,
    ProxmoxSandboxEnvironmentConfig,
    SdnConfig,
    SubnetConfig,
    VmConfig,
    VmSourceConfig,
)

SWITCH_TEMPLATE_TAG = "builtin-isoswitch"
SWITCH_NAME_PREFIX = "isosw-"
# QEMU's dgram backend has no abstract-socket support, so these are real files.
# It unlinks a stale local path before bind but never on exit, so they
# accumulate (tmpfs, cleared at reboot) until something sweeps them.
_SOCKET_PREFIX = "/run/inspect-isonet-"
# Mirrors qemu-server's PCI slots for net0..net31 (PVE/QemuServer/PCI.pm); we
# have no netX devices, so those slots are free.
_MAX_PORTS_PER_VM = 32
_NIC_MODELS = {"virtio": "virtio-net-pci", "e1000": "e1000"}


def _pci_slot(index: int) -> str:
    if index < 6:
        return f"bus=pci.0,addr={hex(18 + index)}"
    return f"bus=pci.1,addr={hex(index - 5)}"


def _random_mac() -> str:
    # Locally administered unicast. QEMU would otherwise give every VM's
    # first NIC 52:54:00:12:34:56, which collides as soon as two share a vnet.
    return ":".join(f"{b:02x}" for b in bytes([0x02]) + secrets.token_bytes(5))


@dataclass(frozen=True)
class DgramPort:
    """One QEMU NIC: binds `local`, sends every frame to `remote`."""

    local: str
    remote: str
    mac: str
    model: str = "virtio-net-pci"

    def render(self, index: int) -> str:
        netdev = (
            f"dgram,id=iso{index},local.type=unix,local.path={self.local},"
            f"remote.type=unix,remote.path={self.remote}"
        )
        device = f"{self.model},netdev=iso{index},mac={self.mac},{_pci_slot(index)}"
        return f"-netdev {netdev} -device {device}"


def _wire(
    path_a: str, path_b: str, mac_a: str, mac_b: str, model_b: str
) -> Tuple[DgramPort, DgramPort]:
    return (
        DgramPort(local=path_a, remote=path_b, mac=mac_a),
        DgramPort(local=path_b, remote=path_a, mac=mac_b, model=model_b),
    )


def render_args(ports: Sequence[DgramPort]) -> str:
    if len(ports) > _MAX_PORTS_PER_VM:
        raise ValueError(
            f"{len(ports)} isolated NICs requested on one VM; max {_MAX_PORTS_PER_VM}"
        )
    return " ".join(port.render(i) for i, port in enumerate(ports))


@dataclass
class SwitchPlan:
    name: str
    subnet: SubnetConfig
    ports: List[DgramPort] = field(default_factory=list)
    # (port index, local /31 address, peer address)
    links: List[Tuple[int, str, str]] = field(default_factory=list)
    routes: List[Tuple[str, str]] = field(default_factory=list)
    dhcp_hosts: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def vm_config(self) -> VmConfig:
        return VmConfig(
            vm_source_config=VmSourceConfig(
                existing_vm_template_tag=SWITCH_TEMPLATE_TAG
            ),
            name=self.name,
            ram_mb=256,
            vcpus=1,
            is_sandbox=False,
            # Only so Proxmox enables the agent, which configure_script runs over
            healthcheck=HealthCheck(test=("true",)),
        )

    @property
    def args(self) -> str:
        return render_args(self.ports)

    def configure_script(self) -> str:
        """Shell script, run as root over QGA, that turns the VM into the switch."""
        network = ip_network(self.subnet.cidr)
        bridge_ports = [
            p.mac
            for i, p in enumerate(self.ports)
            if i not in {link[0] for link in self.links}
        ]
        q = shlex.quote
        lines = [
            "set -eu",
            # QGA exec retries can launch this twice.
            "exec 9>/run/isoswitch.lock && flock 9",
            "[ -e /run/isoswitch.done ] && exit 0",
            "dev_for_mac() { for d in /sys/class/net/*; do "
            '[ "$(cat "$d/address")" = "$1" ] && { echo "${d##*/}"; return 0; }; '
            'done; echo "no interface with MAC $1" >&2; return 1; }',
            # The template has no network config, so there is nothing to undo
            # here; refuse to continue if that ever stops being true.
            "if ip -4 route show default | grep -q .; then "
            'echo "switch VM unexpectedly has a default route" >&2; exit 1; fi',
            "sysctl -qw net.ipv4.ip_forward=1",
            "sysctl -qw net.ipv6.conf.all.disable_ipv6=1",
            "ip link add br0 type bridge forward_delay 0 stp_state 0",
        ]
        for mac in bridge_ports:
            lines.append(f'ip link set "$(dev_for_mac {q(mac)})" master br0 up')
        lines += [
            f"ip addr add {self.subnet.gateway}/{network.prefixlen} dev br0",
            "ip link set br0 up",
        ]
        for port_index, local, peer in self.links:
            mac = self.ports[port_index].mac
            lines += [
                f'dev="$(dev_for_mac {q(mac)})"',
                f'ip addr add {local}/31 dev "$dev"',
                'ip link set "$dev" up',
            ]
        for cidr, via in self.routes:
            lines.append(f"ip route add {cidr} via {via}")

        dnsmasq = [
            "dnsmasq",
            "--keep-in-foreground",
            "--interface=br0",
            "--bind-interfaces",
            # DHCP only: no DNS listener, and don't advertise one to clients.
            "--port=0",
            "--dhcp-option=option:dns-server",
            "--dhcp-authoritative",
            f"--dhcp-option=option:router,{self.subnet.gateway}",
        ]
        for r in self.subnet.dhcp_ranges:
            dnsmasq.append(f"--dhcp-range={r.start},{r.end},{network.netmask},12h")
        for mac, ip in self.dhcp_hosts:
            dnsmasq.append(f"--dhcp-host={mac},{ip}")
        # systemd-run so the QGA exec returns rather than waiting on the daemon.
        lines.append(
            "systemd-run --unit=isoswitch-dnsmasq --property=Restart=always "
            + " ".join(q(a) for a in dnsmasq)
        )
        lines.append("touch /run/isoswitch.done")
        return "\n".join(lines) + "\n"


@dataclass
class IsolatedNetworkPlan:
    switches: List[SwitchPlan]
    guest_ports: Dict[str, List[DgramPort]]

    def guest_args(self, vm_name: str) -> str:
        return render_args(self.guest_ports.get(vm_name, []))

    @staticmethod
    def expected_sockets(args: str) -> List[str]:
        """`local:remote` pairs, as `info network` reports them."""
        return [
            f"{local}:{remote}"
            for local, remote in re.findall(
                r"local\.path=([^,\s]+),remote\.type=unix,remote\.path=([^,\s]+)",
                args,
            )
        ]


def plan_isolated_network(
    config: ProxmoxSandboxEnvironmentConfig,
) -> IsolatedNetworkPlan:
    sdn_config = config.sdn_config
    if not isinstance(sdn_config, SdnConfig) or not sdn_config.isolated:
        raise ValueError("plan_isolated_network needs an isolated SdnConfig")

    # Per-sample token keeps socket paths unique on the host.
    token = secrets.token_hex(6)

    def paths(switch_index: int, port_index: int) -> Tuple[str, str]:
        base = f"{_SOCKET_PREFIX}{token}-{switch_index}-{port_index}"
        return f"{base}-a.sock", f"{base}-b.sock"

    switches: List[SwitchPlan] = []
    by_alias: Dict[str, int] = {}
    for i, vnet in enumerate(sdn_config.vnet_configs):
        switches.append(
            SwitchPlan(name=f"{SWITCH_NAME_PREFIX}{i}", subnet=vnet.subnets[0])
        )
        # SdnConfig guarantees isolated vnets have unique aliases.
        assert vnet.alias is not None
        by_alias[vnet.alias] = i

    clashes = {s.name for s in switches} & set(config.vm_names())
    if clashes:
        raise ValueError(f"VM names {sorted(clashes)} are reserved for switch VMs")

    guest_ports: Dict[str, List[DgramPort]] = {}
    for vm in config.vms_config:
        attachments: List[Tuple[int, MacAddress | None, IPvAnyAddress | None]] = []
        if vm.nics is None:
            # Same default as the SDN path: first vnet, if any.
            if switches:
                attachments.append((0, None, None))
        else:
            for nic in vm.nics:
                if nic.vnet_alias not in by_alias:
                    raise ValueError(
                        f"VM {vm.name!r}: vnet alias {nic.vnet_alias!r} is not in "
                        f"the isolated sdn_config (known: {sorted(by_alias)})"
                    )
                attachments.append((by_alias[nic.vnet_alias], nic.mac, nic.ipv4))

        model = _NIC_MODELS[vm.nic_controller or "virtio"]
        ports: List[DgramPort] = []
        for switch_index, mac, ipv4 in attachments:
            switch = switches[switch_index]
            mac_str = str(mac).lower() if mac else _random_mac()
            switch_end, guest_end = _wire(
                *paths(switch_index, len(switch.ports)), _random_mac(), mac_str, model
            )
            switch.ports.append(switch_end)
            ports.append(guest_end)
            if ipv4 is not None:
                if ipv4 not in ip_network(switch.subnet.cidr):
                    raise ValueError(
                        f"VM {vm.name!r}: {ipv4} is outside {switch.subnet.cidr}"
                    )
                switch.dhcp_hosts.append((mac_str, str(ipv4)))
        guest_ports[vm.name] = ports

    # Full mesh of /31 links between switch VMs.
    link_addresses = iter(ISOLATED_LINK_NETWORK)
    for a in range(len(switches)):
        for b in range(a + 1, len(switches)):
            addr_a, addr_b = next(link_addresses), next(link_addresses)
            sw_a, sw_b = switches[a], switches[b]
            end_a, end_b = _wire(
                *paths(a, len(sw_a.ports)),
                _random_mac(),
                _random_mac(),
                "virtio-net-pci",
            )
            sw_a.links.append((len(sw_a.ports), str(addr_a), str(addr_b)))
            sw_a.ports.append(end_a)
            sw_b.links.append((len(sw_b.ports), str(addr_b), str(addr_a)))
            sw_b.ports.append(end_b)
            sw_a.routes.append((str(sw_b.subnet.cidr), str(addr_b)))
            sw_b.routes.append((str(sw_a.subnet.cidr), str(addr_a)))

    return IsolatedNetworkPlan(switches=switches, guest_ports=guest_ports)


_INFO_NETWORK_NETDEV = re.compile(r"^\s*\\\s+(\S+):\s+index=\d+,type=(\w+),(.*)$")


def check_info_network(output: str, expected_sockets: Sequence[str]) -> None:
    """Fail unless every netdev QEMU reports is one of our unix dgram sockets.

    Parses HMP `info network`, whose format is not a stable interface.
    """
    seen: List[str] = []
    for line in output.splitlines():
        match = _INFO_NETWORK_NETDEV.match(line)
        if not match:
            continue
        netdev_id, kind, rest = match.groups()
        if kind != "dgram" or not rest.startswith("udp=/run/"):
            raise RuntimeError(f"unexpected {kind} netdev {netdev_id}: {line.strip()}")
        sock = rest.strip().removeprefix("udp=")
        if sock not in expected_sockets:
            raise RuntimeError(f"netdev {netdev_id} on unexpected socket {sock!r}")
        seen.append(sock)
    if sorted(seen) != sorted(expected_sockets):
        raise RuntimeError(
            f"QEMU netdevs {sorted(seen)} do not match "
            f"expected {sorted(expected_sockets)}"
        )
