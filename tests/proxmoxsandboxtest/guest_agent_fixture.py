"""HTTP peer and process isolation for guest-agent regression tests."""

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from tempfile import TemporaryFile
from typing import Literal
from unittest.mock import patch

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from proxmoxsandbox._impl.agent_commands import AgentCommands
from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._impl.qga_responses import GuestAgentTamperError
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import VmConfig, VmSourceConfig


class Fault(BaseModel):
    """An external request that stalls or returns after time has elapsed."""

    stage: Literal[
        "upload",
        "launch",
        "status",
        "stdout",
        "stderr",
        "returncode",
        "cleanup",
        "read",
        "iso_attach",
        "iso_detach",
        "iso_delete",
    ]
    mode: Literal["stall", "late_success"] = "stall"
    elapsed_seconds: float = Field(default=3600, gt=0)
    skip_requests: int = Field(default=0, ge=0)


class Scenario(BaseModel):
    """Proxmox-facing replies and the public provider operation to exercise."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["read_file", "exec"] = "read_file"
    file_reply: JsonValue = {"content": ""}
    exec_reply: JsonValue = {"pid": 42}
    status_reply: JsonValue = {"exited": 1, "exitcode": 0}
    status_replies: list[JsonValue] = []
    read_limit: int | None = Field(default=None, gt=0)
    stdout: str = "hello\n"
    stderr: str = ""
    return_code: str = "0"
    os_type: Literal["l26", "win11"] = "l26"
    release: str = "9.2"
    fault: Fault | None = None
    timeout: int | None = 5
    untimed_wait: str | None = None
    http_error: str | None = None
    input: str | None = None
    command: list[str] = ["echo", "hello"]
    local_uploads: bool = False
    faults: list[Fault] = []


class Observation(BaseModel):
    """Visible result, or the failure reported by the provider."""

    kind: Literal["file", "exec", "rejected", "timeout", "error"]
    content: str | None = None
    truncated: bool | None = None
    returncode: int | None = None
    success: bool | None = None
    stdout: str | None = None
    stderr: str | None = None
    error_type: str | None = None
    error_length: int | None = None
    vm_id: int | None = None
    cancelled: bool = False
    launched: bool = False
    status_reads: int = 0


class GuestReplies:
    """An HTTP peer; production parsing and command flow remain in use."""

    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self.monotonic = time.monotonic
        self.elapsed = 0.0
        self.cancelled = False
        self.faults = ([scenario.fault] if scenario.fault else []) + scenario.faults
        self.seen: dict[str, int] = {}
        self.status_replies = list(scenario.status_replies)

    def now(self) -> float:
        """Advance only the clock; production timeout settings stay unchanged."""
        return self.monotonic() + self.elapsed

    async def apply_fault(self, stage: str) -> None:
        self.seen[stage] = self.seen.get(stage, 0) + 1
        fault = next(
            (
                f
                for f in self.faults
                if f.stage == stage and f.skip_requests < self.seen[stage]
            ),
            None,
        )
        if fault is None:
            return
        self.faults.remove(fault)
        self.elapsed += fault.elapsed_seconds
        if fault.mode == "stall":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def handle(self, request: httpx.Request) -> httpx.Response:
        route = request.url.path.rsplit("/", 1)[-1]
        scenario = self.scenario
        if route == "ticket":
            data: JsonValue = {
                "ticket": "test-ticket",
                "CSRFPreventionToken": "test-csrf",
            }
        elif route == "version":
            data = {"release": scenario.release, "repoid": "test", "version": "test"}
        elif route in ("ping", "file-write"):
            if route == "file-write":
                await self.apply_fault("upload")
            data = {}
        elif route == "exec":
            command = json.loads(request.content)["command"]
            cleanup = any("rm -f " in word or "del /f /q " in word for word in command)
            await self.apply_fault("cleanup" if cleanup else "launch")
            data = scenario.exec_reply
        elif route == "exec-status":
            await self.apply_fault("status")
            if set(request.url.params) != {"pid"}:
                raise AssertionError("guest changed authenticated request parameters")
            data = (
                self.status_replies.pop(0)
                if self.status_replies
                else scenario.status_reply
            )
        elif route == "file-read":
            if scenario.http_error is not None:
                return httpx.Response(400, text=scenario.http_error)
            if scenario.operation == "read_file":
                await self.apply_fault("read")
                data = scenario.file_reply
            else:
                filename = request.url.params["file"]
                if filename.endswith("script.stdout"):
                    await self.apply_fault("stdout")
                    content = scenario.stdout
                elif filename.endswith("script.stderr"):
                    await self.apply_fault("stderr")
                    content = scenario.stderr
                elif filename.endswith("script.returncode"):
                    await self.apply_fault("returncode")
                    content = scenario.return_code
                else:
                    raise AssertionError(f"unexpected guest file: {filename}")
                data = {"content": base64.b64encode(content.encode()).decode()}
        elif route == "config":
            empty = all(
                v.startswith("none,") for v in json.loads(request.content).values()
            )
            await self.apply_fault("iso_detach" if empty else "iso_attach")
            data = {}
        elif request.method == "DELETE" and "/content/" in request.url.path:
            await self.apply_fault("iso_delete")
            data = {}
        else:
            raise AssertionError(f"unexpected HTTP request: {request.method} {route}")
        return httpx.Response(200, json={"data": data})


@asynccontextmanager
async def local_upload_peer(api: AsyncProxmoxAPI, enabled: bool):
    if not enabled:
        yield
        return

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1])
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            if b"expect: 100-continue" in headers.lower():
                writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                await writer.drain()
            await reader.readexactly(length)
            body = b'{"data": {}}'
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        api.api_base_url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        yield


def make_sandbox(
    api: AsyncProxmoxAPI, os_type: Literal["l26", "win11"] = "l26"
) -> ProxmoxSandboxEnvironment:
    """Wire the real provider collaborators to a test HTTP peer."""
    infra = InfraCommands.build(api, "test-node", "local")
    return ProxmoxSandboxEnvironment(
        infra_commands=infra,
        agent_commands=AgentCommands(api, "test-node"),
        ipam_mappings=(),
        vm_id=100,
        name="test-sandbox",
        all_vms={
            100: VmConfig(
                vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
                name="test-sandbox",
            )
        },
        sdn_zone_id=None,
        os_type=os_type,
    )


async def exercise(scenario: Scenario) -> Observation:
    """Drive the public file/command interface against a simulated HTTP peer."""
    replies = GuestReplies(scenario)
    client = partial(httpx.AsyncClient, transport=httpx.MockTransport(replies.handle))
    api = AsyncProxmoxAPI("proxmox.test:8006", "test-user", "test-password")
    sandbox = make_sandbox(api, scenario.os_type)
    settings = (
        {"PROXMOX_EXEC_UNTIMED_WAIT_SECONDS": scenario.untimed_wait}
        if scenario.untimed_wait is not None
        else {}
    )
    with (
        patch("httpx.AsyncClient", client),
        patch("time.monotonic", replies.now),
        patch.dict(os.environ, settings),
    ):
        try:
            if scenario.operation == "read_file":
                truncated = False
                if scenario.read_limit is None:
                    content = await sandbox.read_file("/sample", text=False)
                else:
                    content, truncated = await api.read_file_capped(
                        "test-node", 100, "/sample", scenario.read_limit
                    )
                if not isinstance(content, bytes):
                    raise TypeError("binary file read did not return bytes")
                return Observation(
                    kind="file",
                    content=base64.b64encode(content).decode(),
                    truncated=truncated,
                )
            async with local_upload_peer(api, scenario.local_uploads):
                result = await sandbox.exec(
                    scenario.command, input=scenario.input, timeout=scenario.timeout
                )
            return Observation(
                kind="exec",
                returncode=result.returncode,
                success=result.success,
                stdout=result.stdout,
                stderr=result.stderr,
                status_reads=replies.seen.get("status", 0),
            )
        except GuestAgentTamperError as exc:
            return Observation(kind="rejected", vm_id=exc.vm_id)
        except TimeoutError:
            return Observation(
                kind="timeout",
                cancelled=replies.cancelled,
                launched=replies.seen.get("launch", 0) > 0,
            )
        except Exception as exc:
            return Observation(
                kind="error", error_type=type(exc).__name__, error_length=len(str(exc))
            )


def isolated_many(
    scenarios: list[Scenario], *, seconds: float = 5, optimized: bool = False
) -> list[Observation]:
    """An outside watchdog can interrupt a parser that blocks Python itself."""
    command = [sys.executable]
    if optimized:
        command.append("-O")
    command.extend(
        [
            "-c",
            "from tests.proxmoxsandboxtest.guest_agent_fixture import run_worker; "
            "run_worker()",
        ]
    )
    with (
        TemporaryFile(mode="w+") as errors,
        subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[2],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
        ) as process,
        ThreadPoolExecutor(max_workers=1) as reader,
    ):
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("worker pipes were not created")
        try:
            ready = reader.submit(process.stdout.readline).result(timeout=30)
            if ready.strip() != "ready":
                errors.seek(0)
                raise RuntimeError(f"worker did not start: {errors.read()}")
            observations = []
            for scenario in scenarios:
                process.stdin.write(scenario.model_dump_json() + "\n")
                process.stdin.flush()
                result = reader.submit(process.stdout.readline).result(timeout=seconds)
                observations.append(Observation.model_validate_json(result))
            return observations
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def isolated(
    scenario: Scenario, *, seconds: float = 5, optimized: bool = False
) -> Observation:
    """Replay one case with an independent deadline."""
    return isolated_many([scenario], seconds=seconds, optimized=optimized)[0]


def run_worker() -> None:
    """Handle isolated test cases after signaling that imports have finished."""
    print("ready", flush=True)
    for raw in sys.stdin:
        observation = asyncio.run(exercise(Scenario.model_validate_json(raw)))
        print(observation.model_dump_json(exclude_none=True), flush=True)
