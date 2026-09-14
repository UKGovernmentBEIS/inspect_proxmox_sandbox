import asyncio
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from inspect_ai.util import ExecResult
from pydantic import ValidationError
from rich.console import Console
from rich.errors import LiveError
from rich.live import Live

from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox._impl.readiness import (
    ReadinessRunner,
    ReadinessTimeoutError,
    VmReadinessState,
)
from proxmoxsandbox._impl.readiness_display import ReadinessDisplay, render_readiness
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import (
    ReadinessCheck,
    ReadinessCommand,
    ReadinessRepair,
    ReadinessRetry,
    VmConfig,
    VmSourceConfig,
)


def vm(*checks: ReadinessCheck, is_sandbox: bool = False) -> VmConfig:
    return VmConfig(
        vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
        is_sandbox=is_sandbox,
        readiness_checks=checks,
    )


def command_check(**kwargs) -> ReadinessCheck:
    return ReadinessCheck(
        name="service",
        command=ReadinessCommand(argv=("service-health",)),
        **kwargs,
    )


def result(code: int = 0, stdout: str = "") -> ExecResult[str]:
    return ExecResult(success=code == 0, returncode=code, stdout=stdout, stderr="")


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def runner_for(config: VmConfig):
    qemu = MagicMock(spec=QemuCommands)
    qemu.node = "test-node"
    qemu.async_proxmox = MagicMock()
    qemu.async_proxmox.request = AsyncMock(return_value={"status": "running"})
    qemu.ping_qemu_agent = AsyncMock()
    execute = AsyncMock(return_value=result())
    state = VmReadinessState("database", config, vm_id=100)
    clock = FakeClock()
    runner = ReadinessRunner(qemu, state, execute, clock=clock, sleep=clock.sleep)
    return runner, qemu, execute, clock


def test_defaults_and_explicit_builtin_overrides():
    assert [check.kind for check in vm().effective_readiness_checks()] == ["running"]
    assert [
        check.kind for check in vm(is_sandbox=True).effective_readiness_checks()
    ] == ["running", "qemu_agent"]
    custom_ping = ReadinessCheck(
        name="agent", kind="qemu_agent", retry=ReadinessRetry(timeout=900)
    )
    checks = vm(custom_ping, is_sandbox=True).effective_readiness_checks()
    assert len(checks) == 2
    assert checks[1] == custom_ping


def test_agent_enabled_for_non_sandbox_commands_or_repairs():
    qemu = QemuCommands(MagicMock(), "node", "storage", MagicMock(), MagicMock())
    for config, enabled in [
        (vm(), 0),
        (vm(command_check()), 1),
        (vm(is_sandbox=True), 1),
    ]:
        data = {}
        qemu.other_config_json(config, data)
        assert data["agent"] == f"enabled={enabled}"
    assert vm(
        ReadinessCheck(
            name="running",
            kind="running",
            repair=ReadinessRepair(commands=(ReadinessCommand(argv=("repair",)),)),
        )
    ).requires_guest_agent


def test_schema_roundtrip():
    config = vm(
        command_check(
            repair=ReadinessRepair(commands=(ReadinessCommand(argv=("repair",)),))
        )
    )
    assert VmConfig.model_validate_json(config.model_dump_json()) == config


@pytest.mark.parametrize(
    "kwargs",
    [
        {"interval": 0},
        {"backoff": 0.5},
        {"max_interval": 1},
        {"timeout": -1},
        {"attempt_timeout": 0},
        {"timeout": float("inf")},
        {"backoff": float("nan")},
        {"intervall": 1},
    ],
)
def test_invalid_retry(kwargs):
    with pytest.raises(ValidationError):
        ReadinessRetry(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"argv": ()},
        {"argv": ("",)},
        {"argv": ("echo", "\x00")},
        {"argv": ("echo",), "stdout_regex": "["},
        {"argv": ("echo",), "timeout": 0},
    ],
)
def test_invalid_command(kwargs):
    with pytest.raises(ValidationError):
        ReadinessCommand(**kwargs)


