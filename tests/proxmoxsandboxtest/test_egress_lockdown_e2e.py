"""End-to-end check that the optional egress lockdown holds from inside a guest.

Skipped unless `PROXMOX_EGRESS_LOCKDOWN_ENABLED` is set: it needs the host's
lockdown marker in place and the service started, and a locked-down host fails
the rest of the integration suite (which expects guests to have egress). See
CONTRIBUTING.md for the setup and teardown sequence, including why the built-in
VM template has to be baked before locking the host down.

What lockdown does and does not take away, from the guest's point of view: the
mangle FORWARD drops only see packets crossing a default-route NIC, so anything
host-bound still works — the guest keeps its DHCP lease and can still query the
SDN resolver. Blanking dnsmasq's resolv.conf plus the uid-owner OUTPUT drop
removes that resolver's upstream, so it answers but cannot recurse.
"""

import os
import re

import pytest

from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironment,
    ProxmoxSandboxEnvironmentConfig,
)

from .proxmox_sandbox_utils import setup_sandbox

pytestmark = [
    pytest.mark.req_proxmox,
    pytest.mark.skipif(
        os.getenv("PROXMOX_EGRESS_LOCKDOWN_ENABLED") is None,
        reason=(
            "requires a Proxmox host with egress lockdown active; see CONTRIBUTING.md"
        ),
    ),
]

EXTERNAL_IP = "1.1.1.1"
EXTERNAL_NAME = "example.com"

NOT_LOCKED_DOWN_HINT = (
    "Is /etc/inspect-proxmox-egress-lockdown present, and was "
    "inspect-proxmox-egress-lockdown.service started after creating it?"
)

DNS_PROBE_SCRIPT = """
import socket
import struct
import sys

server, name = sys.argv[1], sys.argv[2]

question = b"".join(
    bytes([len(label)]) + label.encode() for label in name.split(".")
) + bytes(1)
query = struct.pack("!HHHHHH", 0x2A2A, 0x0100, 1, 0, 0, 0) + question
query += struct.pack("!HH", 1, 1)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(5)
sock.sendto(query, (server, 53))
try:
    reply = sock.recv(4096)
except socket.timeout:
    print("NO_REPLY")
    sys.exit(0)

flags, _, answer_count = struct.unpack("!HHH", reply[2:8])
print(f"rcode={flags & 0xF} answers={answer_count}")
"""


async def _dns_probe(
    env: ProxmoxSandboxEnvironment, server: str, name: str
) -> tuple[int, int] | None:
    """Query `server` for `name` from inside the guest; None if nothing replied."""
    result = await env.exec(
        ["python3", "-c", DNS_PROBE_SCRIPT, server, name],
        timeout=30,
    )
    assert result.returncode == 0, (
        f"DNS probe did not run in the guest: {result.stderr!r}"
    )

    output = result.stdout.strip()
    if output == "NO_REPLY":
        return None

    match = re.fullmatch(r"rcode=(\d+) answers=(\d+)", output)
    assert match, f"unexpected DNS probe output: {output!r}"
    return int(match.group(1)), int(match.group(2))


async def test_locked_down_host_denies_guest_egress_but_keeps_sdn_services() -> None:
    """A guest on a locked-down host has no egress, but DHCP and SDN DNS work."""
    task_name = "test_egress_lockdown_e2e"
    config = ProxmoxSandboxEnvironmentConfig()

    _, envs_dict = await setup_sandbox(task_name, config)
    try:
        env = envs_dict["default"]
        assert isinstance(env, ProxmoxSandboxEnvironment)

        address_res = await env.exec(
            ["sh", "-c", "ip -4 -o addr show scope global | awk '{print $4}'"],
            timeout=10,
        )
        assert address_res.returncode == 0
        assert address_res.stdout.split(), (
            "sandbox VM has no global IPv4 address, so DHCP from the SDN "
            "dnsmasq stopped working under lockdown"
        )

        gateway_res = await env.exec(
            ["sh", "-c", "ip route show default | awk '{print $3}'"],
            timeout=10,
        )
        assert gateway_res.returncode == 0
        gateway = gateway_res.stdout.strip()
        assert gateway, (
            "no default gateway inside the sandbox VM, so its DHCP lease "
            "carried no route under lockdown"
        )

        http_res = await env.exec(
            [
                "curl",
                "--silent",
                "--max-time",
                "5",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                f"http://{EXTERNAL_IP}",
            ],
            timeout=15,
        )
        assert http_res.stdout.strip() == "000", (
            f"external address {EXTERNAL_IP}:80 reachable from sandbox VM "
            f"(curl returned http_code={http_res.stdout.strip()!r}). "
            f"{NOT_LOCKED_DOWN_HINT}"
        )

        tcp_res = await env.exec(
            [
                "sh",
                "-c",
                f'timeout 5 bash -c "</dev/tcp/{EXTERNAL_IP}/443" '
                "&& echo open || echo blocked",
            ],
            timeout=15,
        )
        assert tcp_res.stdout.strip() == "blocked", (
            f"external address {EXTERNAL_IP}:443 reachable from sandbox VM: "
            f"{tcp_res.stdout!r}. {NOT_LOCKED_DOWN_HINT}"
        )

        getent_res = await env.exec(["getent", "hosts", EXTERNAL_NAME], timeout=30)
        assert getent_res.returncode != 0, (
            f"{EXTERNAL_NAME} resolved from sandbox VM via the guest's normal "
            f"resolver path: {getent_res.stdout!r}. {NOT_LOCKED_DOWN_HINT}"
        )

        external_probe = await _dns_probe(env, gateway, EXTERNAL_NAME)
        assert external_probe is not None, (
            f"SDN resolver on {gateway} stopped answering under lockdown; "
            "guests should keep internal DNS because their queries are "
            "host-bound rather than forwarded"
        )
        rcode, answer_count = external_probe
        assert not (rcode == 0 and answer_count > 0), (
            f"SDN resolver on {gateway} still recursed upstream for "
            f"{EXTERNAL_NAME} (rcode={rcode}, answers={answer_count}). "
            f"{NOT_LOCKED_DOWN_HINT}"
        )

    finally:
        await ProxmoxSandboxEnvironment.sample_cleanup(
            task_name=task_name,
            config=config,
            environments=envs_dict,
            interrupted=False,
        )
