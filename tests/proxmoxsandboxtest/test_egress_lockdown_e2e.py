"""End-to-end check that the optional egress lockdown holds from inside a guest.

Skipped unless `PROXMOX_EGRESS_LOCKDOWN_ENABLED` is set: it needs a host with
the lockdown active, which fails the rest of the integration suite. See
CONTRIBUTING.md for the setup and teardown sequence.

Forwarded traffic is dropped, so the guest has no egress. Its DHCP lease still
works, but the SDN resolver does not: with no upstream all it could serve is its
own lease table, so the host rejects port 53 outright. Rejects, not drops,
because dnsmasq advertises itself as the DHCP-supplied resolver and there is no
way to point guests elsewhere: a drop would hang every lookup in the guest until
its resolver timed out.
"""

import os
import re

import pytest

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironment,
    ProxmoxSandboxEnvironmentConfig,
)

from .proxmox_sandbox_utils import require_host_contract, setup_sandbox

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

NOT_LOCKED_DOWN_HINT = "Is the host actually locked down? See CONTRIBUTING.md."

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
sock.settimeout(3)
# connect(), not sendto(): an unconnected UDP socket is never told about the ICMP
# port-unreachable the host's REJECT rule sends back.
sock.connect((server, 53))
# The host rate-limits ICMP port-unreachable per destination (icmp_ratelimit: one a
# second after a small burst) and the guest's own resolver retries drain that, so one
# silence is not a verdict; the wait between attempts is what lets a token back in.
for _ in range(4):
    sock.send(query)
    try:
        reply = sock.recv(4096)
        break
    except ConnectionRefusedError:
        print("REFUSED")
        sys.exit(0)
    except socket.timeout:
        continue
else:
    print("NO_REPLY")
    sys.exit(0)

flags, _, answer_count = struct.unpack("!HHH", reply[2:8])
print(f"rcode={flags & 0xF} answers={answer_count}")
"""


async def _dns_probe(env: ProxmoxSandboxEnvironment, server: str, name: str) -> str:
    """Query `server` for `name` from the guest.

    Returns "REFUSED" (ICMP port-unreachable), "NO_REPLY" (silence on every attempt), or
    "rcode=N answers=M" if the resolver answered.
    """
    result = await env.exec(
        ["python3", "-c", DNS_PROBE_SCRIPT, server, name],
        timeout=30,
    )
    assert result.returncode == 0, (
        f"DNS probe did not run in the guest: {result.stderr!r}"
    )

    output = result.stdout.strip()
    assert output in ("REFUSED", "NO_REPLY") or re.fullmatch(
        r"rcode=\d+ answers=\d+", output
    ), f"unexpected DNS probe output: {output!r}"
    return output


async def test_locked_down_host_denies_guest_egress_and_dns(
    async_proxmox_api: AsyncProxmoxAPI,
) -> None:
    """A guest keeps its DHCP lease, has no egress, and DNS fails fast."""
    # The port-53 rejection below arrived with aisi2, so an older host fails these for a
    # reason that is not a lapse in isolation.
    await require_host_contract(async_proxmox_api)

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
        assert address_res.stdout.split(), "no global IPv4 address, so DHCP broke"

        gateway_res = await env.exec(
            ["sh", "-c", "ip route show default | awk '{print $3}'"],
            timeout=10,
        )
        assert gateway_res.returncode == 0
        gateway = gateway_res.stdout.strip()
        assert gateway, "no default gateway, so the DHCP lease carried no route"

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
            f"{EXTERNAL_IP}:80 reachable "
            f"(http_code={http_res.stdout.strip()!r}). {NOT_LOCKED_DOWN_HINT}"
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
            f"{EXTERNAL_IP}:443 reachable. {NOT_LOCKED_DOWN_HINT}"
        )

        getent_res = await env.exec(["getent", "hosts", EXTERNAL_NAME], timeout=30)
        assert getent_res.returncode != 0, (
            f"{EXTERNAL_NAME} resolved: {getent_res.stdout!r}. {NOT_LOCKED_DOWN_HINT}"
        )

        # REFUSED rather than NO_REPLY is the point: the host rejects, so the
        # guest learns the port is shut in one round trip.
        udp_probe = await _dns_probe(env, gateway, EXTERNAL_NAME)
        assert udp_probe == "REFUSED", (
            f"SDN resolver on {gateway}:53/udp gave {udp_probe!r}, want REFUSED. "
            f"{NOT_LOCKED_DOWN_HINT}"
        )

        dns_tcp_res = await env.exec(
            [
                "sh",
                "-c",
                f'timeout 5 bash -c "exec 3<>/dev/tcp/{gateway}/53" '
                "2>/dev/null; echo $?",
            ],
            timeout=15,
        )
        dns_tcp_rc = dns_tcp_res.stdout.strip()
        assert dns_tcp_rc not in ("0", "124"), (
            f"{gateway}:53/tcp {'reachable' if dns_tcp_rc == '0' else 'dropped'} "
            f"(exit {dns_tcp_rc}), want a rejection. {NOT_LOCKED_DOWN_HINT}"
        )

    finally:
        await ProxmoxSandboxEnvironment.sample_cleanup(
            task_name=task_name,
            config=config,
            environments=envs_dict,
            interrupted=False,
        )