def test_invalid_checks_and_repairs():
    with pytest.raises(ValidationError, match="Only command checks"):
        ReadinessCheck(name="missing")
    with pytest.raises(ValidationError, match="Only command checks"):
        ReadinessCheck(
            name="extra", kind="running", command=ReadinessCommand(argv=("x",))
        )
    with pytest.raises(ValidationError, match="unique"):
        vm(command_check(), command_check())
    with pytest.raises(ValidationError, match="unique"):
        vm(
            ReadinessCheck(
                name="proxmox-running", command=ReadinessCommand(argv=("x",))
            )
        )
    with pytest.raises(ValidationError, match="Only one running"):
        vm(
            ReadinessCheck(name="a", kind="running"),
            ReadinessCheck(name="b", kind="running"),
        )
    with pytest.raises(ValidationError, match="repair.after"):
        command_check(
            repair=ReadinessRepair(after=600, commands=(ReadinessCommand(argv=("x",)),))
        )
    with pytest.raises(ValidationError):
        ReadinessRepair(commands=())
    with pytest.raises(ValidationError):
        ReadinessRepair(commands=(ReadinessCommand(argv=("x",)),), max_attempts=0)


async def test_empty_checks_only_require_running():
    runner, qemu, execute, clock = runner_for(vm())
    qemu.async_proxmox.request.side_effect = [
        {"status": "stopped"},
        {"status": "running"},
    ]
    await runner.run()
    assert runner.state.phase == "ready"
    assert clock.sleeps == [2]
    qemu.ping_qemu_agent.assert_not_called()
    execute.assert_not_called()
    qemu.async_proxmox.request.assert_awaited_with(
        "GET", "/nodes/test-node/qemu/100/status/current"
    )


async def test_agent_ping_is_a_prerequisite_for_commands():
    runner, qemu, execute, _ = runner_for(vm(command_check(), is_sandbox=True))
    qemu.ping_qemu_agent.side_effect = [RuntimeError("not yet"), None]

    async def check(_):
        assert qemu.ping_qemu_agent.await_count == 2
        return result()

    execute.side_effect = check
    await runner.run()
    assert [check.attempts for check in runner.state.checks] == [1, 2, 1]


async def test_exit_code_and_stdout_must_both_match():
    check = ReadinessCheck(
        name="health",
        command=ReadinessCommand(
            argv=("health",),
            expected_exit_code=2,
            stdout_regex=r"^READY$",
        ),
    )
    runner, _, execute, clock = runner_for(vm(check))
    execute.side_effect = [
        result(0, "READY"),
        result(2, "NOT READY"),
        result(2, "READY"),
    ]
    await runner.run()
    assert clock.sleeps == [2, 3]


async def test_independent_backoffs_are_capped():
    check = command_check(retry=ReadinessRetry(interval=2, backoff=2, max_interval=5))
    runner, _, execute, clock = runner_for(vm(check))
    execute.side_effect = [result(1)] * 4 + [result()]
    await runner.run()
    assert clock.sleeps == [2, 4, 5, 5]


async def test_failed_check_times_out_without_oversleeping():
    runner, _, execute, clock = runner_for(
        vm(command_check(retry=ReadinessRetry(timeout=4)))
    )
    execute.return_value = result(1)
    with pytest.raises(
        ReadinessTimeoutError, match="database.*service.*timed out after 4s"
    ):
        await runner.run()
    assert clock.now == 4
    assert runner.state.phase == "failed"


