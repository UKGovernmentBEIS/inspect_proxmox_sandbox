from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
EC2_SCRIPTS = REPO_ROOT / "src" / "proxmoxsandbox" / "scripts" / "ec2"
VIRTUALIZED_SCRIPT = (
    REPO_ROOT
    / "src"
    / "proxmoxsandbox"
    / "scripts"
    / "virtualized_proxmox"
    / "build_proxmox_auto.sh"
)


def test_launch_contains_expected_imds_settings() -> None:
    launch = (EC2_SCRIPTS / "launch.sh").read_text()

    assert (
        "HttpTokens=required,HttpPutResponseHopLimit=1,"
        "HttpProtocolIpv6=disabled,InstanceMetadataTags=enabled"
    ) in launch


@pytest.mark.parametrize(
    "provisioner",
    [
        EC2_SCRIPTS / "userdata.sh",
        VIRTUALIZED_SCRIPT,
    ],
)
def test_provisioners_contain_expected_iptables_rules(provisioner: Path) -> None:
    # Both provisioners enforce RFC 3927 section 7 (a router must not forward IPv4
    # link-local), rather than denylisting one cloud's metadata IP, and treat
    # IPv6 as unsupported for sandbox guests. The two blocks are kept identical.
    script = provisioner.read_text()

    # Destination drop in raw PREROUTING (host requests are OUTPUT, unaffected).
    assert "iptables -w -t raw -I PREROUTING 1 -d 169.254.0.0/16 -j DROP" in script
    # Source drop in FORWARD, not PREROUTING, so the host's own link-local
    # replies (INPUT) are left intact.
    assert "iptables -w -I FORWARD 1 -s 169.254.0.0/16 -j DROP" in script
    assert "iptables -w -t raw -I PREROUTING 1 -s 169.254.0.0/16 -j DROP" not in script
    # IPv6 unsupported: disabled on guest interfaces + forwarded v6 dropped.
    assert "net.ipv6.conf.default.disable_ipv6 = 1" in script
    assert "ip6tables -w -A FORWARD -j DROP" in script
    # Control-plane conntrack immunity: pveproxy (8006) and ssh (22) are exempt from
    # conntrack (raw NOTRACK, both directions), so a guest that fills the host
    # conntrack table can't get Inspect's connection dropped by "table full".
    assert "for _port in 8006 22; do" in script
    assert (
        'iptables -w -t raw -I PREROUTING 1 -p tcp --dport "$_port" -j CT --notrack'
        in script
    )
    assert (
        'iptables -w -t raw -I OUTPUT 1 -p tcp --sport "$_port" -j CT --notrack'
        in script
    )
    # Boot service installed and enabled.
    assert "ExecStart=/usr/local/bin/inspect-proxmox-block-cloud-metadata.sh" in script
    assert "systemctl enable inspect-proxmox-block-cloud-metadata.service" in script
    # Anti-DoS: a udev-triggered ingress policer caps each sandbox tap's guest->host
    # packet rate, so a flooding guest can't exhaust host conntrack/softirq. The
    # policer drops before conntrack (tc ingress), which NOTRACK/connlimit can't do.
    assert 'KERNEL=="tap*i*"' in script
    assert "inspect-proxmox-tap-policer@$name.service" in script
    assert "action police pkts_rate" in script
    assert "handle ffff: ingress" in script
    # The single-cloud denylist is gone.
    assert "169.254.169.254/32" not in script
    assert "fd00:ec2::254" not in script


def _extract_block_metadata(script: str) -> str:
    """Body of the `<< 'BLOCK_METADATA'` heredoc that both provisioners emit."""
    marker_open = "<< 'BLOCK_METADATA'\n"
    start = script.index(marker_open) + len(marker_open)
    end = script.index("\nBLOCK_METADATA\n", start)
    return script[start:end]


def _extract_heredoc(script: str, marker: str) -> str:
    open_m = f"<< '{marker}'\n"
    start = script.index(open_m) + len(open_m)
    end = script.index(f"\n{marker}\n", start)
    return script[start:end]


def test_block_metadata_script_identical_across_provisioners() -> None:
    # The confine-guests script (link-local block, IPv6 drop, control-plane NOTRACK)
    # is duplicated into both provisioners and must stay byte-identical; drift means
    # one host family silently loses an isolation or availability control.
    ec2 = (EC2_SCRIPTS / "userdata.sh").read_text()
    virt = VIRTUALIZED_SCRIPT.read_text()
    assert _extract_block_metadata(ec2) == _extract_block_metadata(virt)


def test_tap_policer_identical_across_provisioners() -> None:
    # The tap ingress policer (guest->host flood / DoS protection) is likewise
    # duplicated and must not drift between host families.
    ec2 = (EC2_SCRIPTS / "userdata.sh").read_text()
    virt = VIRTUALIZED_SCRIPT.read_text()
    assert _extract_heredoc(ec2, "TAP_POLICER") == _extract_heredoc(virt, "TAP_POLICER")
