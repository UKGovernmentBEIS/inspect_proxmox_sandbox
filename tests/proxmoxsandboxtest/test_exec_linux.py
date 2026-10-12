"""Execute Linux renderers locally to verify the persistent claim contract."""

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Generator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from proxmoxsandbox._impl import exec_linux

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("sh") is None,
    reason="executes Linux guest scripts locally",
)


@dataclass
class _Execution:
    directory: Path
    processes: list[subprocess.Popen[bytes]] = field(default_factory=list)

    @property
    def prefix(self) -> str:
        return str(self.directory / "execution_")

    def path(self, suffix: str) -> Path:
        return Path(f"{self.prefix}{suffix}")

    def write(
        self,
        command: list[str],
        *,
        stdin: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        stdin_file: str | None = None,
    ) -> str:
        script = exec_linux.build_script(
            self.prefix,
            command,
            stdin,
            cwd,
            env or {},
            user,
            timeout,
            stdin_file,
        )
        self.path("script.sh").write_text(script)
        return script

    def launch(self, parts: Sequence[str] = ()) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            exec_linux.build_launch(self.prefix, parts),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.processes.append(process)
        return process

    def run(self, parts: Sequence[str] = (), *, timeout: float = 5) -> int:
        process = self.launch(parts)
        process.communicate(timeout=timeout)
        assert process.returncode is not None
        return process.returncode

    def blocked_command(self) -> list[str]:
        side_effect = shlex.quote(str(self.path("side_effect")))
        ready = shlex.quote(str(self.path("ready")))
        release = shlex.quote(str(self.path("release")))
        finished = shlex.quote(str(self.path("finished")))
        return [
            "sh",
            "-c",
            f"printf 'once\\n' >> {side_effect}; printf 'before\\n'; "
            f": > {ready}; while [ ! -e {release} ]; do sleep 0.01; done; "
            f"printf 'after\\n'; : > {finished}",
        ]

    def wait_ready(self, owner: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + 5
        while not self.path("ready").exists():
            assert owner.poll() is None, "owner exited before the command barrier"
            assert time.monotonic() < deadline, "command did not reach its barrier"
            time.sleep(0.01)


@pytest.fixture
def execution(tmp_path: Path) -> Generator[_Execution, None, None]:
    instance = _Execution(tmp_path)
    try:
        yield instance
    finally:
        # Each launcher owns a new session, so failed assertions cannot kill
        # pytest or leave a blocked test command running in its process group.
        for process in instance.processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=5)


def _snapshot(path: Path) -> tuple[bytes, int, int]:
    stat = path.stat()
    return path.read_bytes(), stat.st_ino, stat.st_mtime_ns


@pytest.mark.parametrize(
    ("command", "returncode", "stdout", "stderr"),
    [
        (
            ["sh", "-c", "printf output; printf diagnostic >&2"],
            0,
            b"output",
            b"diagnostic",
        ),
        (["sh", "-c", "printf failed >&2; exit 23"], 23, b"", b"failed"),
        (["exit", "41"], 41, b"", b""),
        (["exec", "sh", "-c", "printf replaced; exit 19"], 19, b"replaced", b""),
    ],
)
def test_supervisor_publishes_terminal_result_for_arbitrary_command(
    execution: _Execution,
    command: list[str],
    returncode: int,
    stdout: bytes,
    stderr: bytes,
) -> None:
    execution.write(command)

    assert execution.run() == 0
    assert execution.path("script.returncode").read_text() == str(returncode)
    assert execution.path("script.stdout").read_bytes() == stdout
    assert execution.path("script.stderr").read_bytes() == stderr
    assert not execution.path("script.returncode.tmp").exists()
    assert execution.path("claimed").is_dir()


