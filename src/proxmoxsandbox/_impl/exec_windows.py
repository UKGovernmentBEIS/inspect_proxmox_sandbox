"""Windows command supervision and replay-safe launch rendering.

The claim and terminal record belong to one execution and survive output cleanup.
This wrapper does not enforce a guest-side timeout or impersonate another user.
"""

from collections.abc import Sequence

SCRIPT_SUFFIX = ".bat"
TMP_DIR = r"C:\Windows\Temp"

_PAYLOAD_ARGUMENT = "__proxmox_payload"
_CMD = ["cmd.exe", "/d", "/e:on", "/v:off", "/c"]


def build_script(
    tmp_start: str,
    command: list[str],
    stdin: str | bytes | None,
    cwd: str | None,
    env: dict[str, str],
    user: str | None,
    timeout: int | None,
    stdin_file: str | None = None,
) -> str:
    """Supervise the command in a child cmd, then publish its terminal result.

    Standard input must be staged in a file. ``timeout`` only bounds the shared
    executor's host wait; Windows process-tree termination is not implemented.
    """
    if user is not None:
        raise NotImplementedError("Windows command user impersonation is not supported")
    if stdin is not None and stdin_file is None:
        raise NotImplementedError("Windows standard input must be staged in a file")

    # Re-enter the same immutable batch file rather than quote the arbitrary
    # command twice. A payload's `exit` or batch-file transfer cannot bypass the
    # supervising cmd's publication of the real child exit status.
    child = (
        f'cmd.exe /d /e:on /v:off /s /c ""%~f0" {_PAYLOAD_ARGUMENT}"'
        f' >"{tmp_start}script.stdout" 2>"{tmp_start}script.stderr"'
    )
    if stdin_file is not None:
        child += f' <"{stdin_file}"'

    lines = [
        "@echo off",
        "setlocal EnableExtensions DisableDelayedExpansion",
        f'if "%~1"=="{_PAYLOAD_ARGUMENT}" goto payload',
        # An environment variable named ERRORLEVEL shadows cmd's dynamic value.
        # Requested command environment changes run only in the child below.
        'set "ERRORLEVEL="',
        child,
        'set "proxmox_returncode=%ERRORLEVEL%"',
        # Reset the command's status before writing. Redirection can fail without
        # setting ERRORLEVEL, so test the write directly before attempting move.
        "setlocal EnableExtensions",
        f'>"{tmp_start}script.returncode.tmp" echo %proxmox_returncode% || exit /b 125',
        f'move /y "{tmp_start}script.returncode.tmp" '
        f'"{tmp_start}script.returncode" >nul',
        "exit /b %ERRORLEVEL%",
        ":payload",
    ]
    for key, value in env.items():
        escaped_value = value.replace("%", "%%").replace("^", "^^")
        lines.append(f'set "{key}={escaped_value}"')
    if cwd is not None:
        lines.append(f'cd /d "{cwd}" || exit /b 1')

    escaped_args = []
    for arg in command:
        escaped_arg = (
            arg.replace("^", "^^")
            .replace("&", "^&")
            .replace("|", "^|")
            .replace("<", "^<")
            .replace(">", "^>")
        )
        if " " in arg or '"' in arg:
            escaped_arg = f'"{escaped_arg}"'
        escaped_args.append(escaped_arg)
    # End the child batch at the command so cmd returns its native status even
    # when the requested environment defines ERRORLEVEL itself.
    lines.append(" ".join(escaped_args))
    return "\r\n".join(lines) + "\r\n"


def build_launch(tmp_start: str, parts: Sequence[str] = ()) -> list[str]:
    """Claim before assembly or execution; duplicates leave all files intact."""
    claim = f"{tmp_start}claimed"
    script = f"{tmp_start}script{SCRIPT_SUFFIX}"
    # These paths are generated without whitespace or shell metacharacters.
    # QGA escapes embedded quotes for a C argv parser, not cmd's /c parser.
    launch = script
    if parts:
        sources = "+".join(parts)
        launch = f"copy /b {sources} {script} >nul && {launch}"
    # Check the directory only after the atomic creation attempt. An unrelated
    # mkdir failure must not be reported as a duplicate execution.
    command = (
        f"mkdir {claim} 2>nul & "
        f"if errorlevel 1 (if exist {claim}\\ "
        "(exit /b 0) else (echo Cannot claim guest execution 1>&2 & exit /b 125)) "
        f"else ({launch})"
    )
    return [*_CMD, command]


def build_cleanup(paths: Sequence[str]) -> list[str]:
    """Remove only the exact output/staging files chosen by the shared owner."""
    command = "del /f /q " + " ".join(paths) + " 2>nul" if paths else "exit /b 0"
    return [*_CMD, command]
