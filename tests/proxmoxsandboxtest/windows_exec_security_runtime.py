"""Generate Windows SYSTEM-token checks for execution workspace permissions.

Run this generator on the development host, then run its PowerShell output as
SYSTEM in a disposable Windows guest. It uses an actual restricted access token,
without creating accounts or passwords, and removes only its owned fixtures.
The JSON result contains no command payloads or credentials.
"""

import argparse
import base64
import importlib.util
import json
import subprocess
import uuid
from pathlib import Path


def build_harness() -> str:
    path = (
        Path(__file__).resolve().parents[2] / "src/proxmoxsandbox/_impl/exec_windows.py"
    )
    spec = importlib.util.spec_from_file_location("exec_windows_security_runtime", path)
    assert spec is not None and spec.loader is not None
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    root = rf"C:\Windows\Temp\inspect-exec-security-{uuid.uuid4().hex}"
    fixtures = {}
    for name in (
        "private",
        "foreign_owner",
        "weak_acl",
        "unprotected_acl",
        "noninheritable_acl",
        "file",
        "junction",
        "junction_parent\\child",
    ):
        directory = rf"{root}\{name}"
        argv = renderer.build_prepare(directory)
        fixtures[name] = {
            "directory": directory,
            "exe": argv[0],
            "arguments": subprocess.list2cmdline(argv[1:]),
        }
    empty = root + "-empty"
    argv = renderer.build_prepare(empty)
    fixtures["empty"] = {
        "directory": empty,
        "exe": argv[0],
        "arguments": subprocess.list2cmdline(argv[1:]),
    }

    directory = root + r"\large"
    prefix = directory + "\\"
    hash_script = rf"""$ErrorActionPreference = 'Stop'
[IO.File]::AppendAllText('{prefix}side', "ran`n")
$stream = [Console]::OpenStandardInput()
$sha = [Security.Cryptography.SHA256]::Create()
try {{
    [Console]::WriteLine([BitConverter]::ToString(
        $sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant())
}} finally {{ $stream.Dispose(); $sha.Dispose() }}
"""
    command = [
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        base64.b64encode(hash_script.encode("utf-16le")).decode("ascii"),
    ]
    script = renderer.build_script(
        tmp_start=prefix,
        command=command,
        stdin=None,
        cwd=None,
        env={},
        user=None,
        timeout=30,
        stdin_file=prefix + "stdin",
    ).encode("ascii")
    parts = [prefix + "script.bat.part0", prefix + "script.bat.part1"]
    split = len(script) // 2
    length, chunk_size = 4 * 1024 * 1024, 40 * 1024
    count = (length + chunk_size - 1) // chunk_size
    width = len(str(count - 1))
    stdin_parts = [f"{prefix}stdin.part{i:0{width}d}" for i in range(count)]
    cleanup_paths = [
        prefix + "script.bat",
        *parts,
        prefix + "stdin",
        *stdin_parts,
        prefix + "script.stdout",
        prefix + "script.stderr",
        prefix + "script.returncode.tmp",
    ]
    large: dict[str, object] = {
        "directory": directory,
        "prefix": prefix,
        "length": length,
        "chunk_size": chunk_size,
        "script_parts": {
            path: base64.b64encode(data).decode("ascii")
            for path, data in zip(parts, (script[:split], script[split:]))
        },
        "stdin_parts": stdin_parts,
        "cleanup_paths": cleanup_paths,
        "unrelated": f"{prefix}stdin.part{count:0{width}d}",
    }
    for name, argv in (
        ("prepare", renderer.build_prepare(directory)),
        ("launch", renderer.build_launch(prefix, parts, stdin_parts)),
        ("cleanup", renderer.build_cleanup(cleanup_paths)),
    ):
        large[name] = {
            "exe": argv[0],
            "arguments": subprocess.list2cmdline(argv[1:]),
            "command_length": len(subprocess.list2cmdline(argv)),
        }
    payload = base64.b64encode(
        json.dumps(
            {"root": root, "empty": empty, "cases": fixtures, "large": large}
        ).encode()
    ).decode("ascii")
    return _POWERSHELL.replace("__FIXTURES_BASE64__", payload)


