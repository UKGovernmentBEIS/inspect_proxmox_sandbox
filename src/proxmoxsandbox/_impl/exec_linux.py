"""Render Linux commands for the persistent guest execution contract."""

import base64
import shlex
from collections.abc import Sequence

SCRIPT_SUFFIX = ".sh"
TMP_DIR = "/tmp"


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
    # A separate shell scope prevents an arbitrary exit/exec, cwd failure or
    # environment change from bypassing or changing result publication.
    lines = ["("]
    if user is not None:
        lines.append(f"su -l {shlex.quote(user)} << 'EOF{tmp_start}EOF'")
    if cwd is not None:
        lines.append(f"cd {shlex.quote(cwd)} || exit $?")
    for key, value in env.items():
        lines.append(f"export {shlex.quote(key)}={shlex.quote(value)}")

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
        command_line += f" <{shlex.quote(stdin_file)}"
    lines.append(command_line)
    if user is not None:
        lines.append(f"EOF{tmp_start}EOF")

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


def build_launch(tmp_start: str, parts: Sequence[str] = ()) -> list[str]:
    """Claim an execution before assembling or running its uploaded script."""
    claim = shlex.quote(f"{tmp_start}claimed")
    script = shlex.quote(f"{tmp_start}script{SCRIPT_SUFFIX}")
    lines = [f"if mkdir -- {claim} 2>/dev/null; then"]
    if parts:
        lines.append(f"  cat -- {shlex.join(parts)} >{script} || exit $?")
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
