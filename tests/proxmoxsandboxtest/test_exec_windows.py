"""Check Windows rendering without treating string checks as OS enforcement."""

import base64
from typing import Any

import pytest

from proxmoxsandbox._impl import exec_windows

PREFIX = "C:\\Windows\\Temp\\proxmox_123\\"
CMD_EXE = r"C:\Windows\System32\cmd.exe"


def test_prepare_uses_fixed_powershell_and_encoded_script() -> None:
    argv = exec_windows.build_prepare(PREFIX[:-1])
    assert argv[:-1] == [
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
    ]
    assert all(argument.isascii() for argument in argv)
    script = base64.b64decode(argv[-1], validate=True).decode("utf-16le")
    assert f"$path = '{PREFIX[:-1]}'" in script


def test_prepare_creates_acl_atomically_and_rejects_foreign_objects() -> None:
    script = base64.b64decode(exec_windows.build_prepare(PREFIX[:-1])[-1]).decode(
        "utf-16le"
    )
    assert "SetSecurityDescriptorSddlForm('O:SYG:SYD:P(A;OICI;FA;;;SY)')" in script
    assert "[IO.Directory]::CreateDirectory($path, $security)" in script
    assert script.index("SetSecurityDescriptorSddlForm") < script.index(
        "CreateDirectory"
    )
    assert "ReparsePoint" in script
    assert (
        "GetOwner([Security.Principal.SecurityIdentifier]).Value -ne 'S-1-5-18'"
        in script
    )
    assert "-not $actual.AreAccessRulesProtected -or $rules.Count -ne 1" in script
    assert (
        "$rule.IdentityReference.Value -ne 'S-1-5-18' -or $rule.IsInherited" in script
    )
    assert "[Security.AccessControl.AccessControlType]::Allow" in script
    assert "[Security.AccessControl.FileSystemRights]::FullControl" in script
    assert (
        "[Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'"
        in script
    )
    assert "[Security.AccessControl.PropagationFlags]::None" in script
    assert "SetAccessControl" not in script
    assert "Set-Acl" not in script
    assert "icacls" not in script


def _script(**changes: Any) -> str:
    options: dict[str, Any] = dict(
        tmp_start=PREFIX,
        command=["cmd.exe", "/c", "echo hello"],
        stdin=None,
        cwd=None,
        env={},
        user=None,
        timeout=30,
    )
    options.update(changes)
    return exec_windows.build_script(**options)


def test_supervisor_is_separate_from_payload_and_publishes_after_child() -> None:
    script = _script(command=["exit", "7"], env={"ERRORLEVEL": "999"})
    supervisor, payload = script.split(":payload\r\n")

    assert 'if "%~1"=="__proxmox_payload" goto payload' in supervisor
    assert f'{CMD_EXE} /d /e:on /v:off /s /c ""%~f0" __proxmox_payload"' in supervisor
    assert 'set "ERRORLEVEL="' in supervisor
    assert 'set "ERRORLEVEL=999"' not in supervisor
    assert 'set "ERRORLEVEL=999"' in payload
    assert payload.endswith("exit 7\r\n")
    assert supervisor.index('set "ERRORLEVEL="') < supervisor.index("cmd.exe")
    assert supervisor.index("cmd.exe") < supervisor.index(
        'set "proxmox_returncode=%ERRORLEVEL%"'
    )
    assert supervisor.index("echo %proxmox_returncode%") < supervisor.index("move /y")
    assert (
        f'move /y "{PREFIX}script.returncode.tmp" '
        f'"{PREFIX}script.returncode" >nul\r\nexit /b %ERRORLEVEL%'
    ) in supervisor
    assert "del " not in script


def test_result_write_checks_its_own_status_not_command_failure() -> None:
    script = _script()
    assert (
        'set "proxmox_returncode=%ERRORLEVEL%"\r\n'
        "setlocal EnableExtensions\r\n"
        f'>"{PREFIX}script.returncode.tmp" '
        "echo %proxmox_returncode% || exit /b 125\r\n"
    ) in script


def test_staged_input_is_redirected_into_child() -> None:
    input_path = f"{PREFIX}stdin"
    script = _script(stdin_file=input_path)
    child = next(line for line in script.splitlines() if line.startswith(CMD_EXE))
    assert child.endswith(f'<"{input_path}"')
    assert f'>"{PREFIX}script.stdout"' in child
    assert f'2>"{PREFIX}script.stderr"' in child