_POWERSHELL = r"""$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$data = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('__FIXTURES_BASE64__')) | ConvertFrom-Json
$root = [string]$data.root
$empty = [string]$data.empty
$results = [Collections.Generic.List[object]]::new()
$created = $false
$started = [Diagnostics.Stopwatch]::StartNew()
$parentSddl = $null

function Assert-True([bool]$condition, [string]$message) {
    if (-not $condition) { throw $message }
}
function Test-Case([string]$name, [scriptblock]$body) {
    try {
        & $body
        $results.Add([ordered]@{ name = $name; passed = $true })
    } catch {
        $message = [string]$_.Exception.Message
        $results.Add([ordered]@{ name = $name; passed = $false;
            error = $message.Substring(0, [Math]::Min(1000, $message.Length)) })
    }
}
function Run-Prepare($case, [bool]$success = $true) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = [string]$case.exe
    $info.Arguments = [string]$case.arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    try {
        Assert-True ($process.Start()) 'Preparation process did not start'
        if (-not $process.WaitForExit(20000)) {
            $process.Kill()
            throw 'Preparation exceeded 20 seconds'
        }
        $null = $process.StandardOutput.ReadToEnd()
        $errorText = $process.StandardError.ReadToEnd()
        if ($success) {
            Assert-True ($process.ExitCode -eq 0) (
                'Preparation failed: ' + $errorText.Substring(
                    0, [Math]::Min(600, $errorText.Length)))
        } else {
            Assert-True ($process.ExitCode -ne 0) 'Unsafe workspace was accepted'
        }
    } finally { $process.Dispose() }
}
function New-Directory([string]$path, [string]$sddl) {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetSecurityDescriptorSddlForm($sddl)
    $null = [IO.Directory]::CreateDirectory($path, $acl)
}
function Descriptor([string]$path) {
    return [IO.Directory]::GetAccessControl($path).GetSecurityDescriptorSddlForm(
        [Security.AccessControl.AccessControlSections]'Access,Owner,Group')
}

try {
    Assert-True ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -eq
        'S-1-5-18') 'The harness must run as SYSTEM'
    Assert-True ($root -match
        '^C:\\Windows\\Temp\\inspect-exec-security-[a-f0-9]{32}$') (
        'Refusing unexpected fixture root')
    Assert-True (-not [IO.Directory]::Exists($root)) 'Fixture root already exists'
    Assert-True (-not [IO.Directory]::Exists($empty)) 'Empty fixture already exists'
    $parentSddl = Descriptor 'C:\Windows\Temp'
    New-Directory $root 'O:SYG:SYD:P(A;OICI;FA;;;SY)(A;OICI;GRGX;;;BU)'
    $created = $true

    Test-Case 'Temp ancestors are not reparse points' {
        $ancestor = [IO.Directory]::GetParent($root)
        while ($null -ne $ancestor) {
            Assert-True (([IO.File]::GetAttributes($ancestor.FullName) -band
                [IO.FileAttributes]::ReparsePoint) -eq 0) (
                'Temp has a reparse-point ancestor')
            $ancestor = $ancestor.Parent
        }
    }
    Test-Case 'Temp does not grant ordinary users directory replacement rights' {
        $acl = [IO.Directory]::GetAccessControl('C:\Windows\Temp')
        $privileged = @('S-1-5-18', 'S-1-5-32-544',
            'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
        Assert-True ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -in
            $privileged) 'Temp has a non-administrator owner'
        $rules = $acl.GetAccessRules($true, $true,
            [Security.Principal.SecurityIdentifier])
        $dangerous = [Security.AccessControl.FileSystemRights](
            'Delete,DeleteSubdirectoriesAndFiles,ChangePermissions,TakeOwnership')
        foreach ($rule in $rules) {
            if ($rule.AccessControlType -ne 'Allow' -or
                ($rule.PropagationFlags -band
                    [Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0) {
                continue
            }
            if (($rule.FileSystemRights -band $dangerous) -eq 0) { continue }
            Assert-True ($rule.IdentityReference.Value -in $privileged) (
                'Temp permits a non-administrator to replace private directories')
        }
    }
    Test-Case 'private directory has protected SYSTEM owner and inheritable DACL' {
        Run-Prepare $data.cases.private
        $path = [string]$data.cases.private.directory
        $acl = [IO.Directory]::GetAccessControl($path)
        Assert-True ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -eq
            'S-1-5-18') 'Wrong workspace owner'
        Assert-True ($acl.AreAccessRulesProtected) 'Workspace DACL inherits'
        $rules = @($acl.GetAccessRules($true, $true,
            [Security.Principal.SecurityIdentifier]))
        Assert-True ($rules.Count -eq 1) 'Workspace has extra access rules'
        $rule = $rules[0]
        Assert-True ($rule.IdentityReference.Value -eq 'S-1-5-18' -and
            $rule.AccessControlType -eq 'Allow' -and -not $rule.IsInherited -and
            $rule.FileSystemRights -eq 'FullControl' -and
            $rule.InheritanceFlags -eq 'ContainerInherit,ObjectInherit' -and
            $rule.PropagationFlags -eq 'None') 'Wrong workspace DACL'
        foreach ($name in @('script.bat', 'stdin', 'script.stdout',
                'script.stderr', 'script.returncode')) {
            $file = [IO.Path]::Combine($path, $name)
            [IO.File]::WriteAllText($file, 'owned test data')
            $child = [IO.File]::GetAccessControl($file)
            $childRules = @($child.GetAccessRules($true, $true,
                [Security.Principal.SecurityIdentifier]))
            Assert-True ($childRules.Count -eq 1 -and $childRules[0].IsInherited -and
                $childRules[0].IdentityReference.Value -eq 'S-1-5-18' -and
                $childRules[0].AccessControlType -eq 'Allow' -and
                $childRules[0].FileSystemRights -eq 'FullControl') (
                'A payload/control file did not inherit SYSTEM-only access')
        }
    }
    Test-Case 'preparation retry preserves existing payload and descriptor' {
        $path = [string]$data.cases.private.directory
        $before = Descriptor $path
        Run-Prepare $data.cases.private
        Assert-True ((Descriptor $path) -eq $before) 'Retry changed descriptor'
        Assert-True ([IO.File]::ReadAllText($path + '\stdin') -eq
            'owned test data') 'Retry changed payload'
    }
    Test-Case 'existing unsafe objects fail without permission repair' {
        $cases = @{
            foreign_owner = 'O:BAG:SYD:P(A;OICI;FA;;;SY)'
            weak_acl = 'O:SYG:SYD:P(A;OICI;FA;;;SY)(A;OICI;GR;;;BU)'
            unprotected_acl = 'O:SYG:SYD:(A;OICI;FA;;;SY)'
            noninheritable_acl = 'O:SYG:SYD:P(A;;FA;;;SY)'
        }
        foreach ($name in $cases.Keys) {
            $case = $data.cases.$name
            New-Directory $case.directory $cases[$name]
            $before = Descriptor $case.directory
            Run-Prepare $case $false
            Assert-True ((Descriptor $case.directory) -eq $before) (
                'Preparation repaired an unsafe object')
        }
        [IO.File]::WriteAllText($data.cases.file.directory, 'retained')
        Run-Prepare $data.cases.file $false
        Assert-True ([IO.File]::ReadAllText($data.cases.file.directory) -eq
            'retained') 'Preparation mutated a pre-existing file'
    }
    Test-Case 'directory junctions are rejected without touching their targets' {
        $target = $root + '\target'
        New-Directory $target 'O:SYG:SYD:P(A;OICI;FA;;;SY)'
        foreach ($name in @('junction', 'junction_parent')) {
            $link = $root + '\' + $name
            $null = & C:\Windows\System32\cmd.exe /d /c mklink /J $link $target
            Assert-True ($LASTEXITCODE -eq 0) 'Junction fixture failed'
        }
        $before = Descriptor $target
        Run-Prepare $data.cases.junction $false
        Run-Prepare $data.cases.'junction_parent\child' $false
        Assert-True ((Descriptor $target) -eq $before) 'Junction target ACL changed'
        Assert-True ([IO.Directory]::GetFileSystemEntries($target).Count -eq 0) (
            'Preparation wrote through a junction')
    }
    Test-Case 'real restricted token cannot read or mutate execution records' {
        # A restricting SID makes Windows require both the original token and
        # BUILTIN\Users to pass its access check. The public baseline must pass.
        Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
public static class InspectRestrictedAccess {
    [StructLayout(LayoutKind.Sequential)]
    struct SidAndAttributes { public IntPtr Sid; public uint Attributes; }
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool ConvertStringSidToSid(string value, out IntPtr sid);
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern bool CreateRestrictedToken(IntPtr token, uint flags,
        uint disabledCount, IntPtr disabled, uint privilegeCount, IntPtr privileges,
        uint restrictingCount, SidAndAttributes[] restricting, out IntPtr result);
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern bool ImpersonateLoggedOnUser(IntPtr token);
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern bool RevertToSelf();
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll")]
    static extern IntPtr LocalFree(IntPtr pointer);
    static void Check(bool success) {
        if (!success) throw new Win32Exception(Marshal.GetLastWin32Error());
    }
    static bool Allowed(Action action) {
        try { action(); return true; }
        catch (UnauthorizedAccessException) { return false; }
    }
    public static Dictionary<string, bool> Probe(string privatePath, string publicPath,
            string emptyPath) {
        IntPtr original = IntPtr.Zero, restricted = IntPtr.Zero, users = IntPtr.Zero;
        bool impersonating = false;
        var results = new Dictionary<string, bool>();
        try {
            Check(OpenProcessToken(Process.GetCurrentProcess().Handle, 0x000E,
                out original));
            Check(ConvertStringSidToSid("S-1-5-32-545", out users));
            Check(CreateRestrictedToken(original, 1, 0, IntPtr.Zero, 0,
                IntPtr.Zero, 1, new[] { new SidAndAttributes { Sid = users } },
                out restricted));
            Check(ImpersonateLoggedOnUser(restricted));
            impersonating = true;
            results["public_read"] = Allowed(() => File.ReadAllText(publicPath));
            results["public_write"] = Allowed(() =>
                File.AppendAllText(publicPath, "."));
            results["public_list"] = Allowed(() => Directory.GetFiles(
                Path.GetDirectoryName(publicPath)));
            foreach (string name in new[] { "script.bat", "stdin", "script.stdout",
                    "script.stderr", "script.returncode" }) {
                string path = Path.Combine(privatePath, name);
                results[name + "_read_denied"] = !Allowed(() => File.ReadAllText(path));
                results[name + "_write_denied"] = !Allowed(() =>
                    File.AppendAllText(path, "."));
            }
            results["private_list_denied"] = !Allowed(() =>
                Directory.GetFiles(privatePath));
            results["private_create_denied"] = !Allowed(() => File.WriteAllText(
                Path.Combine(privatePath, "unwanted"), "."));
            results["private_claim_denied"] = !Allowed(() => Directory.CreateDirectory(
                Path.Combine(privatePath, "claimed")));
            results["private_delete_denied"] = !Allowed(() =>
                Directory.Delete(emptyPath));
            return results;
        } finally {
            if (impersonating) Check(RevertToSelf());
            if (restricted != IntPtr.Zero) CloseHandle(restricted);
            if (original != IntPtr.Zero) CloseHandle(original);
            if (users != IntPtr.Zero) LocalFree(users);
        }
    }
}
'@
        $public = $root + '\public'
        [IO.File]::WriteAllText($public, 'public fixture')
        $publicAcl = [Security.AccessControl.FileSecurity]::new()
        $publicAcl.SetSecurityDescriptorSddlForm('O:SYG:SYD:P(A;;FA;;;SY)(A;;FA;;;BU)')
        [IO.File]::SetAccessControl($public, $publicAcl)
        Run-Prepare $data.cases.empty
        $checks = [InspectRestrictedAccess]::Probe(
            $data.cases.private.directory, $public, $empty)
        foreach ($check in $checks.GetEnumerator()) {
            Assert-True ($check.Value) ('Restricted token check failed: ' + $check.Key)
        }
    }
    Test-Case '4 MiB binary stdin preserves bytes and exact cleanup permits no replay' {
        $case = $data.large
        Assert-True ($case.launch.command_length -lt 8191 -and
            $case.cleanup.command_length -lt 8191) 'Windows command line is too long'
        Run-Prepare $case.prepare
        $payload = New-Object byte[] ([int]$case.length)
        [Random]::new(20261012).NextBytes($payload)
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $expected = [BitConverter]::ToString($sha.ComputeHash($payload)).Replace(
                '-', '').ToLowerInvariant()
        } finally { $sha.Dispose() }
        $offset = 0
        foreach ($path in $case.stdin_parts) {
            $length = [Math]::Min([int]$case.chunk_size, $payload.Length - $offset)
            $part = New-Object byte[] $length
            [Array]::Copy($payload, $offset, $part, 0, $length)
            [IO.File]::WriteAllBytes([string]$path, $part)
            $offset += $length
        }
        foreach ($part in $case.script_parts.PSObject.Properties) {
            [IO.File]::WriteAllBytes($part.Name,
                [Convert]::FromBase64String($part.Value))
        }
        Run-Prepare $case.launch
        $prefix = [string]$case.prefix
        Assert-True ([IO.File]::ReadAllText($prefix + 'script.returncode').Trim() -eq
            '0') 'Binary stdin command failed'
        Assert-True ([IO.File]::ReadAllText($prefix + 'script.stdout').Trim() -eq
            $expected) 'Binary stdin digest changed'
        Assert-True ([IO.File]::ReadAllText($prefix + 'script.stderr').Length -eq
            0) 'Binary stdin command produced errors'
        [IO.File]::WriteAllText($case.unrelated, 'retained')
        Run-Prepare $case.cleanup
        foreach ($path in $case.cleanup_paths) {
            Assert-True (-not [IO.File]::Exists([string]$path)) (
                'Cleanup left owned data')
        }
        Assert-True ([IO.Directory]::Exists($prefix + 'claimed')) 'Claim was deleted'
        Assert-True ([IO.File]::ReadAllText($prefix + 'script.returncode').Trim() -eq
            '0') 'Terminal record was deleted'
        Assert-True ([IO.File]::ReadAllText($case.unrelated) -eq
            'retained') 'Cleanup deleted an unlisted chunk filename'
        Run-Prepare $case.launch
        Assert-True ([IO.File]::ReadAllLines($prefix + 'side').Count -eq
            1) 'Delayed launch replayed the command'
    }
} catch {
    $message = [string]$_.Exception.Message
    $results.Add([ordered]@{ name = 'harness'; passed = $false;
        error = $message.Substring(0, [Math]::Min(1000, $message.Length)) })
} finally {
    if ($created) {
        try {
            foreach ($name in @('junction', 'junction_parent')) {
                $link = $root + '\' + $name
                if ([IO.Directory]::Exists($link)) { [IO.Directory]::Delete($link) }
            }
            [IO.Directory]::Delete($root, $true)
            if ([IO.Directory]::Exists($empty)) { [IO.Directory]::Delete($empty) }
        } catch {
            $results.Add([ordered]@{ name = 'owned fixture cleanup'; passed = $false;
                error = 'Could not remove an owned fixture' })
        }
    }
}
$failed = @($results | Where-Object { -not $_.passed }).Count
[ordered]@{ passed = ($failed -eq 0); failed = $failed;
    elapsed_ms = $started.ElapsedMilliseconds; parent_sddl = $parentSddl;
    results = @($results.ToArray())
} | ConvertTo-Json -Depth 5 -Compress
if ($failed -ne 0) { exit 1 }
exit 0
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output", type=Path, help="self-contained PowerShell output path"
    )
    args = parser.parse_args()
    args.output.write_text(build_harness(), encoding="ascii", newline="\r\n")


if __name__ == "__main__":
    main()
