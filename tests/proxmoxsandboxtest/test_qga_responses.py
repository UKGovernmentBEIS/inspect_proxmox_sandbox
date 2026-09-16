"""Guest-agent responses are attacker-controlled; these pin the boundary.

The live scenarios are in test_hostile_guest_agent_e2e.py; this locks the
same rules down without a Proxmox host.
"""

import base64
import time

import httpx
import pytest

from proxmoxsandbox import _proxmox_sandbox_environment as mod
from proxmoxsandbox._impl import agent_commands as ac_mod
from proxmoxsandbox._impl.agent_commands import AgentCommands
from proxmoxsandbox._impl.async_proxmox import _http_status_error
from proxmoxsandbox._impl.qga_responses import (
    MAX_EXEC_DATA_CHARS,
    ExecReturn,
    ExecStatus,
    GuestAgentTamperError,
    decode_file_read,
    parse_guest,
)

VM = 100


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# --- guest-exec ---------------------------------------------------------------


def test_exec_pid_accepts_honest_int():
    assert parse_guest(ExecReturn, VM, "exec", {"pid": 856}).pid == 856


@pytest.mark.parametrize(
    "data",
    [
        {"pid": "31337?injected=1&file="},
        {"pid": "856"},
        {"pid": True},
        {"pid": 0},
        {"pid": -1},
        {"pid": 2**31},
        {"pid": None},
        {},
        [],
        "856",
        None,
    ],
)
def test_exec_pid_rejects_anything_unfit_for_a_url(data):
    with pytest.raises(GuestAgentTamperError):
        parse_guest(ExecReturn, VM, "exec", data)


# --- guest-exec-status --------------------------------------------------------


def test_exec_status_accepts_pve_shapes():
    running = parse_guest(ExecStatus, VM, "exec-status", {"exited": 0})
    assert running.exited == 0
    done = parse_guest(
        ExecStatus,
        VM,
        "exec-status",
        {"exited": 1, "exitcode": 3, "out-data": "out\n", "err-data": "err\n"},
    )
    assert (done.exitcode, done.out_data, done.err_data) == (3, "out\n", "err\n")
    signalled = parse_guest(ExecStatus, VM, "exec-status", {"exited": 1, "signal": 9})
    assert signalled.exitcode is None and signalled.signal == 9
    # JSON booleans, should PVE ever stop converting them to 1/0
    assert parse_guest(ExecStatus, VM, "exec-status", {"exited": True}).exited == 1
    # Windows exit codes use the whole DWORD range
    assert (
        parse_guest(
            ExecStatus, VM, "exec-status", {"exited": 1, "exitcode": 0xC0000005}
        ).exitcode
        == 0xC0000005
    )


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"exited": "1"},
        {"exited": 2},
        {"exited": None},
        {"exited": 1, "exitcode": "0"},
        {"exited": 1, "exitcode": 2**33},
        {"exited": 1, "exitcode": True},
        {"exited": 1, "signal": "x"},
        {"exited": 1, "signal": 9999},
        {"exited": 1, "signal": -1},
        {"exited": 1, "err-data": 12},
        {"exited": 1, "err-data": ["x"]},
        {"exited": 1, "out-data": "x" * (MAX_EXEC_DATA_CHARS + 1)},
    ],
)
def test_exec_status_rejects_type_confusion(data):
    with pytest.raises(GuestAgentTamperError):
        parse_guest(ExecStatus, VM, "exec-status", data)


def test_tamper_error_names_vm_and_truncates_value(caplog):
    with pytest.raises(GuestAgentTamperError) as ex:
        parse_guest(ExecStatus, VM, "exec-status", {"exited": "x" * 5000})
    assert "VM 100" in str(ex.value)
    assert len(str(ex.value)) < 600
    assert any(
        "VM 100" in r.message and r.levelname == "WARNING" for r in caplog.records
    )


