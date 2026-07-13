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
    # Boot service installed and enabled.
    assert "ExecStart=/usr/local/bin/inspect-proxmox-block-cloud-metadata.sh" in script
    assert "systemctl enable inspect-proxmox-block-cloud-metadata.service" in script
    # The single-cloud denylist is gone.
    assert "169.254.169.254/32" not in script
    assert "fd00:ec2::254" not in script
