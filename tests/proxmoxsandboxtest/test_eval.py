import asyncio
import hashlib
import logging
from pathlib import Path

import pytest
from inspect_ai import Task, eval, task
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.scorer import Score, Target, accuracy, includes, scorer
from inspect_ai.solver import Solver, TaskState, basic_agent, generate, solver
from inspect_ai.tool import bash
from inspect_ai.util import sandbox

from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment

CURRENT_DIR = Path(__file__).parent


@task
def task_for_test() -> Task:
    return Task(
        dataset=[
            Sample(
                input="sample text",
                target="42",
            ),
        ],
        solver=[
            basic_agent(
                tools=[bash()],
                message_limit=20,
            ),
        ],
        scorer=includes(),
        sandbox="proxmox",
    )


def test_inspect_eval() -> None:
    eval_logs = eval(
        tasks=[task_for_test()],
        model=get_model(
            "mockllm/model",
            custom_outputs=[
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="bash",
                    tool_arguments={"cmd": "uname -a"},
                ),
                ModelOutput.for_tool_call(
                    model="mockllm/model",
                    tool_name="submit",
                    tool_arguments={"answer": "42"},
                ),
            ],
        ),
        log_level="trace",
        display="plain",
        # sandbox_cleanup=False
    )

    assert len(eval_logs) == 1
    assert eval_logs[0]
    assert eval_logs[0].error is None
    assert eval_logs[0].samples
    sample = eval_logs[0].samples[0]
    tool_calls = [x for x in sample.messages if x.role == "tool"]
    assert "ubuntu" in tool_calls[0].text


def _make_structured_payload(line_count: int) -> bytes:
    """Build a fixed-width known-structured text payload.

    Each line ~25 chars, padded to a fixed width so byte offsets are
    predictable. ~5 MiB for 200k lines — well over the 1 MiB fast-path
    threshold.
    """
    return b"\n".join(
        f"line {i:08d} payload xyz".encode() for i in range(line_count)
    ) + b"\n"


_DMESG_RED_FLAGS = (
    "Can't open blockdev",
    "ahci: ",
    "ata.*: failed",
    "I/O error",
    "Buffer I/O error",
)


@solver
def _write_and_inspect(line_count: int) -> Solver:
    """Solver: snapshot guest kernel state, write payload, snapshot again.

    The interesting failure modes for the ISO fast path aren't "the bytes
    didn't arrive" — the chunked-QGA fallback fixes that silently. The
    real failures are kernel-level: AHCI not enumerating sata5, optical
    open() rate-limiting, media-change events lost. So this solver:

    1. Snapshots dmesg line count BEFORE the write so we can diff later.
    2. Records the guest's view of /dev/sr* and the AHCI ata host count.
    3. Does the write (sha-verified in-guest as a baseline correctness
       check).
    4. Captures any NEW dmesg lines after the write and scans for known
       trouble signatures.

    Everything ends up in state.metadata so the scorer (and a human
    reading the eval log) can see the actual guest-side picture.
    """

    async def solve(state: TaskState, generate):
        payload = _make_structured_payload(line_count)
        assert len(payload) > 1024 * 1024, (
            f"payload {len(payload)} too small to exercise fast path"
        )
        target = "/tmp/iso_fast_path_demo.txt"

        # 1. Snapshot kernel state before the write.
        pre = await sandbox().exec(
            [
                "sh",
                "-c",
                "dmesg 2>/dev/null | wc -l; "
                "ls -1 /dev/sr* 2>/dev/null | tr '\\n' ' '; echo; "
                "ls /sys/class/ata_port/ 2>/dev/null | wc -l",
            ]
        )
        pre_lines = pre.stdout.strip().split("\n")
        dmesg_lines_pre = int(pre_lines[0])
        sr_devices_pre = pre_lines[1].strip()
        ata_port_count = int(pre_lines[2])

        # 2. Do the write.
        await sandbox().write_file(target, payload)

        # 3. In-guest verification + post-write snapshot.
        post = await sandbox().exec(
            [
                "sh",
                "-c",
                f"wc -l < {target}; "
                f"wc -c < {target}; "
                f"sha256sum {target} | awk '{{print $1}}'; "
                f"dmesg 2>/dev/null | wc -l; "
                "ls -1 /dev/sr* 2>/dev/null | tr '\\n' ' '; echo; "
                # Any dmesg lines added since the pre-snapshot:
                f"dmesg 2>/dev/null | tail -n +{dmesg_lines_pre + 1}",
            ]
        )
        chunks = post.stdout.split("\n")
        actual_lines = int(chunks[0])
        actual_bytes = int(chunks[1])
        actual_sha = chunks[2].strip()
        dmesg_lines_post = int(chunks[3])
        sr_devices_post = chunks[4].strip()
        new_dmesg = "\n".join(chunks[5:]).strip()

        red_flag_hits = [
            line for line in new_dmesg.splitlines()
            if any(flag in line for flag in _DMESG_RED_FLAGS)
        ]

        state.metadata["payload_size"] = len(payload)
        state.metadata["expected_lines"] = line_count
        state.metadata["expected_sha"] = hashlib.sha256(payload).hexdigest()
        state.metadata["sr_devices_pre"] = sr_devices_pre
        state.metadata["sr_devices_post"] = sr_devices_post
        state.metadata["ata_port_count"] = ata_port_count
        state.metadata["dmesg_lines_added"] = dmesg_lines_post - dmesg_lines_pre
        state.metadata["dmesg_new"] = new_dmesg
        state.metadata["dmesg_red_flags"] = red_flag_hits

        state.metadata["actual_lines"] = actual_lines
        state.metadata["actual_bytes"] = actual_bytes
        state.metadata["actual_sha"] = actual_sha
        return state

    return solve


