"""Host-firewall isolation for the Proxmox sandbox.

Sandbox VMs would otherwise be able to reach pveproxy (port 8006), SSH, and
other host services via the host's SDN gateway IP, vmbr0 IP, or VPC ENI.
This module enables Proxmox's own cluster + node firewall via the API and
posts a small set of allow rules so DNS/DHCP from VMs keeps working.

Unlike SdnCommands or QemuCommands, FirewallCommands has no register_*,
deregister_*, or task_cleanup methods: the firewall config is intentionally
persistent across runs, not a per-sample ephemeral resource.
"""

import abc
import socket
from ipaddress import (
    IPv4Address,
    ip_address,
    ip_network,
)
from logging import getLogger
from typing import List, Optional, Tuple

from inspect_ai.util import trace_action
from pydantic import BaseModel

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.task_wrapper import TaskWrapper
from proxmoxsandbox.schema import HostIsolation

OURS_COMMENT = "inspect-proxmox-sandbox: host-isolation"


class HostIsolationConflictError(RuntimeError):
    """Raised when conflicting firewall config exists on the Proxmox host.

    The framework only modifies rules and ipset entries it tagged with
    ``OURS_COMMENT``; any foreign rule that would shadow our managed
    ACCEPTs (e.g. an existing IN DROP / REJECT on the same dport) blocks
    the apply. The caller must either remove the conflicting rules or set
    ``host_isolation.enabled=False`` (not recommended).
    """


