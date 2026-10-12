"""Render Linux commands for the persistent guest execution contract."""

import base64
import shlex
from collections.abc import Sequence

SCRIPT_SUFFIX = ".sh"
TMP_DIR = "/tmp"


def build_prepare(workspace: str) -> list[str]:
    """Create a private workspace, or verify a previous preparation unchanged."""
    directory = shlex.quote(workspace)
    # stat does not follow symlinks. A service-owned directory in the sticky
    # temporary directory cannot be replaced by another guest user after this
    # check. Never repair a pre-existing object's owner or permissions.
    # %f includes the type: 41c0 is a directory with mode 0700.
    script = (
        f"mkdir -m 700 -- {directory} 2>/dev/null\n"
        f'if [ "$(stat -c \'%f:%u\' -- {directory})" != "41c0:$(id -u)" ]; then\n'
        f"  printf 'Unsafe guest execution workspace: %s\\n' {directory} >&2\n"
        "  exit 1\n"
        "fi"
    )
    return ["sh", "-c", script]


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
    """Supervise a claimed command and atomically publish its terminal result."""
    payload = []
    if cwd is not None:
        payload.append(f"cd {shlex.quote(cwd)} || exit $?")
    for key, value in env.items():
        payload.append(f"export {shlex.quote(key)}={shlex.quote(value)}")

    command_line = shlex.join(command)
    if timeout is not None:
        # Keep the guest's termination grace inside the shared polling grace.
        command_line = f"timeout -k 5s {timeout}s {command_line}"
    if stdin is not None:
        # Encoding preserves binary input and keeps it out of shell syntax.
        data = stdin if isinstance(stdin, bytes) else stdin.encode("utf-8")
        encoded = base64.b64encode(data).decode("ascii")
        command_line = f"echo '{encoded}' | base64 -d | {command_line}"
    if stdin_file is not None:
        command_line += " <&3 3<&-"
    payload.append(command_line)

    # Provider files are private without changing the command's own umask.
    # A separate shell scope keeps exit/exec, cwd and env away from publication.
    lines = ["proxmox_umask=$(umask)", "umask 077", "(", 'umask "$proxmox_umask"']
    if stdin_file is not None:
        # Open before su: the target user receives only its intended input,
        # without gaining traversal or read access to the control directory.
        lines.append(f"exec 3<{shlex.quote(stdin_file)} || exit $?")
    if user is not None:
        # Keep source off argv and avoid a heredoc delimiter in arbitrary data.
        encoded = base64.b64encode("\n".join(payload).encode()).decode("ascii")
        lines.append(
            f"printf '%s' '{encoded}' | base64 -d | su -l -- {shlex.quote(user)}"
        )
    else:
        lines.extend(payload)

    stdout = shlex.quote(f"{tmp_start}script.stdout")
    stderr = shlex.quote(f"{tmp_start}script.stderr")
    temporary_result = shlex.quote(f"{tmp_start}script.returncode.tmp")
    result = shlex.quote(f"{tmp_start}script.returncode")
    lines.extend(
        [
            f") >{stdout} 2>{stderr}",
            "status=$?",
            f"printf '%s' \"$status\" >{temporary_result} && "
            f"mv -fT -- {temporary_result} {result}",
        ]
    )
    return "\n".join(lines) + "\n"


def build_launch(
    tmp_start: str,
    parts: Sequence[str] = (),
    stdin_parts: Sequence[str] = (),
) -> list[str]:
    """Claim an execution before assembling or running its uploaded script."""
    claim = shlex.quote(f"{tmp_start}claimed")
    script = shlex.quote(f"{tmp_start}script{SCRIPT_SUFFIX}")
    lines = [f"if mkdir -m 700 -- {claim} 2>/dev/null; then"]
    if parts:
        lines.append(f"  (umask 077; cat -- {shlex.join(parts)} >{script}) || exit $?")
    if stdin_parts:
        stdin = shlex.quote(f"{tmp_start}stdin")
        lines.append(
            f"  (umask 077; cat -- {shlex.join(stdin_parts)} >{stdin}) || exit $?"
        )
    lines.extend(
        [
            f"  exec sh {script}",
            f"elif [ -d {claim} ]; then",
            # The owner may have finished or died. Neither permits a replay;
            # the stable result record, not this wrapper's exit, decides that.
            "  exit 0",
            "else",
            f"  printf 'Unable to claim guest execution: %s\\n' {claim} >&2",
            "  exit 1",
            "fi",
        ]
    )
    return ["sh", "-c", "\n".join(lines)]


def build_cleanup(paths: Sequence[str]) -> list[str]:
    """Remove only the exact disposable files selected by the shared executor."""
    return ["rm", "-f", "--", *paths]
