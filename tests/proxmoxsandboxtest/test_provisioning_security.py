import re
import shlex
import subprocess
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


def _extract_heredoc(script: str, delimiter: str) -> str:
    match = re.search(
        rf"<< '{delimiter}'\n(.*?\n){delimiter}\n", script, flags=re.DOTALL
    )
    assert match, f"heredoc {delimiter} not found"
    return match.group(1)


@pytest.mark.parametrize(
    "delimiter",
    [
        "EGRESS_LOCKDOWN",
        "EGRESS_LOCKDOWN_UNIT",
        "EGRESS_LOCKDOWN_TIMER",
        "EGRESS_LOCKDOWN_HALT_UNIT",
    ],
)
def test_egress_lockdown_heredocs_identical(delimiter: str) -> None:
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
    assert "OnUnitActiveSec=1min" in script
    lockdown = _extract_heredoc(script, "EGRESS_LOCKDOWN")
    assert "could not determine management NIC" not in lockdown
    assert "ERROR: no default-route NIC found" in lockdown
    assert "RemainAfterExit" not in _extract_heredoc(script, "EGRESS_LOCKDOWN_UNIT")


def _fixup_seed_extraction_line() -> str:
    """Return the fixup's ``SEEDED_HASH=...`` pipeline that reads the marker."""
    fixup = _extract_heredoc(
        (EC2_SCRIPTS / "userdata.sh").read_text(), "FIXUP_PASSWORD"
    )
    for line in fixup.splitlines():
        if line.startswith("SEEDED_HASH="):
            return line
    raise AssertionError("SEEDED_HASH extraction line not found in the fixup")


def test_seeded_password_fixup_extracts_the_hash_from_user_data(tmp_path) -> None:
    # Run the fixup's real extraction pipeline, redirecting only its data source
    # (call-ec2-hypervisor) to a fake user-data blob — the grep/cut under test is
    # taken verbatim from the script.
    blob = tmp_path / "user-data"

    def extract(user_data: str) -> str:
        blob.write_text(user_data)
        line = _fixup_seed_extraction_line().replace(
            "/usr/local/bin/call-ec2-hypervisor latest/user-data",
            f"cat {shlex.quote(str(blob))}",
        )
        script = f'set -euo pipefail\n{line}\nprintf "%s" "$SEEDED_HASH"'
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=True
        ).stdout

    hashed = "$6$" + "a" * 8 + "$" + "b" * 40
    # Present — including indented, as it is once embedded in the cloud-init doc.
    assert extract(f"#cloud-config\n      # proxmox-root-pw-hash={hashed}\n") == hashed
    # Absent — empty, so the fixup falls through to generating a password.
    assert extract("#cloud-config\nruncmd:\n  - echo hi\n") == ""


def test_seeded_password_fixup_applies_encrypted_and_drops_the_plaintext_file() -> None:
    # The apply half writes to /root, so it can't be exercised root-free here;
    # assert on the script text (as the rest of this suite does) that a seeded
    # hash is applied as already-encrypted and the plaintext file the SSM-fetch
    # path relies on is removed.
    fixup = _extract_heredoc(
        (EC2_SCRIPTS / "userdata.sh").read_text(), "FIXUP_PASSWORD"
    )
    assert "chpasswd -e" in fixup
    assert "rm -f /root/root-password" in fixup
