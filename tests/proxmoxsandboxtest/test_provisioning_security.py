import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
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
    assert "iptables -w -t raw -I PREROUTING 2 -d 169.254.0.0/16 -j DROP" in script
    # Source drop in FORWARD, not PREROUTING, so the host's own link-local
    # replies (INPUT) are left intact.
    assert "iptables -w -I FORWARD 1 -s 169.254.0.0/16 -j DROP" in script
    assert "iptables -w -t raw -I PREROUTING 1 -s 169.254.0.0/16 -j DROP" not in script
    # Host-bound traffic is denied before conntrack; infrastructure services
    # and management ingress keep their explicit exceptions.
    assert "CHAIN=INSP-SANDBOX-HOST-LOCAL" in script
    assert "iptables-restore -w --noflush" in script
    assert "cat /etc/inspect-proxmox/host-local.rules" in script
    rules = _extract_heredoc(script, "HOST_LOCAL_RULES")
    assert "--sport 68 --dport 67 -j RETURN" in rules
    assert "-p udp --dport 53 -m addrtype --dst-type LOCAL -j RETURN" in rules
    assert "-p tcp --dport 53 -m addrtype --dst-type LOCAL -j RETURN" in rules
    assert "-m addrtype --dst-type LOCAL -j DROP" in rules
    assert "--physdev-is-bridged" not in script
    assert '[ -e "/sys/class/net/${MEMBER##*/}/device" ] || continue' in script
    # IPv6 unsupported: disabled on guest interfaces + forwarded v6 dropped.
    assert "net.ipv6.conf.default.disable_ipv6 = 1" in script
    assert "ip6tables -w -A FORWARD -j DROP" in script
    # Boot service installed and enabled.
    assert "ExecStart=/usr/local/bin/inspect-proxmox-block-cloud-metadata.sh" in script
    assert "systemctl enable inspect-proxmox-block-cloud-metadata.service" in script
    # The single-cloud denylist is gone.
    assert "169.254.169.254/32" not in script
    assert "fd00:ec2::254" not in script


def _extract_heredoc(script: str, delimiter: str) -> str:
    match = re.search(
        rf"<< '{delimiter}'\n(.*?\n){delimiter}\n", script, flags=re.DOTALL
    )
    assert match, f"heredoc {delimiter} not found"
    return match.group(1)


@pytest.mark.parametrize(
    "provisioner", [EC2_SCRIPTS / "userdata.sh", VIRTUALIZED_SCRIPT]
)
def test_host_guard_gates_api_and_guest_startup(provisioner: Path) -> None:
    unit = _extract_heredoc(provisioner.read_text(), "BLOCK_METADATA_UNIT")
    settings = dict(line.split("=", 1) for line in unit.splitlines() if "=" in line)
    consumers = {"pvedaemon.service", "pveproxy.service", "pve-guests.service"}
    assert consumers <= set(settings.get("RequiredBy", "").split())
    assert consumers <= set(settings.get("Before", "").split())
    # Reload keeps the active dependency and guests running if an invalid policy
    # is rejected atomically; restarting the prerequisite stops its dependents.
    assert settings.get("ExecReload") == settings["ExecStart"]


@pytest.mark.parametrize(
    "delimiter",
    [
        "BLOCK_METADATA",
        "HOST_LOCAL_RULES",
        "EGRESS_LOCKDOWN",
        "EGRESS_LOCKDOWN_UNIT",
        "EGRESS_LOCKDOWN_TIMER",
        "EGRESS_LOCKDOWN_HALT_UNIT",
    ],
)
def test_firewall_heredocs_identical(delimiter: str) -> None:
    userdata = (EC2_SCRIPTS / "userdata.sh").read_text()
    virtualized = VIRTUALIZED_SCRIPT.read_text()

    assert _extract_heredoc(userdata, delimiter) == _extract_heredoc(
        virtualized, delimiter
    )


@pytest.mark.parametrize(
    "provisioner",
    [
        EC2_SCRIPTS / "userdata.sh",
        VIRTUALIZED_SCRIPT,
    ],
)
def test_provisioners_contain_egress_lockdown(provisioner: Path) -> None:
    script = provisioner.read_text()

    assert "cat > /usr/local/bin/inspect-proxmox-egress-lockdown.sh" in script
    assert "ExecStart=/usr/local/bin/inspect-proxmox-egress-lockdown.sh" in script
    assert "systemctl enable inspect-proxmox-egress-lockdown.service" in script
    assert "systemctl enable inspect-proxmox-egress-lockdown.timer" in script
    assert "OnFailure=inspect-proxmox-egress-lockdown-halt.service" in script
    assert "systemctl mask --runtime pveproxy.service pvedaemon.service" in script
    assert "systemctl stop pveproxy.service pvedaemon.service" in script
    assert (
        'iptables -w -t mangle -I FORWARD 1 -o "$NIC" '
        '-m comment --comment "$COMMENT $RUN_ID" -j DROP'
    ) in script
    assert (
        'iptables -w -t mangle -I FORWARD 1 -i "$NIC" '
        '-m comment --comment "$COMMENT $RUN_ID" -j DROP'
    ) in script
    assert (
        'iptables -w -t mangle -I OUTPUT 1 -o "$NIC" -m owner --uid-owner dnsmasq '
        '-m comment --comment "$COMMENT $RUN_ID" -j DROP'
    ) in script
    assert (
        "iptables -w -t mangle -I FORWARD 1 "
        '-m comment --comment "$COMMENT $RUN_ID" -j DROP'
    ) in script
    assert (
        "iptables -w -t filter -I INPUT 1 ! -i lo -p udp --dport 53 "
        '-m comment --comment "$COMMENT $RUN_ID" -j REJECT'
    ) in script
    assert (
        "iptables -w -t filter -I INPUT 1 ! -i lo -p tcp --dport 53 "
        '-m comment --comment "$COMMENT $RUN_ID" -j REJECT --reject-with tcp-reset'
    ) in script
    assert "OnUnitActiveSec=1min" in script
    lockdown = _extract_heredoc(script, "EGRESS_LOCKDOWN")
    assert "could not determine management NIC" not in lockdown
    assert "ERROR: no default-route NIC found" in lockdown
    assert "RemainAfterExit" not in _extract_heredoc(script, "EGRESS_LOCKDOWN_UNIT")


@unittest.skipUnless(
    os.environ.get("PROXMOX_KERNEL_TESTS") == "1", "requires a disposable Linux host"
)
class HostLocalKernelTest(unittest.TestCase):
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
        else:
            userdata = Path(__file__).parents[2] / (
                "src/proxmoxsandbox/scripts/ec2/userdata.sh"
            )
            self.script = userdata.read_text().split("<< 'BLOCK_METADATA'\n", 1)[1]
            self.script = self.script.split("\nBLOCK_METADATA\n", 1)[0]
        if source:
            rules = Path("/etc/inspect-proxmox/host-local.rules").read_text()
        else:
            rules = userdata.read_text().split("<< 'HOST_LOCAL_RULES'\n", 1)[1]
            rules = rules.split("\nHOST_LOCAL_RULES\n", 1)[0]
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
