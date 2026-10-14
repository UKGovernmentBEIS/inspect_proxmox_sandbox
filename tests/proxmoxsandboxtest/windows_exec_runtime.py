r"""Generate a dependency-free PowerShell check of the emitted Windows wrappers.

Generate on Linux with::

    python tests/proxmoxsandboxtest/windows_exec_runtime.py /tmp/exec-runtime.ps1

Copy the artifact to an authorised Windows test guest and run::

    powershell.exe -NoProfile -NonInteractive -File C:\Temp\exec-runtime.ps1

The harness writes one bounded JSON document to stdout and exits nonzero if any
check fails. It creates and removes only its unique owned temporary directory.
PowerShell is test infrastructure, not part of the production execution path.
"""

import argparse
import base64
import importlib.util
import json
import subprocess
import uuid
from pathlib import Path
from types import ModuleType


def _load_renderer() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2] / "src/proxmoxsandbox/_impl/exec_windows.py"
    )
    spec = importlib.util.spec_from_file_location("exec_windows_runtime", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _native(argv: list[str]) -> dict[str, str]:
    return {"exe": argv[0], "arguments": subprocess.list2cmdline(argv[1:])}


def build_harness() -> str:
    renderer = _load_renderer()
    root = rf"C:\Windows\Temp\inspect-exec-runtime-{uuid.uuid4().hex}"
    fixtures: dict[str, object] = {}

    def case(
        name: str,
        command: list[str] | None = None,
        *,
        returncode: int = 0,
        waiting: bool = False,
        chunked: bool = False,
        body_commands: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        stdin_bytes: bytes | None = None,
    ) -> None:
        prefix = rf"{root}\{name}_"
        body_path = prefix + "body.bat"
        body = [
            "@echo off",
            f'echo side>>"{prefix}side"',
            f'echo ready>"{prefix}ready"',
        ]
        if waiting:
            body.extend(
                [
                    ":wait",
                    f'if exist "{prefix}release" goto finished',
                    "ping -n 2 127.0.0.1 >nul",
                    "goto wait",
                    ":finished",
                ]
            )
        body.extend(body_commands or ["echo stdout", "echo stderr 1>&2"])
        body.append(f"exit /b {returncode}")
        stdin_file = prefix + "stdin" if stdin_bytes is not None else None
        script = renderer.build_script(
            tmp_start=prefix,
            command=command or [body_path],
            stdin=None,
            cwd=cwd,
            env=env or {},
            user=None,
            timeout=20,
            stdin_file=stdin_file,
        ).encode("ascii")
        files = {body_path: ("\r\n".join(body) + "\r\n").encode("ascii")}
        if stdin_file is not None and stdin_bytes is not None:
            files[stdin_file] = stdin_bytes
        parts: list[str] = []
        if chunked:
            split = len(script) // 2
            parts = [prefix + "part0", prefix + "part1"]
            files.update(zip(parts, (script[:split], script[split:])))
        else:
            files[prefix + "script.bat"] = script
        cleanup_paths = [
            prefix + "script.bat",
            prefix + "script.stdout",
            prefix + "script.stderr",
            prefix + "script.returncode.tmp",
            *parts,
        ]
        fixtures[name] = {
            "prefix": prefix,
            "files": {
                path: base64.b64encode(data).decode("ascii")
                for path, data in files.items()
            },
            "launch": _native(renderer.build_launch(prefix, parts)),
            "cleanup": _native(renderer.build_cleanup(cleanup_paths)),
            "cleanup_paths": cleanup_paths,
            "parts": parts,
        }

    case("success")
    case("nonzero", returncode=17)
    case("exit", ["exit", "29"])
    case("direct_batch", returncode=23)
    case("concurrent", waiting=True)
    case("chunked", waiting=True, chunked=True)
    case("preclaimed")
    case("cleanup")
    case("inherited_errorlevel", returncode=31)
    case(
        "payload_errorlevel",
        returncode=41,
        env={"ERRORLEVEL": "777"},
        body_commands=["echo %ERRORLEVEL%"],
    )
    case(
        "context",
        cwd=root,
        env={"PROXMOX_TEST_VALUE": "literal%value%"},
        stdin_bytes=b"payload line\r\n",
        body_commands=["cd", "echo %PROXMOX_TEST_VALUE%", "findstr /r .*"],
    )
    case("claim_failure")
    case("publication_failure")
    payload = base64.b64encode(
        json.dumps({"root": root, "cases": fixtures}).encode("utf-8")
    ).decode("ascii")
    return _POWERSHELL.replace("__FIXTURES_BASE64__", payload)


_POWERSHELL = r"""$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$data = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('__FIXTURES_BASE64__')) | ConvertFrom-Json
$root = [string]$data.root
$results = [Collections.Generic.List[object]]::new()
$processes = [Collections.Generic.List[Diagnostics.Process]]::new()
$created = $false
$total = [Diagnostics.Stopwatch]::StartNew()

function Assert-True([bool]$condition, [string]$message) {
    if (-not $condition) { throw $message }
}
function Start-Native($command, [hashtable]$environment = @{}) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = [string]$command.exe
    $info.Arguments = [string]$command.arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    foreach ($key in $environment.Keys) {
        $info.EnvironmentVariables[$key] = [string]$environment[$key]
    }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    Assert-True ($process.Start()) 'Native process failed to start'
    $processes.Add($process)
    return $process
}
function Wait-Native($process, [int]$expected = 0) {
    Assert-True ($process.WaitForExit(10000)) 'Native process exceeded 10 seconds'
    $out = $process.StandardOutput.ReadToEnd()
    $err = $process.StandardError.ReadToEnd()
    Assert-True ($process.ExitCode -eq $expected) (
        'Wrapper exited {0}: {1}' -f $process.ExitCode,
        (($out + $err).Substring(0, [Math]::Min(1000, ($out + $err).Length))))
}
function Run-Native($command, [int]$expected = 0, [hashtable]$environment = @{}) {
    Wait-Native (Start-Native $command $environment) $expected
}
function Wait-File([string]$path) {
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-Path -LiteralPath $path)) {
        Assert-True ($timer.ElapsedMilliseconds -lt 10000) "File not created: $path"
        Start-Sleep -Milliseconds 25
    }
}
function Read-Text([string]$path) { return [IO.File]::ReadAllText($path).Trim() }
function Assert-Result($case, [int]$expected) {
    $path = $case.prefix + 'script.returncode'
    Assert-True (Test-Path -LiteralPath $path) 'No terminal record'
    Assert-True ((Read-Text $path) -eq [string]$expected) 'Wrong terminal return code'
    Assert-True (Test-Path -LiteralPath ($case.prefix + 'claimed')) 'Claim missing'
    Assert-True (-not (Test-Path -LiteralPath ($path + '.tmp'))) (
        'Publication incomplete')
}
function Assert-SideOnce($case) {
    $lines = [IO.File]::ReadAllLines($case.prefix + 'side')
    Assert-True ($lines.Length -eq 1 -and $lines[0] -eq 'side') 'Command replayed'
}
function Test-Case([string]$name, [scriptblock]$body) {
    $timer = [Diagnostics.Stopwatch]::StartNew()
    try {
        & $body
        $results.Add([ordered]@{ name = $name; passed = $true;
            elapsed_ms = $timer.ElapsedMilliseconds })
    } catch {
        $message = [string]$_.Exception.Message
        $results.Add([ordered]@{ name = $name; passed = $false;
            elapsed_ms = $timer.ElapsedMilliseconds;
            error = $message.Substring(0, [Math]::Min(1500, $message.Length)) })
    }
}
try {
    $rootPattern = '^C:\\Windows\\Temp\\inspect-exec-runtime-[a-f0-9]{32}$'
    Assert-True ($root -match $rootPattern) 'Refusing unexpected root'
    Assert-True (-not (Test-Path -LiteralPath $root)) 'Owned root already exists'
    $null = [IO.Directory]::CreateDirectory($root)
    $created = $true
    foreach ($property in $data.cases.PSObject.Properties) {
        foreach ($file in $property.Value.files.PSObject.Properties) {
            $bytes = [Convert]::FromBase64String($file.Value)
            [IO.File]::WriteAllBytes($file.Name, $bytes)
        }
    }
    Test-Case 'ordinary success and separate streams' {
        $case = $data.cases.success
        Run-Native $case.launch
        Assert-Result $case 0
        Assert-SideOnce $case
        Assert-True ((Read-Text ($case.prefix + 'script.stdout')) -eq 'stdout') (
            'Bad stdout')
        Assert-True ((Read-Text ($case.prefix + 'script.stderr')) -eq 'stderr') (
            'Bad stderr')
    }
    Test-Case 'nonzero completion' {
        $case = $data.cases.nonzero
        Run-Native $case.launch
        Assert-Result $case 17
        Assert-SideOnce $case
    }
    Test-Case 'exit cannot bypass publication' {
        $case = $data.cases.exit
        Run-Native $case.launch
        Assert-Result $case 29
    }
    Test-Case 'direct batch transfer cannot bypass publication' {
        $case = $data.cases.direct_batch
        Run-Native $case.launch
        Assert-Result $case 23
        Assert-SideOnce $case
    }
    Test-Case 'inherited ERRORLEVEL cannot shadow terminal status' {
        $case = $data.cases.inherited_errorlevel
        Run-Native $case.launch 0 @{ ERRORLEVEL = '999' }
        Assert-Result $case 31
        Assert-SideOnce $case
    }
    Test-Case 'payload ERRORLEVEL is isolated from supervisor status' {
        $case = $data.cases.payload_errorlevel
        Run-Native $case.launch 0 @{ ERRORLEVEL = '999' }
        Assert-Result $case 41
        Assert-True ((Read-Text ($case.prefix + 'script.stdout')) -eq '777') (
            'Payload ERRORLEVEL was not preserved')
    }
    Test-Case 'working directory, literal percent environment and staged stdin' {
        $case = $data.cases.context
        Run-Native $case.launch
        Assert-Result $case 0
        $lines = [IO.File]::ReadAllLines($case.prefix + 'script.stdout')
        Assert-True ($lines.Length -eq 3) 'Context output has unexpected lines'
        Assert-True ($lines[0] -eq $root) 'Working directory was not applied'
        Assert-True ($lines[1] -eq 'literal%value%') 'Environment value was altered'
        Assert-True ($lines[2] -eq 'payload line') 'Staged standard input was altered'
    }
    Test-Case 'claim creation failure cannot masquerade as a duplicate' {
        $case = $data.cases.claim_failure
        [IO.File]::WriteAllText($case.prefix + 'claimed', 'not a directory')
        Run-Native $case.launch 125
        Assert-True (-not (Test-Path -LiteralPath ($case.prefix + 'side'))) (
            'Command ran without claiming')
        $terminal = $case.prefix + 'script.returncode'
        Assert-True (-not (Test-Path -LiteralPath $terminal)) (
            'Claim failure published a terminal result')
    }
    Test-Case 'publication failure remains incomplete and cannot replay' {
        $case = $data.cases.publication_failure
        $terminal = $case.prefix + 'script.returncode'
        $null = [IO.Directory]::CreateDirectory($terminal + '.tmp')
        Run-Native $case.launch 125
        Assert-SideOnce $case
        Assert-True (-not (Test-Path -LiteralPath $terminal)) (
            'Failed publication produced a terminal record')
        Assert-True (Test-Path -LiteralPath ($case.prefix + 'claimed')) 'Claim missing'
        Run-Native $case.launch
        Assert-SideOnce $case
        Assert-True (-not (Test-Path -LiteralPath $terminal)) (
            'Duplicate falsely completed an unpublished execution')
    }
    Test-Case 'concurrent and delayed duplicates do not replay' {
        $case = $data.cases.concurrent
        $owner = Start-Native $case.launch
        Wait-File ($case.prefix + 'ready')
        $duplicates = @(1..6 | ForEach-Object { Start-Native $case.launch })
        foreach ($duplicate in $duplicates) { Wait-Native $duplicate }
        Assert-True (-not $owner.HasExited) 'Owner unexpectedly exited'
        $terminal = $case.prefix + 'script.returncode'
        Assert-True (-not (Test-Path -LiteralPath $terminal)) (
            'Duplicate exit published an early result')
        Assert-SideOnce $case
        [IO.File]::WriteAllText($case.prefix + 'release', 'release')
        Wait-Native $owner
        Assert-Result $case 0
        Run-Native $case.launch
        Assert-SideOnce $case
        Assert-Result $case 0
    }
    Test-Case 'chunk reassembly is protected by the claim' {
        $case = $data.cases.chunked
        $owner = Start-Native $case.launch
        Wait-File ($case.prefix + 'ready')
        $script = $case.prefix + 'script.bat'
        $original = [Convert]::ToBase64String([IO.File]::ReadAllBytes($script))
        foreach ($part in $case.parts) { [IO.File]::WriteAllText($part, 'INVALID') }
        Run-Native $case.launch
        Assert-True ([Convert]::ToBase64String([IO.File]::ReadAllBytes($script)) -eq (
            $original)) 'Duplicate rewrote the running script'
        $terminal = $case.prefix + 'script.returncode'
        Assert-True (-not (Test-Path -LiteralPath $terminal)) (
            'Duplicate falsely published completion')
        [IO.File]::WriteAllText($case.prefix + 'release', 'release')
        Wait-Native $owner
        Assert-Result $case 0
        Assert-SideOnce $case
    }
    Test-Case 'preclaimed identity never starts or mutates outputs' {
        $case = $data.cases.preclaimed
        $null = [IO.Directory]::CreateDirectory($case.prefix + 'claimed')
        [IO.File]::WriteAllText($case.prefix + 'script.stdout', 'retained')
        Run-Native $case.launch
        Assert-True (-not (Test-Path -LiteralPath ($case.prefix + 'side'))) (
            'Claim replayed')
        $terminal = $case.prefix + 'script.returncode'
        Assert-True (-not (Test-Path -LiteralPath $terminal)) (
            'Preclaim falsely completed')
        Assert-True ((Read-Text ($case.prefix + 'script.stdout')) -eq 'retained') (
            'Preclaim mutated outputs')
    }
    Test-Case 'exact cleanup retains records and prevents late replay' {
        $case = $data.cases.cleanup
        Run-Native $case.launch
        Assert-Result $case 0
        $unrelated = $case.prefix + 'unrelated'
        [IO.File]::WriteAllText($unrelated, 'retained')
        Run-Native $case.cleanup
        foreach ($path in $case.cleanup_paths) {
            Assert-True (-not (Test-Path -LiteralPath $path)) "Cleanup left $path"
        }
        Assert-Result $case 0
        Assert-True ((Read-Text $unrelated) -eq 'retained') 'Cleanup was not exact'
        Run-Native $case.launch
        Assert-SideOnce $case
        Assert-Result $case 0
    }
} catch {
    $message = [string]$_.Exception.Message
    $results.Add([ordered]@{ name = 'harness'; passed = $false;
        error = $message.Substring(0, [Math]::Min(1500, $message.Length)) })
} finally {
    foreach ($process in $processes) {
        if (-not $process.HasExited) {
            & taskkill.exe /PID $process.Id /T /F 2>&1 | Out-Null
            $null = $process.WaitForExit(5000)
        }
        $process.Dispose()
    }
    if ($created) {
        try { Remove-Item -LiteralPath $root -Recurse -Force } catch {
            $results.Add([ordered]@{ name = 'owned root cleanup'; passed = $false;
                error = 'Failed to remove owned temporary root' })
        }
    }
}
$failed = @($results | Where-Object { -not $_.passed }).Count
[ordered]@{ passed = ($failed -eq 0); failed = $failed;
    elapsed_ms = $total.ElapsedMilliseconds; root = $root;
    results = @($results.ToArray())
} | ConvertTo-Json -Depth 5 -Compress
if ($failed -ne 0) { exit 1 }
exit 0
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="output self-contained .ps1 path")
    args = parser.parse_args()
    args.output.write_text(build_harness(), encoding="ascii", newline="\r\n")


if __name__ == "__main__":
    main()