def test_simultaneous_launches_have_one_owner(execution: _Execution) -> None:
    effect = shlex.quote(str(execution.path("side_effect")))
    execution.write(["sh", "-c", f"printf 'once\\n' >> {effect}"])

    processes = [execution.launch() for _ in range(8)]
    for process in processes:
        process.communicate(timeout=5)
        assert process.returncode == 0

    assert execution.path("side_effect").read_text() == "once\n"
    assert execution.path("script.returncode").read_text() == "0"


def test_duplicate_exit_does_not_wait_or_publish_owner_result(
    execution: _Execution,
) -> None:
    execution.write(execution.blocked_command())
    owner = execution.launch()
    execution.wait_ready(owner)
    output_before = _snapshot(execution.path("script.stdout"))

    assert execution.run(timeout=2) == 0
    assert owner.poll() is None
    assert not execution.path("script.returncode").exists()
    assert _snapshot(execution.path("script.stdout")) == output_before
    assert execution.path("side_effect").read_text() == "once\n"

    execution.path("release").touch()
    owner.communicate(timeout=5)
    assert owner.returncode == 0
    assert execution.path("script.returncode").read_text() == "0"
    assert execution.path("script.stdout").read_text() == "before\nafter\n"


def test_delayed_duplicate_preserves_completed_result(execution: _Execution) -> None:
    effect = shlex.quote(str(execution.path("side_effect")))
    execution.write(
        ["sh", "-c", f"printf 'once\\n' >> {effect}; printf output; exit 7"]
    )
    assert execution.run() == 0
    snapshots = {
        suffix: _snapshot(execution.path(suffix))
        for suffix in ("script.returncode", "script.stdout", "script.stderr")
    }

    for _ in range(3):
        assert execution.run() == 0

    assert execution.path("side_effect").read_text() == "once\n"
    for suffix, before in snapshots.items():
        assert _snapshot(execution.path(suffix)) == before
    assert execution.path("script.returncode").read_text() == "7"


def test_claim_protects_chunk_assembly_from_running_duplicates(
    execution: _Execution,
) -> None:
    script = execution.write(execution.blocked_command())
    execution.path("script.sh").unlink()
    midpoint = len(script) // 2
    parts = [execution.path("part0"), execution.path("part1")]
    parts[0].write_text(script[:midpoint])
    parts[1].write_text(script[midpoint:])
    part_names = [str(part) for part in parts]
    owner = execution.launch(part_names)
    execution.wait_ready(owner)
    assembled_before = _snapshot(execution.path("script.sh"))
    assert assembled_before[0] == script.encode()

    duplicates = [execution.launch(part_names) for _ in range(4)]
    for duplicate in duplicates:
        duplicate.communicate(timeout=2)
        assert duplicate.returncode == 0
    assert _snapshot(execution.path("script.sh")) == assembled_before
    assert owner.poll() is None
    assert not execution.path("script.returncode").exists()

    execution.path("release").touch()
    owner.communicate(timeout=5)
    assert execution.path("side_effect").read_text() == "once\n"
    assert execution.path("script.returncode").read_text() == "0"


def test_cleanup_does_not_reopen_claim_or_remove_terminal_record(
    execution: _Execution,
) -> None:
    effect = shlex.quote(str(execution.path("side_effect")))
    script = execution.write(["sh", "-c", f"printf 'once\\n' >> {effect}"])
    part = execution.path("part0")
    part.write_text(script)
    assert execution.run([str(part)]) == 0
    terminal_before = _snapshot(execution.path("script.returncode"))
    neighbour = execution.path("script.unowned")
    neighbour.write_text("keep")
    disposable = [
        execution.path(suffix)
        for suffix in (
            "script.sh",
            "script.stdout",
            "script.stderr",
            "script.returncode.tmp",
            "stdin",
            "part0",
        )
    ]

    subprocess.run(
        exec_linux.build_cleanup([str(path) for path in disposable]),
        check=True,
        timeout=5,
    )
    assert execution.path("claimed").is_dir()
    assert all(not path.exists() for path in disposable)
    assert neighbour.read_text() == "keep"

    assert execution.run([str(part)]) == 0
    assert execution.path("side_effect").read_text() == "once\n"
    assert _snapshot(execution.path("script.returncode")) == terminal_before
    assert all(not path.exists() for path in disposable)