def test_status_returncode_falls_back_to_signal():
    assert mod._status_returncode(ExecStatus(exited=1, exitcode=3)) == 3
    assert mod._status_returncode(ExecStatus(exited=1, signal=9)) == 137
    assert mod._status_returncode(ExecStatus(exited=1)) == 1


# --- agent/file-read (decode=0) ----------------------------------------------


def test_file_read_honest_shapes():
    # Verified live on PVE 9.2: a read that fills `count` is flagged truncated,
    # a shorter one never is.
    assert decode_file_read(VM, {"content": _b64(b"A" * 99)}, 100) == (b"A" * 99, False)
    assert decode_file_read(VM, {"content": _b64(b"A" * 100), "truncated": 1}, 100) == (
        b"A" * 100,
        True,
    )
    assert decode_file_read(VM, {"content": ""}, 100) == (b"", False)
    assert decode_file_read(VM, {}, 100) == (b"", False)
    # PVE concatenates each ~1 MiB chunk's own padded base64
    chunked = _b64(b"A" * 1000) + _b64(b"B" * 500)
    assert decode_file_read(VM, {"content": chunked}, 2000) == (
        b"A" * 1000 + b"B" * 500,
        False,
    )


@pytest.mark.parametrize(
    "data",
    [
        {"content": "!!!!"},
        {"content": "A"},  # bad length
        {"content": "AAAA===="},
        {"content": "héllo"},
        {"content": 12345},
        {"content": ["QUFB"]},
        {"content": _b64(b"A" * 200)},  # body exceeds count
        {"content": _b64(b"A" * 100)},  # fills count, not flagged
        {"content": _b64(b"A"), "truncated": 1},  # flagged, did not fill count
        {"content": "", "truncated": True},
        {"content": _b64(b"A"), "truncated": "1"},
        [],
        None,
    ],
)
def test_file_read_rejects_lies(data):
    with pytest.raises(GuestAgentTamperError):
        decode_file_read(VM, data, 100)


# --- bounds -------------------------------------------------------------------


def _error(status: int, text: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://proxmox:8006/api2/json/x")
    response = httpx.Response(status, request=request, text=text)
    return _http_status_error(response)


def test_error_text_is_capped():
    err = _error(500, "Agent error: " + "X" * 100_000)
    assert len(str(err)) < 9_000
    assert "Agent error: X" in str(err)
    assert "does not exist" in str(_error(500, "Agent error: PID ld does not exist"))


async def test_retry_envelope_has_a_wall_clock_cap(monkeypatch):
    # Each attempt "takes" 2 minutes (a stalled agent): the envelope must stop
    # at the cap, well before the 25 attempts the fast-failure path gets.
    now = {"t": 0.0}
    monkeypatch.setattr(ac_mod.time, "monotonic", lambda: now["t"])

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(ac_mod.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    async def stalls():
        calls["n"] += 1
        now["t"] += 120
        raise httpx.ReadTimeout("")

    agent = AgentCommands(async_proxmox=None, node="proxmox")  # type: ignore[arg-type]
    with pytest.raises(httpx.ReadTimeout):
        await agent._retry_on_qga_error("read", stalls)
    assert calls["n"] < ac_mod._QGA_MAX_RETRIES
    assert calls["n"] * 120 <= ac_mod._QGA_RETRY_MAX_TOTAL_SECONDS + 120


def test_exec_untimed_wait_env_override(monkeypatch):
    monkeypatch.delenv("PROXMOX_EXEC_UNTIMED_WAIT_SECONDS", raising=False)
    assert mod._exec_untimed_wait() == mod._EXEC_UNTIMED_WAIT_SECONDS
    monkeypatch.setenv("PROXMOX_EXEC_UNTIMED_WAIT_SECONDS", "15")
    assert mod._exec_untimed_wait() == 15


def test_time_module_untouched():
    # guard against the monkeypatch above leaking
    assert time.monotonic() > 0
