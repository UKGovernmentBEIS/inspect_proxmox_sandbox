"""Runner-side helpers for the malicious-QGA e2e scenarios.

- `install_malicious_agent` deploys `_shim.py` + a scenario into a live guest via
  the honest provider, then swaps `qemu-ga` for the shim (systemd-run, outside the
  agent's cgroup so stopping the agent doesn't kill the shim).
- `RequestRecorder` wraps `AsyncProxmoxAPI.request` on a live instance to capture
  every (method, path) the provider sends, so URL injection is asserted
  mechanically rather than by eyeballing pveproxy logs.
- `b64` / scenario builders keep the shim dumb: out-data/err-data are base64 here
  because PVE's exec-status handler base64-decodes them before the provider sees them.
"""

import base64
import contextlib
import json
from pathlib import Path

_SHIM_SRC = (Path(__file__).parent / "_shim.py").read_bytes()

# Delay before the shim takes over, so the env.exec that launches it can finish
# talking to the still-running honest agent first.
_SWAP_DELAY_S = 3


def b64(data) -> str:
    if isinstance(data, str):
        data = data.encode()
    return base64.b64encode(data).decode()


async def install_malicious_agent(env, scenario: dict) -> None:
    """Deploy the shim into `env`'s guest and swap it in for the real qemu-ga."""
    await env.write_file("/root/_shim.py", _SHIM_SRC)
    await env.write_file("/root/scenario.json", json.dumps(scenario))
    # Transient unit: its own cgroup, so stopping qemu-guest-agent from inside it
    # doesn't take the shim down with the agent. Mask + kill so the real agent
    # can't restart and race the shim for the single-reader virtio port.
    swap = (
        f"sleep {_SWAP_DELAY_S}; "
        "systemctl stop qemu-guest-agent 2>/dev/null; "
        "systemctl mask qemu-guest-agent 2>/dev/null; "
        "pkill -9 -x qemu-ga 2>/dev/null; sleep 1; "
        "exec python3 /root/_shim.py /root/scenario.json"
    )
    await env.exec(
        ["systemd-run", "--collect", "--unit=malqga", "sh", "-c", swap]
    )
    # Wait past the swap so the next provider call hits the shim.
    import asyncio

    await asyncio.sleep(_SWAP_DELAY_S + 3)


async def read_shim_log(env) -> str:
    """Read the shim's own request log back through the shim (path-keyed)."""
    try:
        data = await env.read_file("/root/shim.log", text=False)
    except Exception as e:  # noqa: BLE001
        return f"<no shim log: {e!r}>"
    return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data


class RequestRecorder:
    """Records (method, path) of every AsyncProxmoxAPI.request call on `env`."""

    def __init__(self, env):
        self._api = env.agent_commands.async_proxmox
        self._orig = None
        self.calls: list[tuple[str, str]] = []

    def __enter__(self):
        orig = self._api.request

        async def _wrapped(method, path, *a, **k):
            self.calls.append((method, path))
            return await orig(method, path, *a, **k)

        self._orig = orig
        self._api.request = _wrapped  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc):
        if self._orig is not None:
            self._api.request = self._orig  # type: ignore[method-assign]

    def paths(self) -> list[str]:
        return [p for _, p in self.calls]


# --- scenario builders -----------------------------------------------------


def scenario(
    *,
    exec_return=None,
    exec_status_return=None,
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: str = "0",
    file_read_overrides: dict | None = None,
) -> dict:
    """Build a scenario dict for the shim.

    Defaults model an honest command: pid, exited=1/exitcode=0, and canned
    stdout/stderr/returncode files. Override any piece to attack a field.
    """
    responses = {}
    responses["guest-exec"] = exec_return or {"return": {"pid": 9999}}
    responses["guest-exec-status"] = exec_status_return or {
        "return": {"exited": 1, "exitcode": 0}
    }
    file_read_by_path = {
        "script.stdout": _read_reply(stdout),
        "script.stderr": _read_reply(stderr),
        "script.returncode": _read_reply(returncode.encode()),
        "__default__": _read_reply(b""),
    }
    if file_read_overrides:
        file_read_by_path.update(file_read_overrides)
    return {"responses": responses, "file_read_by_path": file_read_by_path}


def _read_reply(raw: bytes, *, eof: bool = True, count: int | None = None) -> dict:
    return {
        "return": {
            "buf-b64": base64.b64encode(raw).decode(),
            "count": len(raw) if count is None else count,
            "eof": eof,
        }
    }


def raw_read_reply(*, buf_b64: str, count: int, eof: bool) -> dict:
    """A file-read reply with fully guest-chosen fields (for truncation lies)."""
    return {"return": {"buf-b64": buf_b64, "count": count, "eof": eof}}


@contextlib.contextmanager
def nullcontext_recorder(env):
    with RequestRecorder(env) as r:
        yield r
