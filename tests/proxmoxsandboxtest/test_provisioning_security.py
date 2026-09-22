"""Pins the host configuration shipped in the inspect-proxmox-host debs under host/.

Also checks that both provisioners install the same release of the bundle, and that
the release matches the contract the e2e tests assert.
"""

import re
from pathlib import Path

from .proxmox_sandbox_utils import HOST_CONTRACT

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
PROVISIONERS = [EC2_SCRIPTS / "userdata.sh", VIRTUALIZED_SCRIPT]
HOST = REPO_ROOT / "host"
BASE_PKG = HOST / "inspect-proxmox-host"
EC2_PKG = HOST / "inspect-proxmox-host-ec2"
LIBEXEC = BASE_PKG / "usr" / "libexec" / "inspect-proxmox"
UNITS = BASE_PKG / "usr" / "lib" / "systemd" / "system"


def test_launch_contains_expected_imds_settings() -> None:
    launch = (EC2_SCRIPTS / "launch.sh").read_text()

    assert (
        "HttpTokens=required,HttpPutResponseHopLimit=1,"
        "HttpProtocolIpv6=disabled,InstanceMetadataTags=enabled"
    ) in launch


def test_block_cloud_metadata_rules() -> None:
    # RFC 3927 section 7 (a router must not forward IPv4 link-local), rather than a
    # denylist of one cloud's metadata IP; IPv6 is unsupported for sandbox guests.
    script = (LIBEXEC / "block-cloud-metadata").read_text()

    # Destination drop in raw PREROUTING (host requests are OUTPUT, unaffected).
    assert "iptables -w -t raw -I PREROUTING 1 -d 169.254.0.0/16 -j DROP" in script
    # Source drop in FORWARD, not PREROUTING, so the host's own link-local
    # replies (INPUT) are left intact.
    assert "iptables -w -I FORWARD 1 -s 169.254.0.0/16 -j DROP" in script
    assert "iptables -w -t raw -I PREROUTING 1 -s 169.254.0.0/16 -j DROP" not in script
    assert "ip6tables -w -A FORWARD -j DROP" in script
    # The single-cloud denylist is gone.
    assert "169.254.169.254/32" not in script
    assert "fd00:ec2::254" not in script

    sysctl = (
        BASE_PKG / "usr" / "lib" / "sysctl.d" / "99-inspect-proxmox-disable-ipv6.conf"
    ).read_text()
    assert "net.ipv6.conf.default.disable_ipv6 = 1" in sysctl

    unit = (UNITS / "inspect-proxmox-block-cloud-metadata.service").read_text()
    assert "ExecStart=/usr/libexec/inspect-proxmox/block-cloud-metadata" in unit
    assert "WantedBy=multi-user.target" in unit


def test_egress_lockdown() -> None:
    lockdown = (LIBEXEC / "egress-lockdown").read_text()

    assert (
        'iptables -w -t mangle -I FORWARD 1 -o "$NIC" '
        '-m comment --comment "$COMMENT $RUN_ID" -j DROP'
    ) in lockdown
    assert (
        'iptables -w -t mangle -I FORWARD 1 -i "$NIC" '
        '-m comment --comment "$COMMENT $RUN_ID" -j DROP'
    ) in lockdown
    assert (
        'iptables -w -t mangle -I OUTPUT 1 -o "$NIC" -m owner --uid-owner dnsmasq '
        '-m comment --comment "$COMMENT $RUN_ID" -j DROP'
    ) in lockdown
    assert (
        "iptables -w -t mangle -I FORWARD 1 "
        '-m comment --comment "$COMMENT $RUN_ID" -j DROP'
    ) in lockdown
    assert (
        "iptables -w -t filter -I INPUT 1 ! -i lo -p udp --dport 53 "
        '-m comment --comment "$COMMENT $RUN_ID" -j REJECT'
    ) in lockdown
    assert (
        "iptables -w -t filter -I INPUT 1 ! -i lo -p tcp --dport 53 "
        '-m comment --comment "$COMMENT $RUN_ID" -j REJECT --reject-with tcp-reset'
    ) in lockdown
    assert "ERROR: no default-route NIC found" in lockdown

    service = (UNITS / "inspect-proxmox-egress-lockdown.service").read_text()
    assert "ExecStart=/usr/libexec/inspect-proxmox/egress-lockdown" in service
    assert "OnFailure=inspect-proxmox-egress-lockdown-halt.service" in service
    assert "RemainAfterExit" not in service
    assert "WantedBy=multi-user.target" in service

    timer = (UNITS / "inspect-proxmox-egress-lockdown.timer").read_text()
    assert "OnUnitActiveSec=1min" in timer
    assert "WantedBy=timers.target" in timer

    halt = (UNITS / "inspect-proxmox-egress-lockdown-halt.service").read_text()
    assert "systemctl mask --runtime pveproxy.service pvedaemon.service" in halt
    assert "systemctl stop pveproxy.service pvedaemon.service" in halt
    # Only ever run via OnFailure, never enabled on its own.
    assert "[Install]" not in halt


def test_packages_stay_out_of_usr_local() -> None:
    # Debian policy: a package must not ship files under /usr/local.
    for pkg in (BASE_PKG, EC2_PKG):
        assert not (pkg / "usr" / "local").exists()


def _changelog_version() -> int:
    top = (HOST / "debian" / "changelog").read_text().splitlines()[0]
    match = re.match(r"inspect-proxmox-host \((\d+)\)", top)
    assert match, top
    return int(match.group(1))


def test_host_contract_is_the_package_version() -> None:
    # stamp-contract writes .aisi<version>; the e2e tests assert against HOST_CONTRACT.
    assert _changelog_version() == HOST_CONTRACT


def test_provisioners_install_the_current_release() -> None:
    release = f"INSPECT_PROXMOX_HOST_RELEASE=host-v{_changelog_version()}"
    for provisioner in PROVISIONERS:
        script = provisioner.read_text()
        assert release in script, provisioner
        assert "inspect-proxmox-host-debs.tar" in script, provisioner
        assert "apt-get install -y ./" in script, provisioner
        assert "inspect-proxmox-host-seal" in script, provisioner


def _rebuild_version(name: str) -> str:
    script = (HOST / "rebuilds" / name / "build.sh").read_text()
    match = re.search(r'^PATCHED_VERSION="([^"]+)"', script, flags=re.MULTILINE)
    assert match, name
    return match.group(1)


def test_depends_and_pin_track_the_rebuilds() -> None:
    control = (HOST / "debian" / "control").read_text()
    qemu = _rebuild_version("pve-qemu")
    network = _rebuild_version("pve-network")
    assert f"pve-qemu-kvm (>= {qemu})" in control
    assert f"libpve-network-perl (>= {network})" in control
    for version in (qemu, network):
        assert "+aisi" in version

    pin = (
        BASE_PKG / "etc" / "apt" / "preferences.d" / "inspect-proxmox-host"
    ).read_text()
    for package in ("pve-qemu-kvm", "libpve-network-perl", "libpve-network-api-perl"):
        assert package in pin
    assert "Pin-Priority: 1001" in pin


def test_stamp_refuses_without_the_patches() -> None:
    stamp = (LIBEXEC / "stamp-contract").read_text()
    assert "quirk_mode_page_set_block_size" in stamp
    assert "DNS_SETUP:" in stamp
    patch = (
        HOST / "rebuilds" / "pve-network" / "ipam-reuse-ip-for-known-mac.patch"
    ).read_text()
    assert "+DNS_SETUP:" in patch
