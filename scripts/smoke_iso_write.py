"""Smoke test for the ISO write_file path.

Brings up a sandbox VM via the same flow tests use, writes files of
various sizes, reads them back via QGA read_file, and confirms content
round-trips. Times the large write against the chunked-QGA fallback so
you can eyeball the speedup.

Run:
    set -a; source .env; set +a
    uv run python scripts/smoke_iso_write.py
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

log = logging.getLogger("smoke_iso_write")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


async def write_and_verify(
    env: ProxmoxSandboxEnvironment, label: str, target: str, payload: bytes
) -> float:
    log.info(f"[{label}] writing {len(payload):,} bytes to {target}")
    t0 = time.monotonic()
    await env.write_file(target, payload)
    elapsed = time.monotonic() - t0
    log.info(f"[{label}] wrote in {elapsed:.2f}s")

    # Verify in-guest: read_file is capped at 16 MiB, so check sha256 + size
    # via shell instead. This also catches partial / truncated copies.
    import hashlib as _hashlib

    expected_sha = _hashlib.sha256(payload).hexdigest()
    expected_size = str(len(payload))
    log.info(f"[{label}] verifying sha256 in-guest")
    result = await env.exec(
        cmd=["sh", "-c", f"sha256sum {target} | awk '{{print $1}}'; wc -c < {target}"]
    )
    if not result.success:
        raise AssertionError(f"[{label}] verify exec failed: {result.stderr}")
    out_lines = result.stdout.strip().split("\n")
    actual_sha, actual_size = out_lines[0].strip(), out_lines[1].strip()
    if actual_sha != expected_sha or actual_size != expected_size:
        raise AssertionError(
            f"[{label}] mismatch! expected sha={expected_sha[:12]} size={expected_size}; "
            f"got sha={actual_sha[:12]} size={actual_size}"
        )
    log.info(
        f"[{label}] OK ({len(payload):,} bytes, sha256={expected_sha[:12]}) — {elapsed:.2f}s"
    )
    return elapsed


async def main() -> None:
    cfg = ProxmoxSandboxEnvironmentConfig()
    task_name = "smoke_iso"
    await ProxmoxSandboxEnvironment.task_init(task_name=task_name, config=None)
    envs = await ProxmoxSandboxEnvironment.sample_init(
        task_name=task_name, config=cfg, metadata={}
    )
    env = envs["default"]
    assert isinstance(env, ProxmoxSandboxEnvironment)
    try:
        # Below threshold: should take the QGA single-shot path.
        small = secrets.token_bytes(8 * 1024)
        await write_and_verify(env, "small (8 KiB, QGA 1-shot)", "/tmp/wf_small.bin", small)

        # Above threshold: should take the ISO path.
        medium = secrets.token_bytes(2 * 1024 * 1024)
        iso_t = await write_and_verify(
            env, "medium (2 MiB, ISO)", "/tmp/wf_medium.bin", medium
        )

        # Force the chunked-QGA path by raising the threshold, same payload.
        env.ISO_WRITE_THRESHOLD_BYTES = 10 * 1024 * 1024 * 1024  # type: ignore[misc]
        qga_t = await write_and_verify(
            env, "medium (2 MiB, QGA chunked)", "/tmp/wf_medium_qga.bin", medium
        )
        env.ISO_WRITE_THRESHOLD_BYTES = 1 * 1024 * 1024  # type: ignore[misc]

        # Bigger one to show overhead is amortised.
        big = secrets.token_bytes(20 * 1024 * 1024)
        big_iso_t = await write_and_verify(
            env, "big (20 MiB, ISO)", "/tmp/wf_big.bin", big
        )

        log.info("=" * 60)
        log.info(f"2 MiB ISO       : {iso_t:.2f}s")
        log.info(f"2 MiB QGAchunk  : {qga_t:.2f}s")
        log.info(f"20 MiB ISO      : {big_iso_t:.2f}s")
        log.info(
            f"speedup (2 MiB) : {qga_t / iso_t:.1f}x"
        )
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
