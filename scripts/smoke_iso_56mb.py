"""56 MiB head-to-head: ISO path vs chunked-QGA path.

Matches the PR 70 motivating workload. Verifies via in-guest sha256.

Run:
    set -a; source .env; set +a
    uv run python scripts/smoke_iso_56mb.py
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import time

from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironment,
    ProxmoxSandboxEnvironmentConfig,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger("smoke_iso_56mb")

SIZE = 56 * 1024 * 1024


async def write_and_verify(
    env: ProxmoxSandboxEnvironment, label: str, target: str, payload: bytes
) -> float:
    log.info(f"[{label}] writing {len(payload):,} bytes to {target}")
    t0 = time.monotonic()
    await env.write_file(target, payload)
    elapsed = time.monotonic() - t0
    log.info(f"[{label}] wrote in {elapsed:.2f}s")

    expected_sha = hashlib.sha256(payload).hexdigest()
    expected_size = str(len(payload))
    result = await env.exec(
        cmd=["sh", "-c", f"sha256sum {target} | awk '{{print $1}}'; wc -c < {target}"]
    )
    if not result.success:
        raise AssertionError(f"[{label}] verify exec failed: {result.stderr}")
    out_lines = result.stdout.strip().split("\n")
    actual_sha, actual_size = out_lines[0].strip(), out_lines[1].strip()
    if actual_sha != expected_sha or actual_size != expected_size:
        raise AssertionError(
            f"[{label}] MISMATCH expected sha={expected_sha[:12]} size={expected_size}; "
            f"got sha={actual_sha[:12]} size={actual_size}"
        )
    log.info(f"[{label}] OK sha256={expected_sha[:12]} — {elapsed:.2f}s")
    return elapsed


async def main() -> None:
    cfg = ProxmoxSandboxEnvironmentConfig()
    task_name = "smoke_iso_56"
    await ProxmoxSandboxEnvironment.task_init(task_name=task_name, config=None)
    envs = await ProxmoxSandboxEnvironment.sample_init(
        task_name=task_name, config=cfg, metadata={}
    )
    env = envs["default"]
    assert isinstance(env, ProxmoxSandboxEnvironment)
    try:
        payload = secrets.token_bytes(SIZE)

        # ISO path (default).
        iso_t = await write_and_verify(env, "56 MiB ISO", "/tmp/wf_56_iso.bin", payload)

        # QGA chunked path: same payload, raise threshold to force fallback.
        env.ISO_WRITE_THRESHOLD_BYTES = 10 * 1024 * 1024 * 1024  # type: ignore[misc]
        qga_t = await write_and_verify(
            env, "56 MiB QGA chunked", "/tmp/wf_56_qga.bin", payload
        )

        log.info("=" * 60)
        log.info(f"56 MiB ISO       : {iso_t:.2f}s")
        log.info(f"56 MiB QGAchunk  : {qga_t:.2f}s")
        log.info(f"speedup          : {qga_t / iso_t:.1f}x")
        log.info("=" * 60)
    finally:
        await ProxmoxSandboxEnvironment.sample_cleanup(
            task_name=task_name,
            config=cfg,
            environments=envs,
            interrupted=False,
        )


if __name__ == "__main__":
    if not os.environ.get("PROXMOX_HOST"):
        raise SystemExit("Source .env first: set -a; source .env; set +a")
    asyncio.run(main())
