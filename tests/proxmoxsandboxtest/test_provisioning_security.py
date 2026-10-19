"""Pins the host configuration shipped in the inspect-proxmox-host debs under host/.

Also checks that both provisioners install the same release of the bundle, and that
the release matches the contract the e2e tests assert.
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
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
GUARD_SCRIPT = LIBEXEC / "block-cloud-metadata"
HOST_LOCAL_RULES = BASE_PKG / "etc" / "inspect-proxmox" / "host-local.rules"


def test_launch_contains_expected_imds_settings() -> None:
    launch = (EC2_SCRIPTS / "launch.sh").read_text()

    assert (
        "HttpTokens=required,HttpPutResponseHopLimit=1,"
        "HttpProtocolIpv6=disabled,InstanceMetadataTags=enabled"
    ) in launch


def test_block_cloud_metadata_rules() -> None:
    # RFC 3927 section 7 (a router must not forward IPv4 link-local), rather than a
    # denylist of one cloud's metadata IP; IPv6 is unsupported for sandbox guests.
    script = GUARD_SCRIPT.read_text()

    # Destination drop in raw PREROUTING (host requests are OUTPUT, unaffected),
    # behind the host-local guard's jump.
    assert "iptables -w -t raw -I PREROUTING 2 -d 169.254.0.0/16 -j DROP" in script
    # Source drop in FORWARD, not PREROUTING, so the host's own link-local
    # replies (INPUT) are left intact.
    assert "iptables -w -I FORWARD 1 -s 169.254.0.0/16 -j DROP" in script
    assert "iptables -w -t raw -I PREROUTING 1 -s 169.254.0.0/16 -j DROP" not in script
    assert "ip6tables -w -A FORWARD -j DROP" in script
    # Host-bound traffic is denied before conntrack; infrastructure services
    # and management ingress keep their explicit exceptions.
    assert "CHAIN=INSP-SANDBOX-HOST-LOCAL" in script
    assert "iptables-restore -w --noflush" in script
    assert "cat /etc/inspect-proxmox/host-local.rules" in script
    assert "--physdev-is-bridged" not in script
    assert '[ -e "/sys/class/net/${MEMBER##*/}/device" ] || continue' in script
    rules = HOST_LOCAL_RULES.read_text()
    assert "--sport 68 --dport 67 -j RETURN" in rules
    assert "-p udp --dport 53 -m addrtype --dst-type LOCAL -j RETURN" in rules
    assert "-p tcp --dport 53 -m addrtype --dst-type LOCAL -j RETURN" in rules
    assert "-m addrtype --dst-type LOCAL -j DROP" in rules
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


def test_host_guard_gates_api_and_guest_startup() -> None:
    unit = (UNITS / "inspect-proxmox-block-cloud-metadata.service").read_text()
    settings = dict(
        line.split("=", 1)
        for line in unit.splitlines()
        if "=" in line and not line.startswith("#")
    )
    consumers = {"pvedaemon.service", "pveproxy.service", "pve-guests.service"}
    assert consumers <= set(settings.get("RequiredBy", "").split())
    assert consumers <= set(settings.get("Before", "").split())
    # Reload keeps the active dependency and guests running if an invalid policy
    # is rejected atomically; restarting the prerequisite stops its dependents.
    assert settings.get("ExecReload") == settings["ExecStart"]


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


@unittest.skipUnless(
    os.environ.get("PROXMOX_KERNEL_TESTS") == "1", "requires a disposable Linux host"
)
class HostLocalKernelTest(unittest.TestCase):
    """Runs the guard script in network namespaces: root, iptables, dnsmasq needed.

    PROXMOX_GUARD_SCRIPT points at an installed copy to test a host's own files
    (its /etc/inspect-proxmox/host-local.rules is then used too); otherwise the
    payload under host/ is tested.
    """

    def run_command(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(args, capture_output=True, text=True)
        if check:
            self.assertEqual(result.returncode, 0, f"{args}: {result.stderr}")
        return result.stdout

    def ns(self, who: str, *args: str, check: bool = True) -> str:
        return self.run_command(
            "ip", "netns", "exec", self.names[who], *args, check=check
        )

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="host-guard-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.names = {
            who: f"guard-{os.getpid()}-{who}" for who in ("host", "ext", "vm")
        }
        for name in self.names.values():
            self.run_command("ip", "netns", "add", name)
            self.addCleanup(self.run_command, "ip", "netns", "del", name, check=False)
        for who in self.names:
            self.ns(who, "ip", "link", "set", "lo", "up")
        self.run_command("modprobe", "br_netfilter")
        self.ns("host", "sysctl", "-qw", "net.bridge.bridge-nf-call-iptables=1")
        self.ns("host", "ip", "link", "add", "vmbr0", "type", "bridge")
        self.ns("host", "ip", "link", "set", "vmbr0", "up")
        self.ns("host", "ip", "addr", "add", "192.0.2.1/24", "dev", "vmbr0")
        for who, port, addr in (("ext", "uplink", "2"), ("vm", "tap1", "3")):
            self.ns(
                "host", "ip", "link", "add", port, "type", "veth", "peer", "name", who
            )
            self.ns("host", "ip", "link", "set", who, "netns", self.names[who])
            self.ns("host", "ip", "link", "set", port, "master", "vmbr0")
            self.ns("host", "ip", "link", "set", port, "up")
            self.ns(who, "ip", "link", "set", who, "up")
            self.ns(who, "ip", "addr", "add", f"192.0.2.{addr}/24", "dev", who)
        self.ns("host", "ip", "route", "add", "default", "via", "192.0.2.2")
        brif = self.root / "sys/vmbr0/brif"
        brif.mkdir(parents=True)
        for port in ("uplink", "tap1"):
            (self.root / f"sys/{port}/brport").mkdir(parents=True)
            (brif / port).symlink_to(f"../../{port}/brport")
        (self.root / "sys/uplink/device").mkdir()
        source = os.environ.get("PROXMOX_GUARD_SCRIPT")
        if source:
            self.script = Path(source).read_text()
            rules = Path("/etc/inspect-proxmox/host-local.rules").read_text()
        else:
            self.script = GUARD_SCRIPT.read_text()
            rules = HOST_LOCAL_RULES.read_text()
        self.rules_path = self.root / "host-local.rules"
        self.rules_path.write_text(rules + "\n")
        self.script = self.script.replace(
            "/etc/inspect-proxmox/host-local.rules", str(self.rules_path)
        )
        self.script = self.script.replace("/sys/class/net", str(self.root / "sys"))
        self.path = self.root / "guard.sh"
        self.path.write_text(self.script)

    def start(self, who: str, *args: str) -> None:
        process = subprocess.Popen(
            ["ip", "netns", "exec", self.names[who], *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.wait, 5)
        self.addCleanup(process.terminate)

    def connect(self, who: str, address: str, port: int) -> int:
        return int(
            self.ns(
                who,
                "python3",
                "-c",
                f"""
