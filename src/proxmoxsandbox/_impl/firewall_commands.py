"""Host-firewall isolation for the Proxmox sandbox.

Sandbox VMs would otherwise be able to reach pveproxy (port 8006), SSH, and
other host services via the host's SDN gateway IP, vmbr0 IP, or VPC ENI.
This module enables Proxmox's own cluster + node firewall via the API and
posts a small set of allow rules.

Isolation is by *interface*, not by source IP: the host management ports
(8006, 22) are accepted only on the host's management interface, where
external API/SSH callers arrive. Sandbox VMs reach the host over the SDN /
vmbr0 bridges, so their packets ingress on a different interface and hit the
default-deny INPUT policy — even when they aim at the host's management IP
directly. DNS/DHCP from VMs is accepted on any interface so the SDN dnsmasq
keeps working.

This assumes the management interface does NOT also bridge sandbox VMs (true
for a dedicated management NIC, or an SDN-based setup where guests sit on
their own bridges). If the host's management IP lives on the same bridge VMs
attach to, interface scoping cannot tell a caller from a VM; set
``HostIsolation.management_cidrs`` for a source-IP allowlist instead, or pin
``management_interface`` to the right device.

Unlike SdnCommands or QemuCommands, FirewallCommands has no register_*,
deregister_*, or task_cleanup methods: the firewall config is intentionally
persistent across runs, not a per-sample ephemeral resource.
"""

import abc
from ipaddress import ip_network
from logging import getLogger
from typing import List, Tuple

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


class HostIsolationConfigError(RuntimeError):
    """Raised when the host's management interface can't be determined.

    Auto-detection picks the single active physical interface. A host with
    no physical interface in the API view, or more than one, is ambiguous —
    the caller must set ``host_isolation.management_interface`` to the
    interface external API/SSH traffic arrives on.
    """


class ManagedRule(BaseModel, frozen=True):
    """A single host firewall rule the framework owns.

    The full Proxmox API rule object has additional fields like ``pos`` and
    ``digest`` that change between reads; equality here only compares the
    fields we actually set, so a re-read of a previously-POSTed rule
    matches the constant we built it from.
    """

    type: str
    action: str
    proto: str
    dport: str
    iface: str = ""
    comment: str = OURS_COMMENT
    enable: int = 1
    log: str = "nolog"

    def to_proxmox_params(self) -> dict:
        params = self.model_dump()
        # Proxmox rejects an empty iface; only send it when set.
        if not params.get("iface"):
            params.pop("iface", None)
        return params

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
            iface=rule.get("iface", ""),
            comment=rule.get("comment", ""),
            enable=int(rule.get("enable", 1)),
            log=rule.get("log", "nolog"),
        )


