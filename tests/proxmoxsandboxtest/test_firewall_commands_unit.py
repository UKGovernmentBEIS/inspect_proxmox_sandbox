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
    detect_caller_cidr,
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


def test_detect_caller_cidr_loopback_returns_none():
    """Loopback as the Proxmox host produces a useless /16, so we return None."""
    assert detect_caller_cidr("127.0.0.1") is None


def test_build_desired_management_cidrs_dedupes():
    """Caller's auto-detected CIDR and an identical extra are dedupe'd."""
    auto = detect_caller_cidr("1.1.1.1")
    assert auto is not None, "test requires outbound IPv4 connectivity"

    cfg = HostIsolation(extra_management_cidrs=(IPv4Network(auto),))
    out = FirewallCommands._build_desired_management_cidrs(cfg, "1.1.1.1")
    assert out == [auto]


def test_build_desired_management_cidrs_preserves_order():
    cfg = HostIsolation(
        extra_management_cidrs=(
            IPv4Network("10.0.0.0/16"),
            IPv4Network("203.0.113.0/24"),
        ),
    )
    out = FirewallCommands._build_desired_management_cidrs(cfg, "127.0.0.1")
    assert out == ["10.0.0.0/16", "203.0.113.0/24"]
