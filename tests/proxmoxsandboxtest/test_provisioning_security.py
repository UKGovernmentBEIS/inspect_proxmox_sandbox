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


def test_ec2_launch_requires_imdsv2_with_one_hop() -> None:
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
def test_provisioners_block_forwarded_cloud_metadata(
    provisioner: Path,
) -> None:
    script = provisioner.read_text()

    assert "iptables -w -t raw -C PREROUTING -d 169.254.169.254/32 -j DROP" in script
    assert "iptables -w -t raw -I PREROUTING 1 -d 169.254.169.254/32 -j DROP" in script
    assert "ip6tables -w -t raw -C PREROUTING -d fd00:ec2::254/128 -j DROP" in script
    assert "ip6tables -w -t raw -I PREROUTING 1 -d fd00:ec2::254/128 -j DROP" in script
    assert "ExecStart=/usr/local/bin/inspect-proxmox-block-cloud-metadata.sh" in script
    assert "systemctl enable inspect-proxmox-block-cloud-metadata.service" in script
