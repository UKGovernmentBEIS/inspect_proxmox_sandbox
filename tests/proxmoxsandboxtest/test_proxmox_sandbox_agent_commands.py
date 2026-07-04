import hashlib
from pathlib import Path
from typing import List

import httpx
import pytest
import tenacity
from inspect_ai.util import OutputLimitExceededError
from inspect_ai.util._sandbox.self_check import self_check

from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment

from .proxmox_sandbox_utils import setup_requests_logging

pytestmark = pytest.mark.proxmox


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
    # Oversized exec output must surface as OutputLimitExceededError. decode=0
    # reads whole bytes, so multi-byte output survives truncation (at most one
    # trailing U+FFFD).
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
    """Oversized (binary) exec output raises OutputLimitExceededError, not a 597."""
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
    """read_file round-trips a multi-MiB binary and rejects oversized files.

    14 MiB round-trips byte-exact (also exercises the decode=0 segment-decode);
    20 MiB (over the 16 MiB cap) raises OutputLimitExceededError, not a 597.
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


async def test_read_file_under_cap_but_wire_expanding_round_trips(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment,
) -> None:
    r"""A file under the 16 MiB cap whose content expanded on the wire (#81).

    Under the old decode=1 path the limit was counted against JSON wire bytes,
    not raw bytes. A control byte JSON-escapes to `\u00XX` (6x), so this 8 MiB
    file ballooned to ~48 MiB on the wire and tripped the output limit - a file
    well under any documented cap, rejected purely because of its content. With
    decode=0 the limit is the raw-byte `count`, so it round-trips byte-exact.
    """
    if proxmox_sandbox_environment._is_windows():
        pytest.skip("byte-level wire-expansion repro is Linux-only")
    env = proxmox_sandbox_environment

    # 8 MiB of 0x01: raw is under the 16 MiB cap, but each byte is `\u0001`
    # (6 chars) in the decode=1 JSON, so the old wire size was ~48 MiB.
    size = 8 * 1024 * 1024
    await env.exec(
        ["sh", "-c", f"head -c {size} /dev/zero | tr '\\0' '\\1' > /tmp/rf_ctrl"],
        timeout=60,
    )
    md5_guest = (await env.exec(["md5sum", "/tmp/rf_ctrl"])).stdout.split()[0]
    data = await env.read_file("/tmp/rf_ctrl", text=False)
    assert isinstance(data, bytes)
    assert data == b"\x01" * size
    assert hashlib.md5(data).hexdigest() == md5_guest


async def test_file_read_597_is_response_compression(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment,
) -> None:
    """Pin the upstream 597: it depends on negotiated response compression.

    A large incompressible body truncates mid-transfer (597) when gzip is
    accepted but delivers fine as `identity` - which is why read_file_capped
    sends Accept-Encoding: identity. decode=1 here makes the body big enough to
    trip it.
    """
    if proxmox_sandbox_environment._is_windows():
        pytest.skip("Linux-only")
    env = proxmox_sandbox_environment
    api = env.agent_commands.async_proxmox

    # ~14 MiB of incompressible random -> decode=1 body well past the cliff.
    await env.exec(
        ["sh", "-c", "head -c 14680064 /dev/urandom > /tmp/incompressible"], timeout=60
    )
    await api.request("GET", "/version")  # ensure a ticket
    url = (
        f"{api.api_base_url}/nodes/{env.agent_commands.node}"
        f"/qemu/{env.vm_id}/agent/file-read"
    )

    async def read_with(accept_encoding: str) -> httpx.Response:
        async with httpx.AsyncClient(
            verify=api.verify_tls,
            timeout=httpx.Timeout(connect=15, read=120, write=60, pool=60),
        ) as client:
            return await client.get(
                url,
                headers={
                    "Cookie": f"PVEAuthCookie={api.ticket}",
                    "Accept-Encoding": accept_encoding,
                },
                params={"file": "/tmp/incompressible", "decode": 1},
            )

    # identity: the full body is delivered.
    identity_resp = await read_with("identity")
    assert identity_resp.status_code == 200
    assert len(identity_resp.content) > 14_000_000

    # any negotiated compression: pveproxy tears the large incompressible body
    # down with a non-standard 596/597. (If a future Proxmox stops doing this,
    # this assertion flips - which is the signal that the workaround can go.)
    for accept_encoding in ("gzip", "gzip, deflate, br"):
        resp = await read_with(accept_encoding)
        assert resp.status_code in (596, 597), (
            f"expected 596/597 for Accept-Encoding={accept_encoding!r}, "
            f"got {resp.status_code}"
        )


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

    # 50 MiB can't round-trip (16 MiB cap) so it stays a known failure - but it
    # must fail *gracefully* (OutputLimitExceededError), not a raw 597. The old
    # outcome-blind exclusion is how the crash slipped through.
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