class FirewallCommands(abc.ABC):
    """Configure the Proxmox cluster and host firewall idempotently.

    Single public entrypoint: ``ensure_host_isolation``. The conflict check
    and all writes happen while the firewall is still disabled, and the
    enables are flipped last, so the host is never half-mutated and the API
    caller is never locked out mid-apply.
    """

    logger = getLogger(__name__)
    TRACE_NAME = "proxmox_firewall_command"

    # DNS + DHCP intake for the SDN dnsmasq, accepted on any interface so a
    # VM can resolve / lease regardless of which bridge it sits on. The
    # cluster firewall's default-deny INPUT for the host would otherwise
    # block them.
    MANAGED_DNS_RULES: Tuple[ManagedRule, ...] = (
        ManagedRule(type="in", action="ACCEPT", proto="udp", dport="53"),
        ManagedRule(type="in", action="ACCEPT", proto="tcp", dport="53"),
        ManagedRule(type="in", action="ACCEPT", proto="udp", dport="67"),
    )

    # Host management ports, accepted only on the management interface.
    MANAGEMENT_PORTS: Tuple[str, ...] = ("8006", "22")

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

    async def ensure_host_isolation(self, cfg: HostIsolation) -> None:
        if not cfg.enabled:
            self.logger.debug("host_isolation disabled; skipping firewall setup")
            return
        if self._applied:
            return

        iface = cfg.management_interface or await self._detect_management_interface()
        desired_cidrs = self._build_desired_management_cidrs(cfg)
        desired_rules = self._build_desired_rules(iface)

        with trace_action(self.logger, self.TRACE_NAME, "ensure host isolation"):
            existing_rules = await self.async_proxmox.request(
                "GET", f"/nodes/{self.node}/firewall/rules"
            )
            # Raise on any conflict BEFORE we write anything.
            self._detect_node_rule_conflicts(existing_rules)

            # Stage the ipset and rules while the firewall is still disabled,
            # then flip the enables last. The management-interface ACCEPTs are
            # in place before the policy goes default-deny, so an off-subnet
            # API caller (arriving on the management interface) is never
            # locked out mid-apply.
            await self._ensure_management_ipset(desired_cidrs)
            await self._ensure_node_rules(desired_rules, existing_rules)
            await self._ensure_cluster_fw_enabled()
            await self._ensure_node_fw_enabled()

        self._applied = True

    @classmethod
    def _build_desired_rules(cls, iface: str) -> Tuple[ManagedRule, ...]:
        mgmt = tuple(
            ManagedRule(
                type="in", action="ACCEPT", proto="tcp", dport=port, iface=iface
            )
            for port in cls.MANAGEMENT_PORTS
        )
        return cls.MANAGED_DNS_RULES + mgmt

    async def _detect_management_interface(self) -> str:
        """The interface external API/SSH callers arrive on.

        We use the single active physical interface (``eth`` / ``bond``).
        Matching by the host's management IP is deliberately avoided: a
        DHCP-configured NIC doesn't expose its address in the network config,
        and address matching would otherwise happily return a *bridge* — the
        one case where interface scoping fails open, since VMs attach to that
        same bridge. Anything ambiguous raises.
        """
        ifaces = await self.async_proxmox.request("GET", f"/nodes/{self.node}/network")
        physical = [
            i
            for i in ifaces
            if i.get("type") in ("eth", "bond") and int(i.get("active", 0)) == 1
        ]
        if len(physical) == 1:
            return str(physical[0]["iface"])

        names = sorted(str(i.get("iface", "?")) for i in ifaces)
        raise HostIsolationConfigError(
            "Could not determine the Proxmox management interface "
            f"automatically: found {len(physical)} active physical interfaces "
            f"among {names}. Set host_isolation.management_interface to the "
            "interface external API/SSH traffic arrives on."
        )

    @staticmethod
    def _build_desired_management_cidrs(cfg: HostIsolation) -> List[str]:
        # Dedupe while preserving order. The management interface ACCEPTs
        # cover the normal off-subnet caller; these are an optional source-IP
        # allowlist for cases interface scoping can't serve (e.g. the mgmt IP
        # shares a bridge with VMs). Empty is the common case.
        seen: set[str] = set()
        out: List[str] = []
        for net in cfg.management_cidrs:
            cidr = str(net)
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

    async def _ensure_node_rules(
        self, desired_rules: Tuple[ManagedRule, ...], existing_rules: List[dict]
    ) -> None:
        existing_managed = {
            ManagedRule.from_api_response(r)
            for r in existing_rules
            if r.get("comment") == OURS_COMMENT
        }

        for rule in desired_rules:
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
          1. A foreign rule on the same (type, proto, dport) as one of our
             unscoped DNS/DHCP ACCEPTs — someone else is already managing
             that intake; we don't know whether their intent matches ours.
          2. A foreign IN DROP or IN REJECT rule anywhere on the chain —
             we can't be confident our ACCEPTs are reachable.
        """
        managed_keys = {(r.type, r.proto, r.dport) for r in cls.MANAGED_DNS_RULES}

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
            for r in cls.MANAGED_DNS_RULES
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
