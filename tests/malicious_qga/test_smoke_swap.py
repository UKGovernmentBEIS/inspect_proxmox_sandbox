"""Live smoke test: confirm the shim swap works before writing attack scenarios."""

import pytest

from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment

from .harness import install_malicious_agent, read_shim_log, scenario

pytestmark = pytest.mark.req_proxmox


async def test_shim_swap_honest_roundtrip(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment,
) -> None:
    env = proxmox_sandbox_environment
    if env._is_windows():
        pytest.skip("shim is Linux-only")

    await install_malicious_agent(
        env, scenario(stdout=b"SHIM_OK\n", returncode="0")
    )

    result = await env.exec(["true"], timeout=30)
    print("\n===== SHIM LOG =====")
    print(await read_shim_log(env))
    print("===== RESULT =====")
    print(repr(result))
    assert result.stdout == "SHIM_OK\n"
    assert result.returncode == 0
    assert result.success is True
