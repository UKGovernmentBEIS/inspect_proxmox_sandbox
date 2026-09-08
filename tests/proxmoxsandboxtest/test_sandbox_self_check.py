"""Runs inspect_ai's portable sandbox conformance suite against Proxmox.

Linux only; self_check uses Linux-specific paths and commands.
"""

from typing import AsyncGenerator

import pytest
from inspect_ai.util import OutputLimitExceededError, SandboxEnvironment

# Pull the portable check functions into this module so pytest collects them as
# tests, each driven by the `sandbox_env` fixture below.
from inspect_ai.util._sandbox.self_check import *  # noqa: F401, F403

from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironment,
    ProxmoxSandboxEnvironmentConfig,
)

from .proxmox_sandbox_utils import setup_requests_logging

pytestmark = pytest.mark.req_proxmox

# Known failures, applied as strict xfails in the sandbox_env fixture (keyed on
# the running check's function name).
_XFAILS = {
    "test_read_file_not_allowed": "user is root, so this doesn't work",
    "test_write_text_file_without_permissions": "user is root",
    "test_write_binary_file_without_permissions": "user is root",
}


@pytest.fixture(scope="module", autouse=True)
def reset_global_pool_state():
    # Shadows conftest's function-scoped autouse fixture: sample_cleanup at
    # module teardown needs the pool created by task_init, so we must not
    # clear pool state between checks.
    ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()
    InfraCommands._instances.clear()
    yield
    ProxmoxSandboxEnvironment.proxmox_pool.clear_pools()
    InfraCommands._instances.clear()


@pytest.fixture(scope="module")
async def proxmox_env() -> AsyncGenerator[ProxmoxSandboxEnvironment, None]:
    setup_requests_logging()
    task_name = "self_check"
    config = ProxmoxSandboxEnvironmentConfig()
    await ProxmoxSandboxEnvironment.task_init(task_name=task_name, config=None)
    envs_dict = await ProxmoxSandboxEnvironment.sample_init(
        task_name=task_name, config=config, metadata={}
    )
    default_env = envs_dict["default"]
    assert isinstance(default_env, ProxmoxSandboxEnvironment)

    yield default_env

    await ProxmoxSandboxEnvironment.sample_cleanup(
        task_name=task_name,
        config=config,
        environments=envs_dict,
        interrupted=False,
    )


@pytest.fixture
async def sandbox_env(
    request: pytest.FixtureRequest, proxmox_env: ProxmoxSandboxEnvironment
) -> ProxmoxSandboxEnvironment:
    reason = _XFAILS.get(request.node.originalname)
    if reason is not None:
        request.node.add_marker(pytest.mark.xfail(reason=reason, strict=True))
    return proxmox_env


async def test_read_and_write_large_file_binary(  # type: ignore[no-redef]
    sandbox_env: SandboxEnvironment,
) -> None:
    # Shadows the portable check (same name wins at collection). Proxmox's QGA
    # file-read API is hard-limited to 16 MiB; the portable check writes 50 MiB.
    # read_file() caps at 16 MiB (a documented deviation from Inspect's 100 MiB
    # spec, see read_file in _proxmox_sandbox_environment) — but it must fail
    # *gracefully* (OutputLimitExceededError), not with a raw 597.
    file_name = "test_read_and_write_large_file_binary.file"
    long_bytes = b"\xc3" * (50 * 1024 * 1024)
    await sandbox_env.write_file(file_name, long_bytes)
    with pytest.raises(OutputLimitExceededError):
        await sandbox_env.read_file(file_name, text=False)
    res = await sandbox_env.exec(["rm", "-f", "--", file_name])
    assert res.success