class ManagedRule(BaseModel, frozen=True):
    """A single host firewall rule the framework owns.

    The full Proxmox API rule object has additional fields like ``pos`` and
    ``digest`` that change between reads; equality here only compares the
    fields we actually set, so a re-read of a previously-POSTed rule
    matches the constant in ``FirewallCommands.MANAGED_RULES``.
    """

    type: str
    action: str
    proto: str
    dport: str
    comment: str = OURS_COMMENT
    enable: int = 1
    log: str = "nolog"

    def to_proxmox_params(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_api_response(cls, rule: dict) -> "ManagedRule":
        # API rules may carry extra fields (pos, digest, ipversion, ...).
        # Match by the subset we care about; missing optional fields fall
        # back to the model defaults.
        return cls(
            type=rule["type"],
            action=rule["action"],
            proto=rule.get("proto", ""),
            dport=rule.get("dport", ""),
            comment=rule.get("comment", ""),
            enable=int(rule.get("enable", 1)),
            log=rule.get("log", "nolog"),
        )


def detect_caller_cidr(host: str, port: int = 8006) -> Optional[str]:
    """Return the local IP /16 used to reach ``host:port``, or None.

    Used to widen Proxmox's auto-detected ``management`` ipset so the
    framework's own API calls keep working when the API caller is on a
    different VPC subnet than the Proxmox host (the common case on AWS,
    where dev VMs and Proxmox hosts share a VPC but sit on different /20s).

    UDP ``connect`` triggers kernel route selection without putting any
    packets on the wire. Returns None for loopback / link-local sources,
    or on any socket error — the eval should still proceed because
    Proxmox's own auto-detected /20 is always in the ipset.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, port))
            local_ip = s.getsockname()[0]
    except OSError:
        return None

    try:
        addr = ip_address(local_ip)
    except ValueError:
        return None

    if not isinstance(addr, IPv4Address):
        return None
    if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
        return None

    return str(ip_network(f"{local_ip}/16", strict=False))


class FirewallCommands(abc.ABC):
    """Configure the Proxmox cluster and host firewall idempotently.

    Single public entrypoint: ``ensure_host_isolation``. All conflict checks
    happen before any writes, so a conflicted host is never half-mutated.
    """

    logger = getLogger(__name__)
    TRACE_NAME = "proxmox_firewall_command"

    # The three rules we always want on the node firewall — VMs need
    # DNS and DHCP from the SDN dnsmasq bound to bridge gateway IPs, but
    # the cluster firewall's default-deny INPUT for the host would otherwise
    # block them.
    MANAGED_RULES: Tuple[ManagedRule, ...] = (
        ManagedRule(type="in", action="ACCEPT", proto="udp", dport="53"),
        ManagedRule(type="in", action="ACCEPT", proto="tcp", dport="53"),
        ManagedRule(type="in", action="ACCEPT", proto="udp", dport="67"),
    )

    async_proxmox: AsyncProxmoxAPI
    task_wrapper: TaskWrapper
    node: str
    _applied: bool

    def __init__(
        self, async_proxmox: AsyncProxmoxAPI, task_wrapper: TaskWrapper, node: str
    ):
        self.async_proxmox = async_proxmox
        self.task_wrapper = task_wrapper
        self.node = node
        self._applied = False

    async def ensure_host_isolation(self, cfg: HostIsolation, caller_host: str) -> None:
        if not cfg.enabled:
            self.logger.debug("host_isolation disabled; skipping firewall setup")
            return
        if self._applied:
            return

        desired_cidrs = self._build_desired_management_cidrs(cfg, caller_host)

        with trace_action(self.logger, self.TRACE_NAME, "ensure host isolation"):
            # Order matters: populate the management ipset BEFORE enabling
            # the cluster firewall. Proxmox auto-populates the ipset with
            # the host's own /20, but the framework is usually on a
            # different /20 of the VPC; enabling the firewall first would
            # lock the next API call (the ipset POST) out and time out.
            await self._ensure_management_ipset(desired_cidrs)
            await self._ensure_cluster_fw_enabled()
            await self._ensure_node_fw_enabled()
            await self._ensure_node_rules()

        self._applied = True

    @staticmethod
    def _build_desired_management_cidrs(
        cfg: HostIsolation, caller_host: str
    ) -> List[str]:
        cidrs: List[str] = []
        auto = detect_caller_cidr(caller_host)
        if auto is not None:
            cidrs.append(auto)
        for net in cfg.extra_management_cidrs:
            cidrs.append(str(net))

        # Dedupe while preserving order.
        seen: set[str] = set()
        out: List[str] = []
        for cidr in cidrs:
            if cidr not in seen:
                seen.add(cidr)
                out.append(cidr)
        return out

    async def _ensure_cluster_fw_enabled(self) -> None:
        opts = await self.async_proxmox.request("GET", "/cluster/firewall/options")
        if int(opts.get("enable", 0)) == 1:
            return
        await self.async_proxmox.request(
            "PUT", "/cluster/firewall/options", json={"enable": 1}
        )

    async def _ensure_management_ipset(self, desired_cidrs: List[str]) -> None:
        if not desired_cidrs:
            return

        ipsets = await self.async_proxmox.request("GET", "/cluster/firewall/ipset")
        if not any(s.get("name") == "management" for s in ipsets):
            await self.async_proxmox.request(
                "POST", "/cluster/firewall/ipset", json={"name": "management"}
            )

        existing = await self.async_proxmox.request(
            "GET", "/cluster/firewall/ipset/management"
        )
        existing_cidrs = {self._normalise_cidr(e["cidr"]) for e in existing}

        for cidr in desired_cidrs:
            if self._normalise_cidr(cidr) in existing_cidrs:
                continue
            await self.async_proxmox.request(
                "POST",
                "/cluster/firewall/ipset/management",
                json={"cidr": cidr, "comment": OURS_COMMENT},
            )

    @staticmethod
    def _normalise_cidr(s: str) -> str:
        # Proxmox stores CIDRs as the user provided them ("10.10.10.0/24"
        # vs "10.10.10.1/24"). Canonicalise to the network address so
        # equality checks don't miss equivalents.
        try:
            return str(ip_network(s, strict=False))
        except ValueError:
            return s

    async def _ensure_node_fw_enabled(self) -> None:
        opts = await self.async_proxmox.request(
            "GET", f"/nodes/{self.node}/firewall/options"
        )
        if int(opts.get("enable", 0)) == 1:
            return
        await self.async_proxmox.request(
            "PUT",
            f"/nodes/{self.node}/firewall/options",
            json={"enable": 1},
        )

    async def _ensure_node_rules(self) -> None:
        existing = await self.async_proxmox.request(
            "GET", f"/nodes/{self.node}/firewall/rules"
        )

        # Raise on any conflict BEFORE we POST anything.
        self._detect_node_rule_conflicts(existing)

        existing_managed = {
            ManagedRule.from_api_response(r)
            for r in existing
            if r.get("comment") == OURS_COMMENT
        }

        for rule in self.MANAGED_RULES:
            if rule in existing_managed:
                continue
            await self.async_proxmox.request(
                "POST",
                f"/nodes/{self.node}/firewall/rules",
                json=rule.to_proxmox_params(),
            )

    @classmethod
    def _detect_node_rule_conflicts(cls, existing_rules: List[dict]) -> None:
        """Raise on foreign rules that would prevent our ACCEPTs from working.

        Two cases trip this:
          1. A foreign rule on the same (type, proto, dport) as one of ours
             — someone else is already managing DNS/DHCP intake; we don't
             know whether their intent matches ours.
          2. A foreign IN DROP or IN REJECT rule anywhere on the chain —
             we can't be confident our ACCEPTs are reachable.
        """
        managed_keys = {(r.type, r.proto, r.dport) for r in cls.MANAGED_RULES}

        foreign_collisions: List[dict] = []
        foreign_blockers: List[dict] = []

        for r in existing_rules:
            if r.get("comment") == OURS_COMMENT:
                continue
            key = (
                r.get("type", ""),
                r.get("proto", ""),
                r.get("dport", ""),
            )
            if key in managed_keys:
                foreign_collisions.append(r)
                continue
            if (
                r.get("type") == "in"
                and r.get("action") in ("DROP", "REJECT")
                and int(r.get("enable", 0)) == 1
            ):
                foreign_blockers.append(r)

        if not foreign_collisions and not foreign_blockers:
            return

        def _fmt(r: dict) -> str:
            return (
                f"  {r.get('type', '?').upper()} {r.get('action', '?')} "
                f"-p {r.get('proto', '?')} -dport {r.get('dport', '?')} "
                f"comment={r.get('comment', '(none)')!r}"
            )

        lines = [
            "Host already has Proxmox firewall config that "
            "inspect-proxmox-sandbox cannot safely coexist with.",
            "",
        ]
        if foreign_collisions:
            lines.append("Foreign rule(s) on a port we manage:")
            lines.extend(_fmt(r) for r in foreign_collisions)
            lines.append("")
        if foreign_blockers:
            lines.append("Foreign IN DROP/REJECT rule(s) that may shadow our ACCEPTs:")
            lines.extend(_fmt(r) for r in foreign_blockers)
            lines.append("")
        lines.append("Expected (managed by inspect-proxmox-sandbox):")
        lines.extend(
            f"  IN ACCEPT -p {r.proto} -dport {r.dport} comment={r.comment!r}"
            for r in cls.MANAGED_RULES
        )
        lines.extend(
            [
                "",
                "To resolve:",
                "  - manually remove the conflicting rule(s) on this Proxmox node "
                "and re-run, or",
                "  - set host_isolation.enabled=False in "
                "ProxmoxSandboxEnvironmentConfig (NOT RECOMMENDED — sandbox VMs "
                "will be able to reach pveproxy and SSH on the host).",
            ]
        )
        raise HostIsolationConflictError("\n".join(lines))