def test_owner_death_does_not_permit_replay(execution: _Execution) -> None:
    execution.write(execution.blocked_command())
    owner = execution.launch()
    execution.wait_ready(owner)

    owner.kill()
    owner.wait(timeout=5)
    assert owner.returncode == -signal.SIGKILL
    assert execution.path("claimed").is_dir()
    assert not execution.path("script.returncode").exists()

    assert execution.run() == 0
    assert execution.path("side_effect").read_text() == "once\n"
    assert not execution.path("script.returncode").exists()

    # The original payload survives its supervisor, but its later completion
    # cannot publish a result or allow a duplicate to become another owner.
    execution.path("release").touch()
    owner.communicate(timeout=5)
    deadline = time.monotonic() + 5
    while not execution.path("finished").exists():
        assert time.monotonic() < deadline, "surviving payload did not finish"
        time.sleep(0.01)
    assert execution.path("script.stdout").read_text() == "before\nafter\n"
    assert not execution.path("script.returncode").exists()
    assert execution.run() == 0
    assert execution.path("side_effect").read_text() == "once\n"


@pytest.mark.parametrize("obstruction", ["script.returncode.tmp", "script.returncode"])
def test_publication_failure_does_not_permit_replay(
    execution: _Execution, obstruction: str
) -> None:
    effect = shlex.quote(str(execution.path("side_effect")))
    execution.write(["sh", "-c", f"printf 'once\\n' >> {effect}"])
    execution.path(obstruction).mkdir()

    assert execution.run() != 0
    assert execution.path("claimed").is_dir()
    assert not execution.path("script.returncode").is_file()

    execution.path(obstruction).rmdir()
    assert execution.run() == 0
    assert execution.path("side_effect").read_text() == "once\n"
    assert not execution.path("script.returncode").exists()


def test_failed_chunk_assembly_keeps_claim(execution: _Execution) -> None:
    part = execution.path("part0")
    assert execution.run([str(part)]) != 0
    assert execution.path("claimed").is_dir()
    assert not execution.path("script.returncode").exists()

    effect = shlex.quote(str(execution.path("side_effect")))
    part.write_text(execution.write(["sh", "-c", f"printf once >> {effect}"]))
    assert execution.run([str(part)]) == 0
    assert not execution.path("side_effect").exists()
    assert not execution.path("script.returncode").exists()


@pytest.mark.parametrize("failure", ["existing_file", "missing_parent"])
def test_failed_claim_without_directory_is_setup_failure(
    execution: _Execution, failure: str
) -> None:
    effect = execution.path("side_effect")
    execution.write(["sh", "-c", f"printf once >> {shlex.quote(str(effect))}"])
    if failure == "existing_file":
        execution.path("claimed").write_text("not a directory")
    else:
        execution.directory /= "missing"

    assert execution.run() != 0
    assert not effect.exists()
    assert not execution.path("claimed").is_dir()
    assert not execution.path("script.returncode").exists()


def test_cwd_and_environment_are_scoped_to_payload(execution: _Execution) -> None:
    cwd = execution.directory / "working directory"
    cwd.mkdir()
    value = "spaces, quotes ' and \" and $shell syntax"
    execution.write(
        ["printf", "%s\\n", value],
        cwd=str(cwd),
        env={"PATH": "/no/executables"},
    )

    assert execution.run() == 0
    assert execution.path("script.returncode").read_text() == "0"
    assert execution.path("script.stdout").read_text() == value + "\n"


