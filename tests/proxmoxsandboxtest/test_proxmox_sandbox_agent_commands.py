import hashlib
from pathlib import Path
from typing import List

import pytest
from inspect_ai.util._sandbox.self_check import self_check

from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment

from .proxmox_sandbox_utils import setup_requests_logging


async def test_exec_10mb_limit(
    proxmox_sandbox_environment: ProxmoxSandboxEnvironment,
) -> None:
    num_chars = (
        pow(2, 20) * 10 - 1000
    )  # 10 MiB - 1000, there are vagaries around the extra from JSON marshalling

    if proxmox_sandbox_environment._is_windows():
        exec_cmd = [
            "powershell",
            "-Command",
            f"Write-Host -NoNewline ('a' * {num_chars})",
        ]
    else:
        exec_cmd = ["perl", "-E", f"print 'a' x {num_chars}"]

    exec_result = await proxmox_sandbox_environment.exec(exec_cmd, timeout=120)
    assert len(exec_result.stdout) == num_chars
    assert exec_result.stdout == "a" * num_chars


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
