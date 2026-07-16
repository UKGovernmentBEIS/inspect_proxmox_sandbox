#!/usr/bin/env python3
"""PoC: can an in-range nmap sever Inspect's control connection to Proxmox?

Runs a real Inspect eval against the proxmox sandbox provider. The solver launches an
nmap in the guest that fills the host's nf_conntrack table, then -- while the scan runs
-- probes two things each second and times them:

  * a raw HTTPS connection to pveproxy (:8006)  -- the control plane. This is the exact
    thing the incident dropped, so it is the clean signal for the conntrack effect the
    NOTRACK fix targets.
  * sandbox().read_file()                        -- a real Inspect operation. It rides
    the same :8006 -> qemu-guest-agent path, so it FOLLOWS the :8006 result: when a full
    table drops :8006 SYNs, read_file's connections drop too. (The guest agent itself
    stays responsive under the scan -- a local `qm agent ping` succeeds throughout -- so
    this is not guest-CPU starvation. read_file can look noisier than the raw probe
    because the provider retries it internally.)

Against a host WITHOUT the fix, the :8006 probe stalls/fails once the table fills (the
incident). Against a fixed host (NOTRACK on :8006/22), it stays healthy with the table
full -- UNLESS the scan's packet rate is high enough to saturate the host's own
forwarding softirq, which slows pveproxy regardless of conntrack. That second regime is
a host-capacity limit, not this bug, and NOTRACK does not address it; where its
threshold sits relative to real scanners (nmap rate, masscan, guest core count) is
not characterised here.

Runs with sandbox_cleanup=False, so the VM (and nmap) keep running afterwards for manual
poking; the cleanup command is printed at the end.

Usage:
    source env-<host>            # the PROXMOX_* env this provider reads
    uv run python poc/conntrack_control_plane_poc.py [--scan-target 198.18.0.0/16] \
        [--duration 60]

--scan-target defaults to 198.18.0.0/16 (RFC 2544 benchmarking range, a routable
blackhole that belongs to nobody) so the scan fills the host table via the SNAT path
without hitting real hosts.
"""

import argparse
import asyncio
import os
import time

import httpx
from inspect_ai import Task, eval, task
from inspect_ai.dataset import Sample
from inspect_ai.solver import Solver, TaskState, solver
from inspect_ai.util import sandbox

DEFAULT_SCAN_TARGET = "198.18.0.0/16"
DEFAULT_DURATION = 60


