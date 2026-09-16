"""A root process in the sandbox has replaced qemu-ga.

Each scenario boots a fresh built-in VM, uses the honest agent to install a
canned shim on the virtio port, then drives the provider's public surface
(exec / read_file) against it. The shim executes nothing: it answers
sync/ping honestly and serves one hardcoded reply per command from a JSON
file baked in at install time; a null reply means "never answer".

The provider must fail attributably (GuestAgentTamperError / TimeoutError)
and within a bound, whatever the shim says.
"""

import asyncio
import base64
import json
import time
from typing import Any

import httpx
import pytest
from inspect_ai.util._sandbox.limits import override_max_exec_output_size

from proxmoxsandbox._impl.qga_responses import GuestAgentTamperError
from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironment,
    ProxmoxSandboxEnvironmentConfig,
)

from .proxmox_sandbox_utils import setup_sandbox

pytestmark = pytest.mark.req_proxmox

# Speaks just enough of the QGA wire protocol for PVE's QMPClient: each request
# is a `guest-sync-delimited` line followed by the command line, and the reply
# is 0xff + {"return": id} + "\n" + the command's reply + "\n". PVE also pings
# before every command, so ping must stay honest.
SHIM_SOURCE = r"""
import json, os, sys, time

replies = json.load(open(sys.argv[1]))
fd = os.open("/dev/virtio-ports/org.qemu.guest_agent.0", os.O_RDWR)
buf = b""


def reply(obj, delimited=False):
    data = (b"\xff" if delimited else b"") + json.dumps(obj).encode() + b"\n"
    try:
        os.write(fd, data)
    except OSError:
        pass


while True:
    chunk = os.read(fd, 65536)
    if not chunk:
        # host side disconnected between commands
        time.sleep(0.02)
        continue
    buf += chunk
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        line = line.strip(b"\xff \r\t")
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        cmd = req.get("execute")
        args = req.get("arguments") or {}
        if cmd == "guest-sync-delimited":
            reply({"return": args.get("id")}, delimited=True)
        elif cmd == "guest-sync":
            reply({"return": args.get("id")})
        elif cmd == "guest-ping":
            reply({"return": {}})
        elif cmd in replies:
            if replies[cmd] is not None:
                reply(replies[cmd])
        else:
            reply({"error": {"class": "CommandNotFound", "desc": "shim: " + str(cmd)}})
"""

SHIM_PATH = "/root/qga_shim.py"
REPLIES_PATH = "/root/qga_shim_replies.json"
SHIM_OSINFO_ID = "qga-shim"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _file_read(data: bytes, *, count: int | None = None, eof: bool = True) -> dict:
    return {
        "return": {
            "count": len(data) if count is None else count,
            "buf-b64": _b64(data),
            "eof": eof,
        }
    }


# Enough for exec() to run end to end against an agent that executes nothing:
# the wrapper-script upload is acknowledged, exec returns a pid, exec-status
# says it finished, and every post-exit file-read (stdout/stderr/returncode)
# comes back as "0\n".
HONEST_SHAPED: dict[str, Any] = {
    "guest-get-osinfo": {"return": {"id": SHIM_OSINFO_ID, "name": SHIM_OSINFO_ID}},
    "guest-file-open": {"return": 1000},
    "guest-file-write": {"return": {"count": 0, "eof": False}},
    "guest-file-close": {"return": {}},
    "guest-exec": {"return": {"pid": 4242}},
    "guest-exec-status": {"return": {"exited": True, "exitcode": 0}},
    "guest-file-read": _file_read(b"0\n"),
}


async def _install_shim(env: ProxmoxSandboxEnvironment, overrides: dict) -> None:
    """Swap qemu-ga for the shim, using the honest agent to do it."""
    replies = {**HONEST_SHAPED, **overrides}
    await env.write_file(SHIM_PATH, SHIM_SOURCE)
    await env.write_file(REPLIES_PATH, json.dumps(replies))
    # Deferred so this exec (and its rm -f cleanup) finish over the honest
    # agent before it goes away.
    res = await env.exec(
        [
            "systemd-run",
            "--on-active=4",
            "--unit=qga-shim",
            "sh",
            "-c",
            f"systemctl stop qemu-guest-agent && exec python3 {SHIM_PATH} {REPLIES_PATH}",  # noqa: E501
        ],
        timeout=30,
    )
    assert res.success, res
    api = env.infra_commands.async_proxmox
    node = env.infra_commands.node
    deadline = time.monotonic() + 120
    while True:
        try:
            info = await api.request(
                "GET", f"/nodes/{node}/qemu/{env.vm_id}/agent/get-osinfo"
            )
            if isinstance(info, dict) and info.get("result", {}).get("id") == (
                SHIM_OSINFO_ID
            ):
                return
        except httpx.HTTPStatusError:
            pass  # agent is being swapped
        if time.monotonic() > deadline:
            raise AssertionError("qga shim did not come up")
        await asyncio.sleep(1)