@scorer(metrics=[accuracy()])
def _iso_write_health_scorer():
    """Score 1.0 iff bytes round-trip AND the guest kernel stayed quiet.

    A passing write_file with red flags in dmesg means the fallback masked
    a real kernel issue — don't score that as a clean pass.
    """

    async def score(state: TaskState, target: Target) -> Score:
        m = state.metadata
        checks = {
            "lines": m.get("actual_lines") == m.get("expected_lines"),
            "bytes": m.get("actual_bytes") == m.get("payload_size"),
            "sha":   m.get("actual_sha") == m.get("expected_sha"),
            "kernel_quiet": not m.get("dmesg_red_flags"),
        }
        all_ok = all(checks.values())
        return Score(
            value=1.0 if all_ok else 0.0,
            answer=m.get("actual_sha", ""),
            explanation=(
                f"lines={m.get('actual_lines')}/{m.get('expected_lines')} "
                f"bytes={m.get('actual_bytes')}/{m.get('payload_size')} "
                f"sha_match={checks['sha']} "
                f"sr_pre={m.get('sr_devices_pre')!r} "
                f"sr_post={m.get('sr_devices_post')!r} "
                f"ata_ports={m.get('ata_port_count')} "
                f"new_dmesg_lines={m.get('dmesg_lines_added')} "
                f"red_flags={m.get('dmesg_red_flags')}"
            ),
        )

    return score


@task
def _iso_write_fast_path_task(line_count: int = 200_000) -> Task:
    return Task(
        dataset=[Sample(input="ignored", target="ok")],
        solver=[_write_and_inspect(line_count)],
        scorer=_iso_write_health_scorer(),
        sandbox="proxmox",
    )