@solver
def _scan_and_probe(scan_target: str, duration: int) -> Solver:
    async def solve(state: TaskState, generate):  # type: ignore[no-untyped-def]
        sb = sandbox()

        # nmap -sS needs raw sockets; QGA exec runs as root in the guest.
        await sb.exec(
            [
                "bash",
                "-lc",
                "command -v nmap >/dev/null || "
                "{ apt-get update && apt-get install -y nmap; }",
            ],
            timeout=300,
        )
        # Detached, a single nmap -- a realistic agent scan. -Pn/-n skip discovery/DNS
        # and --max-retries 0 stops backoff against the blackhole, so it fills the host
        # conntrack table in ~20s. Deliberately not parallelised: several nmaps fill
        # faster but also peg the host CPU, which adds pveproxy latency unrelated to
        # conntrack and confounds the control-plane signal.
        await sb.exec(
            [
                "bash",
                "-lc",
                "nohup nmap -sS -Pn -n -T5 --min-rate 30000 --max-retries 0 "
                f"-p- {scan_target} >/dev/null 2>&1 & echo launched",
            ],
            timeout=30,
        )

        host = os.environ["PROXMOX_HOST"]
        port = os.environ.get("PROXMOX_PORT", "8006")
        api_url = f"https://{host}:{port}/"

        await asyncio.sleep(20)  # let the scan fill the table

        # Split timeout isolates the conntrack signal from host-CPU noise: a full
        # conntrack table DROPS the connection's SYN, so a short connect timeout fails
        # on an unfixed host but succeeds on a NOTRACK'd one. The generous read timeout
        # absorbs transient pveproxy slowness during nmap's fill burst (host CPU)
        # without misreading it as the conntrack failure.
        probes: list[dict] = []
        print("\n  t   API :8006 (control plane)   read_file (also guest-CPU bound)")
        timeout = httpx.Timeout(connect=4.0, read=12.0, write=12.0, pool=12.0)
        async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
            for i in range(duration):
                t0 = time.monotonic()
                try:
                    await client.get(api_url)
                    api_ok, api_ms = True, (time.monotonic() - t0) * 1000
                except Exception:
                    api_ok, api_ms = False, (time.monotonic() - t0) * 1000

                t1 = time.monotonic()
                try:
                    await asyncio.wait_for(sb.read_file("/etc/hostname"), timeout=5)
                    rf_ok, rf_ms = True, (time.monotonic() - t1) * 1000
                except Exception:
                    rf_ok, rf_ms = False, (time.monotonic() - t1) * 1000

                probes.append(
                    {"t": i, "api_ok": api_ok, "api_ms": api_ms,
                     "rf_ok": rf_ok, "rf_ms": rf_ms}
                )
                print(
                    f" {i:3d}s  {'OK  ' if api_ok else 'FAIL'} {api_ms:6.0f}ms"
                    f"            {'OK  ' if rf_ok else 'FAIL'} {rf_ms:6.0f}ms"
                )
                await asyncio.sleep(1)

        state.metadata["probes"] = probes
        return state

    return solve


@task
def _poc_task(scan_target: str, duration: int) -> Task:
    return Task(
        dataset=[Sample(input="poc", target="poc")],
        solver=[_scan_and_probe(scan_target, duration)],
        sandbox="proxmox",
    )


def main() -> None:
    """Run the PoC eval against the configured host and print the verdict."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scan-target", default=DEFAULT_SCAN_TARGET)
    ap.add_argument("--duration", type=int, default=DEFAULT_DURATION)
    args = ap.parse_args()

    host = os.environ.get("PROXMOX_HOST", "<unset>")
    print(
        f'watch the host fill up with:  ssh {host} "watch -n1 cat '
        '/proc/sys/net/netfilter/nf_conntrack_count"'
    )

    logs = eval(
        tasks=[_poc_task(args.scan_target, args.duration)],
        model="mockllm/model",
        sandbox_cleanup=False,
        display="plain",
    )

    probes = logs[0].samples[0].metadata.get("probes", [])
    api_fail = sum(1 for p in probes if not p["api_ok"])
    rf_fail = sum(1 for p in probes if not p["rf_ok"])

    print("\n==== result ====")
    print(
        f"probes: {len(probes)}   :8006 connect failures: {api_fail}   "
        f"read_file failures: {rf_fail}"
    )
    print(
        "How to read this:\n"
        "  * UNFIXED host: :8006 connect failures are SUSTAINED for as long as the\n"
        "    table stays full -- that is the incident (Inspect loses the host).\n"
        "  * FIXED host: :8006 stays reachable; expect at most a few blips during\n"
        "    nmap's fill burst (host CPU spikes, not conntrack -- NOTRACK can't help\n"
        "    that and it clears once the scan throttles against the full table).\n"
        "  * read_file rides the same :8006 path, so it FOLLOWS :8006 -- it fails\n"
        "    when :8006 drops and recovers with it. The guest agent itself stays up\n"
        "    (a local `qm agent ping` succeeds under the scan); read_file only looks\n"
        "    noisier because the provider retries it internally.\n"
        "For an unambiguous check, probe :8006 from outside while the table is full\n"
        "and steady (see the host conntrack watch command above)."
    )

    print("\nVM left running (sandbox_cleanup=False) and still scanning.")
    print("clean up with:  inspect sandbox cleanup proxmox")


if __name__ == "__main__":
    main()