def test_env_cwd_and_existing_argument_quoting_stay_in_payload() -> None:
    script = _script(
        command=["app.exe", "two words", "a&b", "x|y", "z>q"],
        cwd=r"C:\work dir",
        env={"VALUE": "100%"},
    )
    supervisor, payload = script.split(":payload\r\n")
    assert "cd /d" not in supervisor
    assert 'set "VALUE=100%%"' in payload
    assert 'cd /d "C:\\work dir" || exit /b 1' in payload
    assert payload.endswith('app.exe "two words" a^&b x^|y z^>q\r\n')


def test_windows_timeout_remains_explicitly_host_side() -> None:
    assert _script(timeout=1) == _script(timeout=100)


@pytest.mark.parametrize("stdin", ["input", b"input", "", b""])
def test_inline_input_is_not_silently_discarded(stdin) -> None:
    with pytest.raises(NotImplementedError, match="staged"):
        _script(stdin=stdin)


def test_user_is_not_silently_ignored() -> None:
    with pytest.raises(NotImplementedError, match="impersonation"):
        _script(user="Administrator")


def test_claim_precedes_assembly_and_duplicate_branch_cannot_mutate_files() -> None:
    parts = [f"{PREFIX}script.bat.part0", f"{PREFIX}script.bat.part1"]
    argv = exec_windows.build_launch(PREFIX, parts)
    assert argv[:5] == [CMD_EXE, "/d", "/e:on", "/v:off", "/c"]
    launch = argv[-1]
    assert launch.startswith(f"mkdir {PREFIX}claimed 2>nul & ")
    assert (
        f"if errorlevel 1 (if exist {PREFIX}claimed\\ "
        "(exit /b 0) else (echo Cannot claim guest execution 1>&2 & exit /b 125))"
    ) in launch
    assert launch.endswith(
        f"else (copy /b {parts[0]}+{parts[1]} {PREFIX}script.bat >nul "
        f"&& {PREFIX}script.bat)"
    )
    assert '"' not in launch
    assert "del " not in launch
    assert "rmdir" not in launch


def test_stdin_is_assembled_after_claim_and_before_running_script() -> None:
    parts = [f"{PREFIX}script.bat.part0", f"{PREFIX}script.bat.part1"]
    stdin_parts = [f"{PREFIX}stdin.part0", f"{PREFIX}stdin.part1"]
    launch = exec_windows.build_launch(PREFIX, parts, stdin_parts)[-1]
    assert launch.startswith(f"mkdir {PREFIX}claimed 2>nul & ")
    assert launch.endswith(
        f"else (copy /b {parts[0]}+{parts[1]} {PREFIX}script.bat >nul "
        f"&& copy /b {stdin_parts[0]}+{stdin_parts[1]} {PREFIX}stdin >nul "
        f"&& {PREFIX}script.bat)"
    )


def test_stdin_can_be_chunked_without_chunking_script() -> None:
    stdin_parts = [f"{PREFIX}stdin.part0", f"{PREFIX}stdin.part1"]
    launch = exec_windows.build_launch(PREFIX, stdin_parts=stdin_parts)[-1]
    assert launch.endswith(
        f"else (copy /b {stdin_parts[0]}+{stdin_parts[1]} {PREFIX}stdin >nul "
        f"&& {PREFIX}script.bat)"
    )


def test_unchunked_launch_still_claims_first() -> None:
    launch = exec_windows.build_launch(PREFIX)[-1]
    assert launch.startswith(f"mkdir {PREFIX}claimed")
    assert launch.endswith(f"else ({PREFIX}script.bat)")
    assert "copy " not in launch


def test_cleanup_has_only_exact_files() -> None:
    paths = [f"{PREFIX}script.stdout", f"{PREFIX}script.stderr"]
    command = exec_windows.build_cleanup(paths)[-1]
    assert command == f"del /f /q {paths[0]} {paths[1]} 2>nul"
    assert '"' not in command
    assert "*" not in command
    assert "claimed" not in command
    assert "returncode" not in command


def test_empty_cleanup_is_a_noop() -> None:
    assert exec_windows.build_cleanup([])[-1] == "exit /b 0"