def test_iso_write_fast_path_via_eval() -> None:
    """End-to-end eval that exercises and probes the ISO fast path.

    Goes beyond "did the bytes arrive" — captures guest dmesg/device
    state before and after the write, so we'd notice kernel-level
    trouble (AHCI errors, "Can't open blockdev", I/O errors) even on
    runs where the fallback masked a real failure. Asserts:

      1. In-guest sha256/wc all match the host's expectation.
      2. The fast-path log line ('iso_write vm=...') appears — the
         ISO path ran, not the chunked-QGA fallback.
      3. The 'fast path disabled' warning does NOT appear — the path
         didn't silently fail and fall back.
      4. (Soft) any red-flag lines added to dmesg get logged.
    """
    # Attach a manual handler — pytest's caplog doesn't reliably catch
    # records emitted from inside Inspect's eval() pipeline.
    captured: list[tuple[str, str]] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append((record.name, record.getMessage()))

    handler = _Capture(level=logging.INFO)
    target_loggers = [
        logging.getLogger("proxmoxsandbox._impl.iso_write"),
        logging.getLogger("proxmoxsandbox._proxmox_sandbox_environment"),
    ]
    saved_levels: list[int] = []
    for lg in target_loggers:
        saved_levels.append(lg.level)
        lg.setLevel(logging.INFO)
        lg.addHandler(handler)

    try:
        eval_logs = eval(
            tasks=[_iso_write_fast_path_task()],
            model=get_model("mockllm/model"),
            log_level="info",
            display="plain",
        )
    finally:
        for lg, saved in zip(target_loggers, saved_levels):
            lg.removeHandler(handler)
            lg.setLevel(saved)

    assert len(eval_logs) == 1
    log = eval_logs[0]
    assert log.error is None, f"eval errored: {log.error}"
    assert log.samples and len(log.samples) == 1
    sample = log.samples[0]
    assert sample.scores, "no scores recorded"

    m = sample.metadata
    # Hard assertions: write_file's correctness contract.
    assert m["actual_lines"] == m["expected_lines"], (
        f"line count mismatch: {m['actual_lines']} vs {m['expected_lines']}"
    )
    assert m["actual_bytes"] == m["payload_size"], (
        f"byte count mismatch: {m['actual_bytes']} vs {m['payload_size']}"
    )
    assert m["actual_sha"] == m["expected_sha"], (
        f"sha256 mismatch: {m['actual_sha']!r} vs {m['expected_sha']!r}"
    )

    # Hard: the fast path ran (the chunked-QGA fallback would not emit
    # this log line; if it were used, this assertion would catch a silent
    # regression).
    iso_write_log_lines = [
        msg for (name, msg) in captured
        if name == "proxmoxsandbox._impl.iso_write"
        and msg.startswith("iso_write vm=")
    ]
    assert iso_write_log_lines, (
        f"expected at least one 'iso_write vm=' log line proving the "
        f"fast path ran; got nothing — did the path silently fall back? "
        f"captured: {captured[-10:]}"
    )

    # Hard: the per-VM disable mechanism didn't fire — that warning
    # would mean iso_write gave up on this VM entirely.
    disabled_warnings = [
        msg for (_, msg) in captured if "fast path disabled" in msg
    ]
    assert not disabled_warnings, (
        f"fast path got disabled unexpectedly: {disabled_warnings}"
    )

    # Soft: surface guest kernel state. Don't fail the test on red
    # flags — the in-writer detach/re-attach retry recovers from the
    # known first-call "Can't open blockdev" race transparently, and
    # some Proxmox/QEMU builds hit that race consistently. But log it
    # loudly so anyone reading test output can spot a real regression.
    red_flags = m.get("dmesg_red_flags") or []
    if red_flags:
        logging.getLogger(__name__).warning(
            "iso_write fast path emitted guest kernel red flags during "
            "write (path recovered, but worth investigating): %s "
            "(new dmesg lines added: %s)",
            red_flags, m.get("dmesg_lines_added"),
        )


@pytest.mark.skip("Not implemented yet")
async def test_named_vms_across_epochs() -> None:
    # TODO: Implement test for named VMs across multiple epochs
    # This test should verify that the `name=` parameter works on multiple
    # epochs (in parallel) of the same eval by:
    # 1. Creating and running an eval with two epochs
    # 2. Verifying each task can independently access its VMs by name
    #    i.e. by specifying a file to be copied into `vm_alpha` at the start
    pass


@pytest.mark.skip(
    "Does not play well as part of a suite - you can run it individually though"
)  # noqa: E501
async def test_cleanup(qemu_commands: QemuCommands) -> None:
    try:
        all_vms = await qemu_commands.list_vms()

        # avoid event loop conflicts
        await asyncio.to_thread(
            eval,
            tasks=[
                Task(
                    dataset=[
                        Sample(
                            input="hello",
                            target="42",
                            setup="""#!/usr/bin/env bash
set -e
echo "failing!!"
false
""",
                        ),
                    ],
                    solver=[
                        generate(),
                    ],
                    scorer=includes(),
                    sandbox="proxmox",
                )
            ],
            model="mockllm/model",
            log_level="trace",
            sandbox_cleanup=False,
        )

        # Because sandbox_cleanup=False, there should be an extra VM
        post_eval_vms = await qemu_commands.list_vms()
        assert len(post_eval_vms) == len(all_vms) + 1

    finally:
        await ProxmoxSandboxEnvironment.cli_cleanup(id=None)