@pytest.fixture
async def hostile_env(request):
    """Fresh VM with the shim installed; parametrise indirectly with overrides."""
    overrides = request.param
    task_name = "test_hostile_guest_agent_e2e"
    config = ProxmoxSandboxEnvironmentConfig()
    _, envs = await setup_sandbox(task_name, config)
    try:
        env = envs["default"]
        assert isinstance(env, ProxmoxSandboxEnvironment)
        await _install_shim(env, overrides)
        yield env
    finally:
        await ProxmoxSandboxEnvironment.sample_cleanup(
            task_name=task_name, config=config, environments=envs, interrupted=False
        )


@pytest.mark.parametrize("hostile_env", [{}], indirect=True)
async def test_honest_shaped_shim_runs_exec(hostile_env):
    """Control: the shim plumbing itself works, so failures below are the lies."""
    res = await hostile_env.exec(["true"], timeout=30)
    assert res.returncode == 0
    assert res.stdout == "0\n"


TAMPER_SCENARIOS = {
    # pid flows into the exec-status URL
    "pid_string_with_query": {
        "guest-exec": {"return": {"pid": "31337?injected=1&file="}}
    },
    "pid_bool": {"guest-exec": {"return": {"pid": True}}},
    "pid_missing": {"guest-exec": {"return": {}}},
    "pid_negative": {"guest-exec": {"return": {"pid": -1}}},
    # exec-status type confusion
    "exited_string": {"guest-exec-status": {"return": {"exited": "1", "exitcode": 0}}},
    "exitcode_string_wrapper_error": {
        "guest-exec-status": {
            "return": {"exited": True, "exitcode": "0", "err-data": _b64(b"shim")}
        }
    },
    "signal_string": {
        "guest-exec-status": {"return": {"exited": True, "signal": "x"}},
        "guest-file-read": _file_read(b""),
    },
    "signal_out_of_range": {
        "guest-exec-status": {"return": {"exited": True, "signal": 9999}},
        "guest-file-read": _file_read(b""),
    },
    # file-read lies
    "content_not_base64": {
        "guest-file-read": {"return": {"count": 4, "buf-b64": "!!!!", "eof": True}}
    },
    "content_not_string": {
        "guest-file-read": {"return": {"count": 1, "buf-b64": 12345, "eof": True}}
    },
    # PVE sums the guest-reported count and sets truncated when eof was never
    # seen, so a 1-byte body arrives flagged truncated.
    "truncated_with_short_body": {
        "guest-file-read": _file_read(b"0", count=2**24, eof=False)
    },
}


@pytest.mark.parametrize(
    "hostile_env",
    list(TAMPER_SCENARIOS.values()),
    ids=list(TAMPER_SCENARIOS.keys()),
    indirect=True,
)
async def test_exec_rejects_tampered_agent_responses(hostile_env):
    with pytest.raises(GuestAgentTamperError):
        await hostile_env.exec(["true"], timeout=20)


OVERSIZE_SCENARIOS = {
    "content_exceeds_count": {"guest-file-read": _file_read(b"A" * 200)},
    # Exactly `count` bytes without the truncated flag: an honest PVE always
    # flags a read that filled `count` (it only clears it on eof).
    "full_body_not_flagged_truncated": {"guest-file-read": _file_read(b"A" * 100)},
}


@pytest.mark.parametrize(
    "hostile_env",
    list(OVERSIZE_SCENARIOS.values()),
    ids=list(OVERSIZE_SCENARIOS.keys()),
    indirect=True,
)
async def test_exec_rejects_file_read_size_lies(hostile_env):
    with override_max_exec_output_size(100):
        with pytest.raises(GuestAgentTamperError):
            await hostile_env.exec(["true"], timeout=20)


@pytest.mark.parametrize(
    "hostile_env",
    [{"guest-exec-status": {"return": {"exited": False}}}],
    ids=["never_exits"],
    indirect=True,
)
async def test_exec_without_timeout_is_bounded(hostile_env, monkeypatch):
    monkeypatch.setenv("PROXMOX_EXEC_UNTIMED_WAIT_SECONDS", "15")
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        # Outer guard only so a pre-patch run fails instead of hanging.
        await asyncio.wait_for(hostile_env.exec(["true"], timeout=None), 180)
    assert time.monotonic() - started < 120


@pytest.mark.parametrize(
    "hostile_env",
    [{"guest-exec-status": None}],
    ids=["stalls_exec_status"],
    indirect=True,
)
async def test_exec_with_timeout_is_bounded_when_agent_stalls(hostile_env):
    """exec(timeout=5) must return in bounded time when every status poll hangs.

    PVE times each guest command out after 5s, and the provider retries those
    as transient; without a deadline that is ~10 minutes per poll.
    """
    from proxmoxsandbox import _proxmox_sandbox_environment as mod

    budget = 5 + mod._EXEC_POLL_GRACE_SECONDS + mod._EXEC_AGENT_BUDGET_SECONDS
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(hostile_env.exec(["true"], timeout=5), budget + 120)
    assert time.monotonic() - started < budget + 60


@pytest.mark.parametrize(
    "hostile_env",
    [{"guest-file-read": _file_read(b"A" * 64, count=2**24, eof=False)}],
    ids=["read_file_truncated_lie"],
    indirect=True,
)
async def test_read_file_rejects_truncated_lie(hostile_env):
    with pytest.raises(GuestAgentTamperError):
        await hostile_env.read_file("/etc/hostname")
