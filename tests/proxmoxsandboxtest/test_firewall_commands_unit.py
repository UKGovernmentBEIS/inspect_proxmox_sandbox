"""Pure-logic tests for FirewallCommands.

The conflict-detection logic is the place a regression silently leaves
hosts unprotected, so we exercise it here without standing up a Proxmox.
"""

from ipaddress import IPv4Network

import pytest

from proxmoxsandbox._impl.firewall_commands import (
    OURS_COMMENT,
    FirewallCommands,
    HostIsolationConflictError,
    ManagedRule,
)
from proxmoxsandbox.schema import HostIsolation


def test_managed_rule_equality_ignores_pos_and_digest():
    api_response = {
        "type": "in",
        "action": "ACCEPT",
        "proto": "udp",
        "dport": "53",
        "comment": OURS_COMMENT,
        "enable": 1,
        "log": "nolog",
        "pos": 0,
        "digest": "abc123",
        "ipversion": 4,
    }
    assert ManagedRule.from_api_response(api_response) == ManagedRule(
        type="in", action="ACCEPT", proto="udp", dport="53"
    )


def test_managed_rule_iface_roundtrips():
    """An interface-scoped rule survives a POST/GET round-trip by equality."""
    rule = ManagedRule(
        type="in", action="ACCEPT", proto="tcp", dport="8006", iface="enp39s0"
    )
    assert rule.to_proxmox_params()["iface"] == "enp39s0"
    assert ManagedRule.from_api_response(rule.to_proxmox_params() | {"pos": 4}) == rule


def test_managed_rule_omits_empty_iface_in_params():
    """An unscoped rule must not send iface='' (Proxmox rejects it)."""
    rule = ManagedRule(type="in", action="ACCEPT", proto="udp", dport="53")
    assert "iface" not in rule.to_proxmox_params()


def test_build_desired_rules_scopes_management_ports_to_iface():
    rules = FirewallCommands._build_desired_rules("enp39s0")
    by_dport = {r.dport: r for r in rules}
    # DNS/DHCP stay unscoped; management ports are pinned to the interface.
    assert by_dport["53"].iface == ""
    assert by_dport["67"].iface == ""
    assert by_dport["8006"].iface == "enp39s0"
    assert by_dport["22"].iface == "enp39s0"
    assert all(r.comment == OURS_COMMENT for r in rules)


def test_detect_conflict_foreign_rule_same_dport():
    """A foreign rule on a port we manage triggers a conflict."""
    rules = [
        {
            "type": "in",
            "action": "DROP",
            "proto": "udp",
            "dport": "53",
            "enable": 1,
        }
    ]
    with pytest.raises(HostIsolationConflictError, match="port we manage"):
        FirewallCommands._detect_node_rule_conflicts(rules)


def test_detect_conflict_foreign_drop_anywhere():
    """A foreign IN DROP/REJECT anywhere on the chain triggers a conflict."""
    rules = [
        {
            "type": "in",
            "action": "REJECT",
            "proto": "tcp",
            "dport": "22",
            "enable": 1,
        }
    ]
    with pytest.raises(HostIsolationConflictError, match="shadow"):
        FirewallCommands._detect_node_rule_conflicts(rules)


def test_detect_conflict_no_conflict_when_only_ours():
    rules = [
        {
            "type": "in",
            "action": "ACCEPT",
            "proto": "udp",
            "dport": "53",
            "comment": OURS_COMMENT,
            "enable": 1,
        },
        {
            "type": "in",
            "action": "ACCEPT",
            "proto": "udp",
            "dport": "67",
            "comment": OURS_COMMENT,
            "enable": 1,
        },
    ]
    FirewallCommands._detect_node_rule_conflicts(rules)  # no raise


def test_detect_conflict_no_conflict_on_unrelated_foreign_accept():
    """A foreign IN ACCEPT on an unrelated port is fine.

    Only foreign rules that *block* (DROP/REJECT) or *touch a port we
    manage* matter — a user's IN ACCEPT on port 9100 is none of our
    business.
    """
    rules = [
        {
            "type": "in",
            "action": "ACCEPT",
            "proto": "tcp",
            "dport": "9100",
            "enable": 1,
        }
    ]
    FirewallCommands._detect_node_rule_conflicts(rules)  # no raise


def test_detect_conflict_ignores_disabled_foreign_drop():
    """A disabled foreign IN DROP is not a real conflict."""
    rules = [
        {
            "type": "in",
            "action": "DROP",
            "proto": "tcp",
            "dport": "22",
            "enable": 0,
        }
    ]
    FirewallCommands._detect_node_rule_conflicts(rules)  # no raise


def test_build_desired_management_cidrs_empty_by_default():
    """No management_cidrs means nothing to add to the ipset."""
    assert FirewallCommands._build_desired_management_cidrs(HostIsolation()) == []


def test_build_desired_management_cidrs_dedupes():
    """Duplicate entries collapse to one."""
    cfg = HostIsolation(
        management_cidrs=(
            IPv4Network("10.0.0.0/16"),
            IPv4Network("10.0.0.0/16"),
        ),
    )
    out = FirewallCommands._build_desired_management_cidrs(cfg)
    assert out == ["10.0.0.0/16"]


def test_build_desired_management_cidrs_preserves_order():
    cfg = HostIsolation(
        management_cidrs=(
            IPv4Network("10.0.0.0/16"),
            IPv4Network("203.0.113.0/24"),
        ),
    )
    out = FirewallCommands._build_desired_management_cidrs(cfg)
    assert out == ["10.0.0.0/16", "203.0.113.0/24"]