def test_payload_observes_cwd_and_environment(execution: _Execution) -> None:
    cwd = execution.directory / "working directory"
    cwd.mkdir()
    value = "a value with 'quotes' and $dollars"
    execution.write(
        ["sh", "-c", 'printf "%s\\n%s" "$PWD" "$EXEC_TEST_VALUE"'],
        cwd=str(cwd),
        env={"EXEC_TEST_VALUE": value},
    )

    assert execution.run() == 0
    assert execution.path("script.stdout").read_text() == f"{cwd}\n{value}"


def test_user_payload_forwarding_preserves_supervision(
    execution: _Execution, monkeypatch: pytest.MonkeyPatch
) -> None:
    binaries = execution.directory / "bin"
    binaries.mkdir()
    arguments = execution.path("su_arguments")
    fake_su = binaries / "su"
    fake_su.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(arguments))}\n"
        "exec /bin/sh\n"
    )
    fake_su.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")
    cwd = execution.directory / "working directory"
    cwd.mkdir()
    execution.write(
        ["sh", "-c", 'printf "%s:%s" "$PWD" "$EXEC_TEST_VALUE"; exit 17'],
        user="requested user",
        env={"EXEC_TEST_VALUE": "forwarded"},
        cwd=str(cwd),
    )

    assert execution.run() == 0
    assert arguments.read_text() == "-l\nrequested user\n"
    assert execution.path("script.stdout").read_text() == f"{cwd}:forwarded"
    assert execution.path("script.returncode").read_text() == "17"


def test_completion_does_not_run_global_sync(
    execution: _Execution, monkeypatch: pytest.MonkeyPatch
) -> None:
    binaries = execution.directory / "bin"
    binaries.mkdir()
    called = execution.path("sync_called")
    fake_sync = binaries / "sync"
    fake_sync.write_text(f"#!/bin/sh\n: > {shlex.quote(str(called))}\n")
    fake_sync.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")
    execution.write(["printf", "output"])

    assert execution.run() == 0
    assert execution.path("script.returncode").read_text() == "0"
    assert not called.exists()


def test_cwd_failure_publishes_terminal_result(execution: _Execution) -> None:
    effect = execution.path("side_effect")
    execution.write(
        ["sh", "-c", f"printf once >> {shlex.quote(str(effect))}"],
        cwd=str(execution.directory / "absent"),
    )

    assert execution.run() == 0
    assert int(execution.path("script.returncode").read_text()) != 0
    assert execution.path("script.stderr").read_bytes()
    assert not effect.exists()


@pytest.mark.parametrize("stdin", ["", "first\nsecond\n'$shell'", b"\x00\xff\ninput"])
def test_inline_stdin_round_trips(execution: _Execution, stdin: str | bytes) -> None:
    execution.write(["cat"], stdin=stdin)

    assert execution.run() == 0
    expected = stdin.encode() if isinstance(stdin, str) else stdin
    assert execution.path("script.stdout").read_bytes() == expected
    assert execution.path("script.returncode").read_text() == "0"


def test_staged_stdin_round_trips(execution: _Execution) -> None:
    stdin = b"\x00\xff\ninput from a staged file"
    source = execution.directory / "input file"
    source.write_bytes(stdin)
    execution.write(["cat"], stdin_file=str(source))

    assert execution.run() == 0
    assert execution.path("script.stdout").read_bytes() == stdin
    assert execution.path("script.returncode").read_text() == "0"


@pytest.mark.skipif(shutil.which("timeout") is None, reason="needs GNU timeout")
@pytest.mark.parametrize(
    ("command", "returncode"),
    [
        (["sh", "-c", "printf started; exec sleep 3"], "124"),
        (["sh", "-c", "trap '' TERM; printf started; exec sleep 10"], "137"),
    ],
)
def test_guest_timeout_publishes_terminal_result(
    execution: _Execution, command: list[str], returncode: str
) -> None:
    execution.write(command, timeout=1)

    assert execution.run(timeout=13) == 0
    assert execution.path("script.stdout").read_text() == "started"
    assert execution.path("script.returncode").read_text() == returncode
