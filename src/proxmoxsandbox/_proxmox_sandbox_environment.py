import base64
import errno
import re
import shlex
import time
from logging import getLogger
from pathlib import Path
from typing import Any, Dict, Generator, List, Tuple, Union

import tenacity
from inspect_ai.util import (
    ExecResult,
    OutputLimitExceededError,
    SandboxConnection,
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
    SandboxEnvironmentLimits,
    concurrency,
    sandboxenv,
    trace_action,
)
from pydantic import BaseModel
from typing_extensions import override

from proxmoxsandbox._impl.agent_commands import AgentCommands
from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.built_in_vm import BuiltInVM
from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox._impl.sdn_commands import IpamMapping
from proxmoxsandbox._impl.task_wrapper import TaskWrapper
from proxmoxsandbox.schema import (
    ProxmoxSandboxEnvironmentConfig,
    SdnConfigType,
)


@sandboxenv(name="proxmox")
class ProxmoxSandboxEnvironment(SandboxEnvironment):
    """An Inspect sandbox environment for Proxmox virtual machines."""

    logger = getLogger(__name__)

    TRACE_NAME = "proxmox_sandbox_environment"

    infra_commands: InfraCommands
    agent_commands: AgentCommands
    qemu_commands: QemuCommands
    task_wrapper: TaskWrapper
    built_in_vm: BuiltInVM
    all_ipam_mappings: Tuple[IpamMapping, ...]
    sdn_config: SdnConfigType
    vm_id: int
    all_vm_ids: Tuple[int, ...]
    sdn_zone_id: str | None

    def __init__(
        self,
        proxmox: AsyncProxmoxAPI,
        node: str,
        ipam_mappings: Tuple[IpamMapping, ...],
        sdn_config: SdnConfigType,
        vm_id: int,
        all_vm_ids: Tuple[int, ...],
        sdn_zone_id: str | None,
    ):
        self.infra_commands = InfraCommands(async_proxmox=proxmox, node=node)
        self.agent_commands = AgentCommands(async_proxmox=proxmox, node=node)
        self.qemu_commands = QemuCommands(async_proxmox=proxmox, node=node)
        self.built_in_vm = BuiltInVM(async_proxmox=proxmox, node=node)
        self.task_wrapper = TaskWrapper(async_proxmox=proxmox)
        self.all_ipam_mappings = ipam_mappings
        self.sdn_config = sdn_config
        self.vm_id = vm_id
        self.all_vm_ids = all_vm_ids
        self.sdn_zone_id = sdn_zone_id

    # originally from k8s sandbox
    def _pipe_user_input(self, stdin: str | bytes) -> str:
        # Encode the user-provided input as base64 for 2 reasons:
        # 1. To avoid issues with special characters (e.g. new lines) in the input.
        # 2. To support binary input (e.g. null byte).
        stdin_b64 = base64.b64encode(
            stdin if isinstance(stdin, bytes) else stdin.encode("utf-8")
        ).decode("ascii")
        # The below comment may or may not be relevant to this sandbox provider.
        # Pipe user input. Simply writing it to the shell's stdin after a command e.g.
        # `cat` results in `cat` blocking indefinitely as there is no way to close the
        # stdin stream in v4.channel.k8s.io.
        return f"echo '{stdin_b64}' | base64 -d | "

    # originally from k8s sandbox
    def _prefix_timeout(self, timeout: int | None) -> str:
        if timeout is None:
            return ""
        # Enforce timeout using `timeout`. Cannot enforce this on the client side
        # (requires terminating the remote process).
        # `-k 5s` sends SIGKILL after grace period in case user command doesn't respect
        # SIGTERM.
        return f"timeout -k 5s {timeout}s "

    # originally from k8s sandbox
    # TODO extract this to its own module and unit test it locally
    def _build_shell_script(
        self,
        tmp_start: str,
        command: List[str],
        stdin: str | bytes | None,
        cwd: str | None,
        env: dict[str, str],
        user: str | None,
        timeout: int | None,
    ) -> str:
        def generate() -> Generator[str, None, None]:
            yield (
                f"rm -f {tmp_start}script.stdout"
                + f" {tmp_start}script.stderr"
                + f" {tmp_start}script.returncode\n"
            )
            if user is not None:
                yield f"su -l {shlex.quote(user)} << 'EOF{tmp_start}EOF'\n"
            # The rest of the script gets quoted in a heredoc if we had to use su
            if cwd is not None:
                yield f"cd {shlex.quote(cwd)} || exit $?\n"
            for key, value in env.items():
                yield f"export {shlex.quote(key)}={shlex.quote(value)}\n"
            if stdin is not None:
                yield self._pipe_user_input(stdin)
            yield (
                f"{self._prefix_timeout(timeout)}{shlex.join(command)}"
                + f" >{tmp_start}script.stdout"
                + f" 2>{tmp_start}script.stderr\n"
                + 'echo -n "$?" >'
                + f" {tmp_start}script.returncode\n"
            )
            yield "sync\n"
            if user is not None:
                yield f"EOF{tmp_start}EOF\n"

        return "".join(generate())

    @staticmethod
    async def ensure_vms(
        async_proxmox_api: AsyncProxmoxAPI, config: ProxmoxSandboxEnvironmentConfig
    ) -> None:
        built_in_vm = BuiltInVM(async_proxmox=async_proxmox_api, node=config.node)
        built_in_names = set()
        for vm_config in config.vms_config:
            if vm_config.vm_source_config.built_in is not None:
                built_in_names.add(vm_config.vm_source_config.built_in)
        for built_in_name in built_in_names:
            await built_in_vm.ensure_exists(built_in_name)

    @classmethod
    @override
    def config_files(cls) -> List[str]:
        return []

    @classmethod
    @override
    def default_concurrency(cls) -> int | None:
        return None

    @classmethod
    @override
    async def task_init(
        cls, task_name: str, config: SandboxEnvironmentConfigType | None
    ) -> None:
        if config is not None:
            if not isinstance(config, ProxmoxSandboxEnvironmentConfig):
                raise ValueError("config must be a ProxmoxSandboxEnvironmentConfig")
            async_proxmox_api = cls._create_async_proxmox_api(config)
            await ProxmoxSandboxEnvironment.ensure_vms(async_proxmox_api, config)
        return None

    @classmethod
    @override
    async def sample_init(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        metadata: dict[str, str],
    ) -> dict[str, SandboxEnvironment]:
        if config is None:
            config = ProxmoxSandboxEnvironmentConfig()
        if not isinstance(config, ProxmoxSandboxEnvironmentConfig):
            raise ValueError("config must be a ProxmoxSandboxEnvironmentConfig")

        async_proxmox_api = cls._create_async_proxmox_api(config)

        infra_commands = InfraCommands(
            async_proxmox=async_proxmox_api, node=config.node
        )

        task_name_start = re.sub("[^a-zA-Z0-9]", "x", task_name[:3].lower())

        proxmox_ids_start = await infra_commands.find_proxmox_ids_start(task_name_start)

        await ProxmoxSandboxEnvironment.ensure_vms(
            async_proxmox_api=async_proxmox_api, config=config
        )

        async with concurrency("proxmox", 1):
            (
                vm_configs_with_ids,
                sdn_zone_id,
                ipam_mappings,
            ) = await infra_commands.create_sdn_and_vms(
                proxmox_ids_start,
                sdn_config=config.sdn_config,
                vms_config=config.vms_config,
            )

        sandboxes: Dict[str, SandboxEnvironment] = {}

        vm_ids = tuple(
            vm_configs_with_id[0] for vm_configs_with_id in vm_configs_with_ids
        )

        found_default = False

        for idx, vm_config_and_id in enumerate(vm_configs_with_ids):
            vm_sandbox_environment = ProxmoxSandboxEnvironment(
                proxmox=async_proxmox_api,
                node=config.node,
                ipam_mappings=ipam_mappings,
                sdn_config=config.sdn_config,
                vm_id=vm_config_and_id[0],
                all_vm_ids=vm_ids,
                sdn_zone_id=sdn_zone_id,
            )
            if not found_default and vm_config_and_id[1].is_sandbox:
                sandboxes["default"] = vm_sandbox_environment
                found_default = True
            else:
                sandbox_name = (
                    vm_config_and_id[1].name
                    if vm_config_and_id[1].name is not None
                    else f"vm_{vm_config_and_id[0]}"
                )
                sandboxes[sandbox_name] = vm_sandbox_environment

        if not found_default:
            raise ValueError(
                "No default sandbox found: at least one VM must have is_sandbox = True"
            )

        # borrowed from k8s provider
        def reorder_default_first(
            sandboxes: dict[str, SandboxEnvironment],
        ) -> dict[str, SandboxEnvironment]:
            # Inspect expects the default sandbox to be the first sandbox in the dict.
            if "default" in sandboxes:
                default = sandboxes.pop("default")
                return {"default": default, **sandboxes}
            return sandboxes

        return reorder_default_first(sandboxes)

    @classmethod
    def _create_async_proxmox_api(
        cls, config: ProxmoxSandboxEnvironmentConfig
    ) -> AsyncProxmoxAPI:
        return AsyncProxmoxAPI(
            host=f"{config.host}:{config.port}",
            user=f"{config.user}@{config.user_realm}",
            password=config.password,
            verify_tls=config.verify_tls,
        )

    @classmethod
    @override
    async def sample_cleanup(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        environments: Dict[str, SandboxEnvironment],
        interrupted: bool,
    ) -> None:
        if not interrupted:
            any_vm_sandbox_environment: ProxmoxSandboxEnvironment | None = None
            for env in environments.values():
                if isinstance(env, ProxmoxSandboxEnvironment):
                    # we only need a single VM sandbox to have enough information
                    # to tear them all down
                    any_vm_sandbox_environment = env

            if any_vm_sandbox_environment is not None:
                async with concurrency("proxmox", 1):
                    await any_vm_sandbox_environment.infra_commands.delete_sdn_and_vms(
                        sdn_zone_id=any_vm_sandbox_environment.sdn_zone_id,
                        ipam_mappings=any_vm_sandbox_environment.all_ipam_mappings,
                        vm_ids=any_vm_sandbox_environment.all_vm_ids,
                    )
        return None

    @classmethod
    @override
    async def task_cleanup(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        cleanup: bool,
    ) -> None:
        if config is None:
            config = ProxmoxSandboxEnvironmentConfig()

        if not isinstance(config, ProxmoxSandboxEnvironmentConfig):
            raise ValueError("config must be a ProxmoxSandboxEnvironmentConfig")

        if cleanup:
            infra_commands = InfraCommands(
                async_proxmox=cls._create_async_proxmox_api(config), node=config.node
            )
            await infra_commands.cleanup()
        else:
            print(
                "\nCleanup all sandbox releases with: "
                "[blue]inspect sandbox cleanup proxmox[/blue]\n"
            )

    @classmethod
    @override
    async def cli_cleanup(cls, id: str | None) -> None:
        if id is None:
            config = ProxmoxSandboxEnvironmentConfig()
            async_proxmox_api = cls._create_async_proxmox_api(config)
            infra_commands = InfraCommands(
                async_proxmox=async_proxmox_api, node=config.node
            )
            await infra_commands.cleanup_no_id()
        else:
            print("\n[red]Cleanup by ID not implemented[/red]\n")

    @classmethod
    @override
    def config_deserialize(cls, config: dict[str, Any]) -> BaseModel:
        return ProxmoxSandboxEnvironmentConfig(**config)

    @override
    async def exec(
        self,
        cmd: List[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] = {},
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
    ) -> ExecResult[str]:
        if self.vm_id is None:
            raise ValueError("VM ID is not set")

        tmp_start = f"/tmp/{__name__}{time.time_ns()}_"

        @tenacity.retry(
            wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
            stop=tenacity.stop_after_delay(timeout)
            if timeout is not None
            else tenacity.stop_never,
            retry=tenacity.retry_if_result(lambda x: x is False),
        )
        async def wait_for_exec(vm_id: int, exec_response_pid: int) -> bool | Dict:
            # TODO check return code of exec - even if the command failed
            # it should always be timeout or success
            #
            # Note: get_agent_exec_status can only be called once
            # per PID after the process is complete.
            # Do not, for example, try to debug the value of the get_agent_exec_status
            # call. It will break the running code in this loop.
            exec_status = await self.agent_commands.get_agent_exec_status(
                vm_id=vm_id, pid=exec_response_pid
            )

            if exec_status["exited"] != 1:
                return False
            else:
                return exec_status

        script = self._build_shell_script(
            tmp_start=tmp_start,
            command=cmd,
            stdin=input,
            cwd=cwd,
            env=env,
            user=user,
            timeout=timeout,
        )

        await self._write_file_only(f"{tmp_start}script.sh", script)

        exec_post_response = await self.agent_commands.exec_command(
            vm_id=self.vm_id, command=["sh", f"{tmp_start}script.sh"]
        )

        exec_response_pid = exec_post_response["pid"]

        assert isinstance(exec_response_pid, int)

        with trace_action(
            self.logger,
            self.TRACE_NAME,
            f"exec_command {self.vm_id=} {exec_response_pid=}",
        ):
            exec_status = await wait_for_exec(self.vm_id, exec_response_pid)

        if exec_status and isinstance(exec_status, Dict) and "err-data" in exec_status:
            # Something went wrong with the wrapper script, not the actual command
            # Possibly user not found. We'll return the error of the wrapper script,
            # in case that's helpful
            stdout = exec_status.get("out-data", "")
            stderr = exec_status.get("err-data", "")
            returncode = exec_status["exitcode"]
            exec_response = ExecResult(
                success=False,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )
        else:
            # TODO: consider reading all files at once?
            stdout = (
                await self.agent_commands.read_file_or_blank(
                    vm_id=self.vm_id,
                    filepath=f"{tmp_start}script.stdout",
                    max_size=SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE,
                )
            )["content"]
            stderr = (
                await self.agent_commands.read_file_or_blank(
                    vm_id=self.vm_id,
                    filepath=f"{tmp_start}script.stderr",
                    max_size=SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE,
                )
            )["content"]
            returncode = await self._read_return_code(tmp_start)
            exec_response = ExecResult(
                success=returncode == 0,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )

        # cleanup - we don't need to wait for the result of this
        await self.agent_commands.exec_command(
            vm_id=self.vm_id,
            command=["sh", "-c", f"rm -f {tmp_start}*"],
        )

        if exec_response.returncode == 124:
            raise TimeoutError("Command timed out")

        if len(exec_response.stderr.splitlines()) == 1:
            # if err-data is longer than one line, then part of the script ran,
            # and it didn't fail on the first line, which is characteristic of
            # failing to execute a non-executable file
            if (
                exec_response.returncode == 126
                and "permission denied" in exec_response.stderr.casefold()
            ):
                raise PermissionError("Permission denied executing command")

        return exec_response

    @tenacity.retry(
        wait=tenacity.wait_exponential(min=0.1, exp_base=1.3),
        stop=tenacity.stop_after_delay(2),
        retry_error_callback=lambda retry_state: 124,
    )
    async def _read_return_code(self, tmp_start):
        returncode_string = (
            await self.agent_commands.read_file_or_blank(
                vm_id=self.vm_id,
                filepath=f"{tmp_start}script.returncode",
                max_size=SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE,
            )
        )["content"]
        returncode_string_stripped = returncode_string.strip()
        if len(returncode_string_stripped) == 0:
            raise ValueError("Return code file is empty")
        return int(returncode_string_stripped)

    async def _write_file_only(self, file: str, contents: str | bytes) -> None:
        if self.vm_id is None:
            raise ValueError("VM ID is not set")
        try:
            await self.agent_commands.write_file(
                vm_id=self.vm_id,
                content=contents
                if isinstance(contents, bytes)
                else contents.encode("UTF-8"),
                filepath=file,
            )
        except Exception as ex:
            if "Agent error" in str(ex):
                if "No such file or directory" in str(ex):
                    raise FileNotFoundError(
                        errno.ENOENT, "No such file or directory.", file
                    )
                elif "Is a directory" in str(ex):
                    raise IsADirectoryError(errno.EISDIR, "Is a directory", file)
                else:
                    raise ex
            else:
                raise ex

    @override
    async def write_file(self, file: str, contents: str | bytes) -> None:
        # Writes contents to file, handling large files by splitting them into chunks
        # and recombining using cat.

        CHUNK_SIZE = (
            40 * 1024
        )  # 40KB chunks to be safe, to take base64 encoding into account
        # note this 40KB limit was based on the Proxmox <=8.3 limit of
        # 60Kb, but this was increased in Proxmox 8.4, so could
        # potentially be increased here. Would need to check the
        # version number to ensure backward compatibility.

        await self.exec(cmd=["mkdir", "-p", "--", str(Path(file).parent.as_posix())])

        # If content is small enough, write directly
        if len(contents) <= CHUNK_SIZE:
            await self._write_file_only(file, contents)
            return

        # For large contents, split into chunks
        chunks = [
            contents[i : i + CHUNK_SIZE] for i in range(0, len(contents), CHUNK_SIZE)
        ]

        # Calculate padding width based on number of chunks
        padding_width = len(str(len(chunks) - 1))

        tmp_start = f"/tmp/{__name__}_write_file_{time.time_ns()}_"

        temp_dir = f"{tmp_start}split_{Path(file).name}"
        try:
            await self.exec(cmd=["mkdir", "-p", "--", temp_dir])

            # Write chunks to temp files with zero-padded numbers
            for i, chunk in enumerate(chunks):
                chunk_file = f"{temp_dir}/chunk_{i:0{padding_width}d}"
                await self._write_file_only(chunk_file, chunk)

            combine_script = (
                f"rm -f {file}\n"
                f'for i in $(seq -f "%0{padding_width}.0f" 0 {len(chunks) - 1}); do\n'
                f'  cat "{temp_dir}/chunk_$i" >> {file}\n'
                f"done\n"
            )
            combine_script_path = f"{temp_dir}/combine.sh"
            await self._write_file_only(combine_script_path, combine_script)
            await self.exec(cmd=["sh", combine_script_path])

        finally:
            await self.exec(cmd=["rm", "-rf", temp_dir])

    @override
    async def read_file(self, file: str, text: bool = True) -> Union[str | bytes]:  # type: ignore
        """Read a file from the sandbox environment.

        File size is limited to 16 MiB - this is a limitation of proxmox.
        This is a deviation from the Inspect spec which states 100 MiB.
        """
        if self.vm_id is None:
            raise ValueError("VM ID is not set")
        # Note, per https://pve.proxmox.com/pve-docs/api-viewer/index.html#/nodes/{node}/qemu/{vm_id}/agent/file-read
        # read from proxmox API is limited to 16777216 bytes
        try:
            read_get_response = await self.agent_commands.read_file(
                vm_id=self.vm_id,
                filepath=file,
                max_size=min(SandboxEnvironmentLimits.MAX_READ_FILE_SIZE, 16777216),
            )
        except Exception as ex:
            if "Agent error" in str(ex):
                if "No such file or directory" in str(ex):
                    raise FileNotFoundError(
                        errno.ENOENT, "No such file or directory.", file
                    )
                elif "Is a directory" in str(ex):
                    raise IsADirectoryError(errno.EISDIR, "Is a directory", file)
                else:
                    raise ex
            else:
                raise ex
        if (
            getattr(read_get_response, "truncated", False)
            or len(read_get_response["content"])
            >= SandboxEnvironmentLimits.MAX_READ_FILE_SIZE
        ):
            raise OutputLimitExceededError("Output size exceeds 16 MiB limit.", file)
        mangled_response = read_get_response["content"]
        bytes_data = mangled_response.encode("iso-8859-1")
        if text:
            return bytes_data.decode("utf-8")
        else:
            return bytes_data

    @override
    async def connection(self) -> SandboxConnection:
        """
        Returns a connection to the sandbox.

        Raises:
           NotImplementedError: For sandboxes that don't provide connections
           ConnectionError: If sandbox is not currently running.
        """
        if self.vm_id is None:
            raise ConnectionError("Sandbox is not running")
        return SandboxConnection(
            type="proxmox",
            command=f"open '{await self.qemu_commands.connection_url(self.vm_id)}'",
        )

    async def create_snapshot(self, snapshot_name: str) -> None:
        """Creates a snapshot of the VM."""

        async def snapshotter() -> None:
            await self.agent_commands.create_snapshot(
                vm_id=self.vm_id, snapshot_name=snapshot_name
            )

        await self.task_wrapper.do_action_and_wait_for_tasks(snapshotter)

    async def restore_snapshot(self, snapshot_name: str) -> None:
        """Restores a snapshot of the VM."""

        async def snapshotter() -> None:
            await self.agent_commands.rollback_to_snapshot(
                vm_id=self.vm_id, snapshot_name=snapshot_name
            )

        await self.task_wrapper.do_action_and_wait_for_tasks(snapshotter)
        await self.infra_commands.qemu_commands.await_vm(
            vm_id=self.vm_id, is_sandbox=True
        )
