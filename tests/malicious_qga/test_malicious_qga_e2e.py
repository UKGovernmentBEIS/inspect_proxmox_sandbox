"""End-to-end malicious-guest scenarios against a real Proxmox instance.

Each test gets a fresh VM (function-scoped fixture), swaps the honest qemu-ga for
`_shim.py` serving an attacker-chosen reply, then drives the provider's normal
entry points and asserts the guest-controlled value is caught at the trust
boundary as GuestAgentTamperError - not as a URL injection, an unattributable
crash (AssertionError/ValueError/TypeError), or a silent bad result.

These fail pre-patch (the raw value reaches the sink) and pass post-patch. They
are the reachability half of SINKS.md: they prove which type-confusions actually
survive Proxmox's own transforms.
"""

import pytest

from proxmoxsandbox._impl.qga_responses import GuestAgentTamperError
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment

from .harness import (
    b64,
    install_malicious_agent,
    raw_read_reply,
    read_shim_log,
    scenario,
)

pytestmark = pytest.mark.req_proxmox


def _skip_windows(env):
    if env._is_windows():
        pytest.skip("malicious shim is Linux-only")


async def test_string_pid_is_caught_before_url(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment,
) -> None:
    """Seed finding: a string pid must not reach the exec-status URL query."""
    env = proxmox_sandbox_environment
    _skip_windows(env)

    await install_malicious_agent(
        env,
        scenario(exec_return={"return": {"pid": "1 OR 1=1"}}),
    )

    with pytest.raises(GuestAgentTamperError) as ei:
        await env.exec(["true"], timeout=30)
    assert ei.value.field == "pid"
    print("\nshim log:\n", await read_shim_log(env))


async def test_string_exitcode_is_caught(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment,
) -> None:
    """A string exitcode in the wrapper-error branch must not become a returncode."""
    env = proxmox_sandbox_environment
    _skip_windows(env)

    await install_malicious_agent(
        env,
        scenario(
            exec_status_return={
                "return": {
                    "exited": 1,
                    "exitcode": "2",
                    "err-data": b64("boom"),
                    "out-data": b64(""),
                }
            }
        ),
    )

    with pytest.raises(GuestAgentTamperError) as ei:
        await env.exec(["true"], timeout=30)
    assert ei.value.field == "exec-status"


async def test_string_signal_is_caught_before_arithmetic(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment,
) -> None:
    """A string signal must not reach `128 + signal` (pre-patch: TypeError)."""
    env = proxmox_sandbox_environment
    _skip_windows(env)

    await install_malicious_agent(
        env,
        scenario(
            exec_status_return={"return": {"exited": 1, "signal": "9"}},
            # Empty returncode file forces the pre-patch 128+signal path.
            file_read_overrides={
                "script.returncode": raw_read_reply(buf_b64="", count=0, eof=True)
            },
        ),
    )

    with pytest.raises(GuestAgentTamperError) as ei:
        await env.exec(["true"], timeout=30)
    assert ei.value.field == "exec-status"


async def test_garbage_returncode_file_is_caught(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment,
) -> None:
    """A non-integer returncode file must not reach int() (pre-patch: ValueError)."""
    env = proxmox_sandbox_environment
    _skip_windows(env)

    await install_malicious_agent(
        env,
        scenario(stdout=b"", returncode="rm -rf /"),
    )

    with pytest.raises(GuestAgentTamperError) as ei:
        await env.exec(["true"], timeout=30)
    assert ei.value.field == "returncode"
