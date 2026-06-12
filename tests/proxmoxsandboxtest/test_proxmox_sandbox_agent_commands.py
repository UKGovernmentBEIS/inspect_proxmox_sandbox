import hashlib
from pathlib import Path
from typing import List

import pytest
import tenacity
from inspect_ai.util import OutputLimitExceededError
from inspect_ai.util._sandbox.self_check import self_check

from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment

from .proxmox_sandbox_utils import setup_requests_logging


async def test_exec_timeout_with_sigterm_handler_no_retryerror(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment,
) -> None:
    """A SIGTERM-handling command that outlives its timeout must not raise RetryError.

    Issue #76: a command that handles SIGTERM and doesn't exit immediately
    (like `john` saving its session) used to surface an opaque tenacity
    RetryError when it ran past its timeout, because the poll deadline equalled
    the in-guest timeout and so fired while the command was still in the
    `timeout -k 5s` SIGKILL grace window. It must now surface as a clean
    TimeoutError or a terminated returncode — never RetryError.
    """
    if proxmox_sandbox_environment._is_windows():
        pytest.skip("SIGTERM handling is Linux-only")

    try:
        result = await proxmox_sandbox_environment.exec(
            ["sh", "-c", "trap 'sleep 30' TERM; while :; do :; done"],
            timeout=5,
        )
        # Force-killed by the in-guest SIGKILL after the grace period.
        assert result.returncode != 0
    except TimeoutError:
        pass  # also acceptable: surfaced as a clean timeout
    except tenacity.RetryError as ex:
        pytest.fail(f"exec leaked tenacity.RetryError (issue #76): {ex}")


@pytest.mark.parametrize("lead", ["", "a"], ids=["even-offset", "odd-offset"])
async def test_exec_10mb_limit(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment, lead: str
) -> None:
    # Output over MAX_EXEC_OUTPUT_SIZE (10 MiB) must truncate cleanly, even when
    # the cut lands in the middle of a multi-byte sequence (issue #77). The old
    # all-'a' version of this test never hit that: plain ASCII has no multi-byte
    # sequences for the cut to split, so it always parsed cleanly.
    #
    # read_file streams the file-read response in 8192-byte chunks and stops once
    # the total first exceeds 10 MiB, so the cut is at a fixed, even byte offset
    # (1281*8192 = 10493952). Each 0xC3 output byte comes back as a 2-byte sequence
    # on the wire (Proxmox re-encodes raw bytes via latin-1). Whether the cut splits
    # one of those pairs depends on the parity of the offset at which they start,
    # which we can't predict exactly (the JSON field order is non-deterministic).
    # So we run two outputs differing by a single leading byte: their pairs sit on
    # opposite parities, guaranteeing exactly one variant lands mid-sequence and
    # reproduces the crash. Pre-fix that variant raised
    # `ValueError: invalid unicode code point`; both must now truncate cleanly.
    if proxmox_sandbox_environment._is_windows():
        pytest.skip("byte-level multi-byte boundary repro is Linux-only")

    high_bytes = pow(2, 20) * 12  # 12 MiB of 0xC3, comfortably over the 10 MiB limit
    exec_cmd = [
        "perl",
        "-e",
        f'binmode STDOUT; print "{lead}", "\\xc3" x {high_bytes}',
    ]

    with pytest.raises(OutputLimitExceededError) as exc_info:
        await proxmox_sandbox_environment.exec(exec_cmd, timeout=120)

    # Partial output is still recovered: the optional ASCII lead, then U+00C3 ('Ã'),
    # with any split trailing byte dropped rather than crashing the parse.
    truncated = exc_info.value.truncated_output
    assert isinstance(truncated, str)
    assert truncated.startswith(lead)
    assert truncated[len(lead) :].strip("\xc3") == ""
    assert len(truncated) > 1_000_000


CURRENT_DIR = Path(__file__).parent


async def test_write_file_small(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment,
) -> None:
    test_content = b"Hello from test_write_file_small!"

    if proxmox_sandbox_environment._is_windows():
        dest_path = "C:\\Windows\\Temp\\test_small.txt"
    else:
        dest_path = "/tmp/test_small.txt"

    await proxmox_sandbox_environment.write_file(dest_path, test_content)
    read_back = await proxmox_sandbox_environment.read_file(dest_path, text=False)

    assert read_back == test_content


async def test_write_file_large(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment,
) -> None:
    with open(CURRENT_DIR / ".." / "oVirtTinyCore64-13.11.ova", "rb") as ova:
        file_contents = ova.read()
        # calculate md5sum of the file
        md5 = hashlib.md5()
        md5.update(file_contents)
        expected_md5 = md5.hexdigest()
        assert expected_md5 == "b6059a0fec3d0e431531abeabff212fe"

    if proxmox_sandbox_environment._is_windows():
        dest_path = "C:\\Windows\\Temp\\test_large_file.ova"
    else:
        dest_path = "test_large_file.ova"

    await proxmox_sandbox_environment.write_file(dest_path, file_contents)

    if proxmox_sandbox_environment._is_windows():
        exec_result = await proxmox_sandbox_environment.exec(
            ["certutil", "-hashfile", dest_path, "MD5"], timeout=60
        )
        assert expected_md5 in exec_result.stdout.lower()
    else:
        exec_result = await proxmox_sandbox_environment.exec(
            ["md5sum", dest_path], timeout=60
        )
        assert exec_result.stdout.startswith(expected_md5)


async def test_self_check(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment,
) -> None:
    if proxmox_sandbox_environment._is_windows():
        pytest.skip("self_check uses Linux-specific paths and commands")

    setup_requests_logging()

    known_failures: List[str] = [
        "test_read_file_not_allowed",  # user is root, so this doesn't work
        "test_write_text_file_without_permissions",  # ditto
        "test_write_binary_file_without_permissions",  # ditto
    ]

    return await check_results_of_self_check(
        proxmox_sandbox_environment, known_failures
    )


async def check_results_of_self_check(sandbox_env, known_failures=[]):
    self_check_results = await self_check(sandbox_env)
    failures = []
    for test_name, result in self_check_results.items():
        if result is not True and test_name not in known_failures:
            failures.append(f"Test {test_name} failed: {result}")
    if failures:
        assert False, "\n".join(failures)