async def test_hung_probe_is_bounded_and_cancelled():
    check = command_check(
        retry=ReadinessRetry(
            interval=0.001, max_interval=0.001, attempt_timeout=0.005, timeout=0.025
        )
    )
    runner, _, execute, _ = runner_for(vm(check))
    runner.clock = asyncio.get_running_loop().time
    runner.sleep = asyncio.sleep
    cancelled = asyncio.Event()

    async def hang(_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    execute.side_effect = hang
    with pytest.raises(ReadinessTimeoutError):
        await asyncio.wait_for(runner.run(), timeout=1)
    assert cancelled.is_set()


async def test_delayed_repair_rechecks_all_successes():
    repair = ReadinessRepair(after=300, commands=(ReadinessCommand(argv=("repair",)),))
    earlier = ReadinessCheck(
        name="earlier", command=ReadinessCommand(argv=("earlier",))
    )
    runner, qemu, execute, clock = runner_for(
        vm(earlier, command_check(repair=repair), is_sandbox=True)
    )
    repaired = False
    calls = []

    async def run(command):
        nonlocal repaired
        calls.append((command.argv[0], clock.now))
        if command.argv[0] == "repair":
            repaired = True
        return result(0 if repaired or command.argv[0] == "earlier" else 1)

    execute.side_effect = run
    await runner.run()
    assert ("repair", 300) in calls
    assert [name for name, _ in calls].count("earlier") == 2
    assert qemu.async_proxmox.request.await_count == 2
    assert qemu.ping_qemu_agent.await_count == 2
    assert runner.state.checks[-1].repairs == 1
    assert runner.state.phase == "ready"


async def test_repair_budget_does_not_establish_readiness():
    repair = ReadinessRepair(
        after=1,
        interval=2,
        max_attempts=2,
        commands=(ReadinessCommand(argv=("repair",)),),
    )
    runner, _, execute, clock = runner_for(
        vm(command_check(retry=ReadinessRetry(timeout=6), repair=repair))
    )
    repair_times = []

    async def run(command):
        if command.argv[0] == "repair":
            repair_times.append(clock.now)
            return result()
        return result(1)

    execute.side_effect = run
    with pytest.raises(ReadinessTimeoutError):
        await runner.run()
    assert repair_times == [1, 3]
    assert runner.state.checks[-1].repairs == 2


async def test_failed_repair_sequence_stops_and_probe_can_still_pass():
    repair = ReadinessRepair(
        after=0,
        commands=(
            ReadinessCommand(argv=("first",)),
            ReadinessCommand(argv=("never",)),
        ),
    )
    runner, _, execute, _ = runner_for(vm(command_check(repair=repair)))
    execute.side_effect = [result(1), result(1), result()]
    await runner.run()
    assert [call.args[0].argv for call in execute.await_args_list] == [
        ("service-health",),
        ("first",),
        ("service-health",),
    ]
    assert "command 1: exit code 1" in runner.state.checks[-1].last_repair


async def test_cancellation_stops_runner_and_in_flight_repair():
    repair = ReadinessRepair(after=0, commands=(ReadinessCommand(argv=("repair",)),))
    runner, _, execute, _ = runner_for(vm(command_check(repair=repair)))
    repairing = asyncio.Event()
    cancelled = asyncio.Event()

    async def run(command):
        if command.argv[0] != "repair":
            return result(1)
        repairing.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    execute.side_effect = run
    task = asyncio.create_task(runner.run())
    await asyncio.wait_for(repairing.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert runner.state.phase == "cancelled"


async def test_command_executor_reuses_guest_wrapper_with_timeout():
    infra = InfraCommands(
        MagicMock(), "node", MagicMock(), MagicMock(), MagicMock(), MagicMock()
    )
    infra.qemu_commands.async_proxmox.request = AsyncMock(
        return_value={"status": "running"}
    )
    infra.qemu_commands.node = "node"
    state = VmReadinessState("service", vm(command_check()), vm_id=100)
    with patch.object(
        ProxmoxSandboxEnvironment, "exec", new_callable=AsyncMock
    ) as execute:
        execute.return_value = result()
        await infra._await_vm_readiness(state, lambda: None)
    execute.assert_awaited_once_with(
        ["service-health"], timeout=30, timeout_retry=False
    )


def test_render_tree_includes_every_vm_retry_and_repair():
    runner, _, _, _ = runner_for(
        vm(
            command_check(
                repair=ReadinessRepair(commands=(ReadinessCommand(argv=("repair",)),))
            )
        )
    )
    state = runner.state
    state.phase = "checking"
    check = state.checks[-1]
    check.phase = "waiting"
    check.detail = "exit code 1"
    check.next_retry = 15
    check.next_repair = 300
    check.deadline = 600
    pending = VmReadinessState("worker", vm())
    pending.detail = "waiting for dependencies: database"
    tree = render_readiness([state, pending], 10)
    assert "database (ID=100): NOT READY" in tree
    assert "  service: waiting: exit code 1; retry in 5.0s" in tree
    assert "repair in 290.0s; repairs 0/1" in tree
    assert "worker: NOT READY / pending" in tree
    assert "waiting for dependencies: database" in tree


async def test_display_snapshots_and_durable_repair_events():
    output = StringIO()
    console = Console(file=output, width=200, color_system=None)
    state = VmReadinessState("vm", vm(command_check()), vm_id=100)
    display = ReadinessDisplay([state], "sample-1", console=console)
    async with display:
        state.checks[-1].phase = "repairing"
        state.checks[-1].detail = "repair attempt 1/1"
        display.changed()
        state.phase = "ready"
    text = output.getvalue()
    assert "sample-1" in text
    assert "NOT READY" in text
    assert "repair attempt 1/1" in text
    assert "vm (ID=100): READY" in text
    assert display.task is not None and display.task.done()


async def test_public_status_does_not_echo_exception_secrets():
    runner, _, execute, _ = runner_for(
        vm(command_check(retry=ReadinessRetry(timeout=1)))
    )
    execute.side_effect = RuntimeError("secret-token")
    with pytest.raises(ReadinessTimeoutError) as exc:
        await runner.run()
    assert "secret-token" not in str(exc.value)
    assert "secret-token" not in render_readiness([runner.state], 1)


async def test_checks_have_independent_retry_schedules_and_latch_success():
    checks = tuple(
        ReadinessCheck(
            name=name,
            command=ReadinessCommand(argv=(name,)),
            retry=ReadinessRetry(interval=interval, max_interval=interval),
        )
        for name, interval in [("fast", 1), ("slow", 3)]
    )
    runner, _, execute, clock = runner_for(vm(*checks))
    calls = []

    async def run(command):
        calls.append((command.argv[0], clock.now))
        return result(0 if clock.now >= 2 else 1)

    execute.side_effect = run
    await runner.run()
    assert calls == [("fast", 0), ("slow", 0), ("fast", 1), ("fast", 2), ("slow", 3)]


async def test_hung_repair_is_bounded_then_readiness_is_rechecked():
    repair = ReadinessRepair(
        after=0, timeout=0.005, commands=(ReadinessCommand(argv=("repair",)),)
    )
    runner, _, execute, _ = runner_for(vm(command_check(repair=repair)))
    cancelled = asyncio.Event()

    async def run(command):
        if command.argv[0] != "repair":
            return result(0 if cancelled.is_set() else 1)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    execute.side_effect = run
    await asyncio.wait_for(runner.run(), 1)
    assert cancelled.is_set()
    assert "TimeoutError" in runner.state.checks[-1].last_repair
    assert runner.state.phase == "ready"


async def test_revalidation_does_not_reset_repair_attempt_budget():
    checks = tuple(
        ReadinessCheck(
            name=name,
            command=ReadinessCommand(argv=(name,)),
            retry=ReadinessRetry(timeout=10),
            repair=ReadinessRepair(
                after=after,
                commands=(ReadinessCommand(argv=(f"repair-{name}",)),),
            ),
        )
        for name, after in [("a", 0), ("b", 3)]
    )
    runner, _, execute, _ = runner_for(vm(*checks))
    healthy = {"a": False, "b": False}
    repairs = []

    async def run(command):
        name = command.argv[0]
        if name.startswith("repair-"):
            repairs.append(name)
            healthy[name[-1]] = True
            if name == "repair-b":
                healthy["a"] = False
            return result()
        return result(0 if healthy[name] else 1)

    execute.side_effect = run
    with pytest.raises(ReadinessTimeoutError):
        await runner.run()
    assert repairs == ["repair-a", "repair-b"]


async def test_live_display_can_share_an_existing_rich_display(monkeypatch):
    monkeypatch.setattr("inspect_ai.util.display_type", lambda: "rich")
    console = Console(file=StringIO(), force_terminal=True, width=100)
    state = VmReadinessState("vm", vm())
    with Live("Inspect", console=console, auto_refresh=False) as outer:
        async with ReadinessDisplay([state], "test", console=console):
            state.phase = "ready"
        assert outer.is_started


async def test_older_rich_falls_back_to_snapshots(monkeypatch):
    monkeypatch.setattr("inspect_ai.util.display_type", lambda: "rich")
    monkeypatch.setattr(
        Live, "start", MagicMock(side_effect=LiveError("nested display"))
    )
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=100)
    state = VmReadinessState("vm", vm())
    async with ReadinessDisplay([state], "test", console=console) as display:
        assert display.live is None
    assert "NOT READY" in output.getvalue()


async def test_display_none_suppresses_status_output(monkeypatch):
    monkeypatch.setattr("inspect_ai.util.display_type", lambda: "none")
    output = StringIO()
    console = Console(file=output)
    state = VmReadinessState("vm", vm())
    async with ReadinessDisplay([state], "test", console=console) as display:
        state.checks[0].phase = "ready"
        display.changed()
    assert output.getvalue() == ""
    assert display.task is None
