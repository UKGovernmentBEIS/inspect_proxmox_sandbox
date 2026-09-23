"""A stalled guest cannot keep a provider call alive by choosing a stage."""

import asyncio
import base64
import json
from functools import partial
from unittest.mock import patch

import httpx
import pytest

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI

from .guest_agent_fixture import (
    Fault,
    GuestReplies,
    Scenario,
    exercise,
    isolated,
    make_sandbox,
)


@pytest.mark.parametrize(
    "stage", ["upload", "launch", "status", "stdout", "stderr", "returncode", "cleanup"]
)
def test_command_stops_waiting_for_a_stalled_guest_at_every_stage(stage):
    observed = isolated(Scenario(operation="exec", fault=Fault(stage=stage)))
    assert observed.kind == "timeout"
    assert observed.cancelled is True


@pytest.mark.parametrize("mode", ["stall", "late_success"])
def test_file_read_does_not_outlive_its_waiting_allowance(mode):
    observed = isolated(Scenario(fault=Fault(stage="read", mode=mode)))
    assert observed.kind == "timeout"


def test_a_command_result_arriving_after_the_budget_is_not_reported_as_success():
    observed = isolated(
        Scenario(operation="exec", fault=Fault(stage="cleanup", mode="late_success"))
    )
    assert observed.kind == "timeout"


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
async def test_invalid_wait_configuration_is_rejected(value: str):
    observed = await exercise(
        Scenario(operation="exec", timeout=None, untimed_wait=value)
    )
    assert observed.kind == "error"
    assert observed.error_type == "ValueError"


@pytest.mark.parametrize("input", [None, "x" * 100_000], ids=["small", "large-input"])
async def test_explicit_timeout_does_not_depend_on_the_untimed_setting(input):
    observed = await exercise(
        Scenario(operation="exec", untimed_wait="invalid", input=input)
    )
    assert observed.kind == "exec"
    assert observed.returncode == 0


def test_large_input_upload_and_its_cleanup_cannot_restart_the_command_budget():
    observed = isolated(
        Scenario(
            operation="exec",
            input="x" * 100_000,
            faults=[
                Fault(stage="upload", skip_requests=2),
                Fault(stage="launch", skip_requests=2),
            ],
        )
    )
    assert observed.kind == "timeout"


def test_time_spent_on_earlier_upload_chunks_counts_toward_the_deadline():
    observed = isolated(
        Scenario(
            operation="exec",
            os_type="win11",
            command=["echo", "x" * 100_000],
            faults=[
                Fault(stage="upload", elapsed_seconds=100, mode="late_success"),
                Fault(stage="upload", elapsed_seconds=100, mode="late_success"),
            ],
        )
    )
    assert observed.kind == "timeout"
    assert observed.launched is False


def test_untimed_command_stops_waiting_even_when_upload_keeps_progressing(monkeypatch):
    monkeypatch.delenv("PROXMOX_EXEC_UNTIMED_WAIT_SECONDS", raising=False)
    observed = isolated(
        Scenario(
            operation="exec",
            timeout=None,
            os_type="win11",
            command=["echo", "x" * (4 * 1024 * 1024)],
            faults=[
                Fault(stage="upload", mode="late_success", elapsed_seconds=300)
                for _ in range(100)
            ],
        )
    )
    assert observed.kind == "timeout"
    assert observed.launched is False


@pytest.mark.parametrize("stage", ["iso_detach", "iso_delete"])
def test_iso_cleanup_cannot_hold_the_command_open_indefinitely(stage):
    observed = isolated(
        Scenario(
            operation="exec",
            command=["echo", "x" * 200_000],
            iso_uploads=True,
            faults=[Fault(stage="status"), Fault(stage=stage)],
        )
    )
    assert observed.kind == "timeout"


async def test_cancelled_iso_transfer_is_not_reused_by_the_next_write():
    replies = GuestReplies(
        Scenario(operation="exec", iso_uploads=True, fault=Fault(stage="iso_attach"))
    )
    api = AsyncProxmoxAPI("proxmox.test:8006", "test-user", "test-password")
    sandbox = make_sandbox(api)
    client = partial(httpx.AsyncClient, transport=httpx.MockTransport(replies.handle))

    async def wait_for_attach():
        while replies.seen.get("iso_attach", 0) == 0:
            await asyncio.sleep(0.01)

    with patch("httpx.AsyncClient", client):
        first = asyncio.create_task(sandbox.write_file("/first", b"x" * 200_000))
        try:
            await asyncio.wait_for(wait_for_attach(), 5)
        finally:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first, 5)
        await asyncio.wait_for(sandbox.write_file("/second", b"y" * 200_000), 10)

    assert replies.seen["iso_attach"] == 1
    assert replies.seen.get("upload", 0) > 0


@pytest.mark.parametrize("stage", ["iso_detach", "iso_delete"])
async def test_slow_successful_iso_cleanup_preserves_the_fast_transfer(stage):
    replies = GuestReplies(
        Scenario(
            operation="exec",
            iso_uploads=True,
            fault=Fault(stage=stage, mode="late_success", elapsed_seconds=45),
        )
    )
    api = AsyncProxmoxAPI("proxmox.test:8006", "test-user", "test-password")
    sandbox = make_sandbox(api)
    client = partial(httpx.AsyncClient, transport=httpx.MockTransport(replies.handle))

    with patch("httpx.AsyncClient", client), patch("time.monotonic", replies.now):
        await sandbox.write_file("/first", b"x" * 200_000)
        await sandbox.write_file("/second", b"y" * 200_000)

    assert replies.seen["iso_attach"] == 2
    assert replies.seen.get("upload", 0) == 0


async def test_slow_temporary_file_cleanup_is_allowed_to_finish():
    class CleanupReplies(GuestReplies):
        def __init__(self):
            super().__init__(Scenario(operation="exec"))
            self.scripts: dict[str, bytes] = {}
            self.cleaning = False
            self.cleanup_polls = 0
            self.cleanup_finished = False

        async def handle(self, request: httpx.Request) -> httpx.Response:
            route = request.url.path.rsplit("/", 1)[-1]
            if route == "file-write":
                data = json.loads(request.content)
                self.scripts[data["file"]] = base64.b64decode(data["content"])
            elif route == "exec":
                self.cleaning = any(
                    b"rm -rf " in self.scripts.get(arg, b"")
                    for arg in json.loads(request.content)["command"]
                )
            elif route == "exec-status" and self.cleaning:
                self.cleanup_polls += 1
                if self.cleanup_polls == 1:
                    self.elapsed += 45
                    return httpx.Response(200, json={"data": {"exited": 0}})
                self.cleanup_finished = True
            return await super().handle(request)

    replies = CleanupReplies()
    api = AsyncProxmoxAPI("proxmox.test:8006", "test-user", "test-password")
    sandbox = make_sandbox(api)
    client = partial(httpx.AsyncClient, transport=httpx.MockTransport(replies.handle))
    with patch("httpx.AsyncClient", client), patch("time.monotonic", replies.now):
        await sandbox.write_file("/sample", b"x" * 100_000)

    assert replies.cleanup_finished
