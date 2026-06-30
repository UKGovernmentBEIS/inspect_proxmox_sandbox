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
    """A SIGTERM-handling command outliving its timeout must not raise RetryError."""
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
        pytest.fail(f"exec leaked tenacity.RetryError: {ex}")


# 😀 (U+1F600): a 4-byte UTF-8 character, so the recovered/decoded output is real
# text rather than the mangled byte form that an earlier version of this test asserted.
_EMOJI = "\U0001f600"
_EMOJI_BYTES = _EMOJI.encode("utf-8")  # b"\xf0\x9f\x98\x80"


async def test_exec_10mb_limit(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment,
) -> None:
    # Output over MAX_EXEC_OUTPUT_SIZE (10 MiB) must truncate cleanly and surface
    # as OutputLimitExceededError. The read uses decode=0 (raw bytes), so the
    # truncated output is whole bytes decoded as UTF-8 - multi-byte sequences
    # round-trip and a cut inside a 4-byte char leaves at most one trailing
    # U+FFFD, never a parse crash (the byte-boundary fragility of decode=1, and
    # its even/odd-offset test, are gone).
    if proxmox_sandbox_environment._is_windows():
        pytest.skip("multi-byte output repro is Linux-only")

    count = pow(2, 20) * 3  # 3 Mi emoji * 4 bytes = 12 MiB, well over the 10 MiB limit
    escaped = "".join(f"\\x{b:02x}" for b in _EMOJI_BYTES)
    exec_cmd = [
        "perl",
        "-e",
        f'binmode STDOUT; print "{escaped}" x {count}',
    ]

    with pytest.raises(OutputLimitExceededError) as exc_info:
        await proxmox_sandbox_environment.exec(exec_cmd, timeout=120)

    truncated = exc_info.value.truncated_output
    assert isinstance(truncated, str)
    assert truncated.rstrip("�").strip(_EMOJI) == ""
    assert len(truncated) > 1_000_000


async def test_exec_large_binary_output_surfaces_cleanly(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment,
) -> None:
    """Oversized exec output must raise OutputLimitExceededError, not crash.

    Regression for the "597 Broken pipe" sample crash: the file-read of
    script.stdout was issued without a `count` cap, so PVE tried to return the
    whole (binary, base64-inflated) stdout in one JSON body and the
    pveproxy->pvedaemon hop tore it down with a non-standard "597 Broken pipe"
    that escaped the retry layer and propagated. High-entropy binary is the
    worst case (it inflates ~2x on the wire), so this reproduces the original
    `cat <binary>` shape rather than plain ASCII. With the read capped at
    max_size the response stays deliverable and the overflow surfaces cleanly.
    """
    if proxmox_sandbox_environment._is_windows():
        pytest.skip("binary stdout repro is Linux-only")

    # 30 MiB of random bytes straight to stdout, well over the 10 MiB exec cap.
    with pytest.raises(OutputLimitExceededError):
        await proxmox_sandbox_environment.exec(
            ["sh", "-c", "head -c 31457280 /dev/urandom"], timeout=120
        )


async def test_read_file_large_binary_surfaces_cleanly(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment,
) -> None:
    """read_file round-trips a multi-MiB binary file and rejects oversized ones.

    Same "597 Broken pipe" root cause as the exec regression, on the read_file
    path: a >~10 MiB binary read used to inflate past the proxy hop's limit and
    crash. The fix reads via decode=0 (compact, content-independent body) so a
    full 16 MiB read stays deliverable and an oversized file raises
    OutputLimitExceededError instead. The 14 MiB case also guards the decode=0
    segment-decode (PVE concatenates per-1 MiB-chunk base64).
    """
    if proxmox_sandbox_environment._is_windows():
        pytest.skip("binary repro is Linux-only")
    env = proxmox_sandbox_environment

    # 14 MiB binary (multi-chunk, not 3-aligned) must round-trip byte-exact.
    await env.exec(
        ["sh", "-c", "head -c 14680064 /dev/urandom > /tmp/rf14"], timeout=60
    )
    md5_guest = (await env.exec(["md5sum", "/tmp/rf14"])).stdout.split()[0]
    data = await env.read_file("/tmp/rf14", text=False)
    assert isinstance(data, bytes) and len(data) == 14680064
    assert hashlib.md5(data).hexdigest() == md5_guest

    # 20 MiB (over the 16 MiB cap) must fail cleanly, not crash with a 597.
    await env.exec(
        ["sh", "-c", "head -c 20971520 /dev/urandom > /tmp/rf20"], timeout=60
    )
    with pytest.raises(OutputLimitExceededError):
        await env.read_file("/tmp/rf20", text=False)


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
        # Proxmox's QGA file-read API is hard-limited to 16 MiB; this self_check
        # writes 50 MiB. read_file() caps at 16 MiB (a documented deviation from
        # Inspect's 100 MiB spec, see read_file in _proxmox_sandbox_environment).
        "test_read_and_write_large_file_binary",
    ]

    results = await check_results_of_self_check(
        proxmox_sandbox_environment, known_failures
    )

    # The 16 MiB hard cap means we can't round-trip the 50 MiB file this test
    # writes, so it stays a known failure - but it must fail *gracefully* with
    # OutputLimitExceededError, not a raw transport crash. That distinction is
    # exactly what the original "597 Broken pipe" sample crash slipped through:
    # the known-failure exclusion was outcome-blind.
    large_file_result = results["test_read_and_write_large_file_binary"]
    assert "OutputLimitExceededError" in str(large_file_result), (
        "Oversized read_file must fail with OutputLimitExceededError, got: "
        f"{large_file_result!r}"
    )


async def test_exec_self_kill_degrades_gracefully(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment,
) -> None:
    if proxmox_sandbox_environment._is_windows():
        pytest.skip("signal kill is POSIX-only")

    result = await proxmox_sandbox_environment.exec(["sh", "-c", "pkill -f script.sh"])
    assert result.success is False
    assert result.returncode in (143, 137)  # 128+SIGTERM / 128+SIGKILL

    after = await proxmox_sandbox_environment.exec(["echo", "alive"])
    assert after.success and after.stdout.strip() == "alive"


async def check_results_of_self_check(sandbox_env, known_failures=[]):
    self_check_results = await self_check(sandbox_env)
    failures = []
    for test_name, result in self_check_results.items():
        if result is not True and test_name not in known_failures:
            failures.append(f"Test {test_name} failed: {result}")
    if failures:
        assert False, "\n".join(failures)
    return self_check_results