import socket
with socket.socket() as s:
 s.settimeout(1)
 print(s.connect_ex(({address!r},{port})))
""",
            )
        )

    def test_management_peer_and_dns_survive_while_host_services_are_blocked(self):
        self.ns("host", "bash", str(self.path))
        self.ns("host", "bash", str(self.path))
        rules = self.ns("host", "iptables", "-t", "raw", "-S", "PREROUTING")
        self.assertEqual(rules.count("-j INSP-SANDBOX-HOST-LOCAL"), 1)
        listener = """
import socket, time
listeners=[]
for port in (22,8006):
 s=socket.socket(); s.bind(('0.0.0.0',port)); s.listen(20); listeners.append(s)
time.sleep(60)
"""
        self.start("host", "python3", "-c", listener)
        self.start("ext", "python3", "-c", listener)
        self.start(
            "host",
            "dnsmasq",
            "--no-daemon",
            "--no-resolv",
            "--no-hosts",
            "--address=/guard.test/192.0.2.42",
            "--interface=vmbr0",
            "--bind-interfaces",
            "--pid-file=",
        )
        for _ in range(20):
            if self.connect("host", "127.0.0.1", 8006) == 0:
                break
            time.sleep(0.1)
        for port in (22, 8006):
            self.assertEqual(self.connect("ext", "192.0.2.1", port), 0)
            self.assertNotEqual(self.connect("vm", "192.0.2.1", port), 0)
            self.assertEqual(self.connect("vm", "192.0.2.2", port), 0)
        dns = self.ns(
            "vm",
            "python3",
            "-c",
            """
import socket, struct
query=b'\\x12\\x34\\x01\\x00\\x00\\x01\\x00\\x00\\x00\\x00\\x00\\x00\\x05guard\\x04test\\x00\\x00\\x01\\x00\\x01'
for kind in (socket.SOCK_DGRAM,socket.SOCK_STREAM):
 with socket.socket(type=kind) as s:
  s.settimeout(2); s.connect(('192.0.2.1',53))
  s.sendall(query if kind == socket.SOCK_DGRAM else struct.pack('!H',len(query))+query)
  if kind == socket.SOCK_DGRAM:
   reply=s.recv(1024)
  else:
   with s.makefile('rb') as response:
    length=struct.unpack('!H',response.read(2))[0]
    reply=response.read(length)
  assert socket.inet_aton('192.0.2.42') in reply, reply
print('UDP_AND_TCP_DNS_OK')
""",
        )
        self.assertIn("UDP_AND_TCP_DNS_OK", dns)

    def test_invalid_policy_keeps_previous_rules_and_other_chains(self):
        self.ns("host", "iptables", "-t", "raw", "-N", "UNRELATED")
        self.ns("host", "iptables", "-t", "raw", "-A", "UNRELATED", "-j", "DROP")
        self.ns("host", "bash", str(self.path))
        before = self.ns("host", "iptables", "-t", "raw", "-S")
        self.rules_path.write_text("-A missing-chain -j DROP\n")
        result = subprocess.run(
            ["ip", "netns", "exec", self.names["host"], "bash", str(self.path)],
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.ns("host", "iptables", "-t", "raw", "-S"), before)
        self.assertIn("-A UNRELATED -j DROP", before)
        self.rules_path.unlink()
        result = subprocess.run(
            ["ip", "netns", "exec", self.names["host"], "bash", str(self.path)],
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.ns("host", "iptables", "-t", "raw", "-S"), before)

    def test_unknown_bridge_uplink_fails_before_changing_rules(self):
        shutil.rmtree(self.root / "sys/uplink/device")
        result = subprocess.run(
            ["ip", "netns", "exec", self.names["host"], "bash", str(self.path)],
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        rules = self.ns("host", "iptables", "-t", "raw", "-S")
        self.assertNotIn("INSP-SANDBOX-HOST-LOCAL", rules)
