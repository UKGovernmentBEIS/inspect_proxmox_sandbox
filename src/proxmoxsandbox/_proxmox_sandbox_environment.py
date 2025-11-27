import base64
import errno
import re
import shlex
import time
from logging import getLogger
from pathlib import Path
from typing import Any, Dict, Generator, List, Tuple, Type, Union

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
from proxmoxsandbox._impl.task_wrapper import TaskWrapper
from proxmoxsandbox._proxmox_pool import ProxmoxPoolABC, QueueBasedProxmoxPool
from proxmoxsandbox.schema import (
    ProxmoxInstanceConfig,
    ProxmoxSandboxEnvironmentConfig,
    SdnConfigType,
)


@sandboxenv(name="proxmox")
class ProxmoxSandboxEnvironment(SandboxEnvironment):
    """An Inspect sandbox environment for Proxmox virtual machines."""

    logger = getLogger(__name__)

    TRACE_NAME = "proxmox_sandbox_environment"

    proxmox_pool: Type[ProxmoxPoolABC] = QueueBasedProxmoxPool

    # Instance variables
    infra_commands: InfraCommands
    agent_commands: AgentCommands
    qemu_commands: QemuCommands
    task_wrapper: TaskWrapper
    built_in_vm: BuiltInVM
    sdn_config: SdnConfigType
    vm_id: int
    all_vm_ids: Tuple[int, ...]
    sdn_zone_id: str | None
    # Multi-instance pool fields
    instance: ProxmoxInstanceConfig | None
    pool_id: str | None

    def __init__(
        self,
        proxmox: AsyncProxmoxAPI,
        node: str,
        sdn_config: SdnConfigType,
        vm_id: int,
        all_vm_ids: Tuple[int, ...],
        sdn_zone_id: str | None,
        instance: ProxmoxInstanceConfig | None = None,
        pool_id: str | None = None,
    ):
        self.infra_commands = InfraCommands(async_proxmox=proxmox, node=node)
        self.agent_commands = AgentCommands(async_proxmox=proxmox, node=node)
        self.qemu_commands = QemuCommands(async_proxmox=proxmox, node=node)
        self.built_in_vm = BuiltInVM(async_proxmox=proxmox, node=node)
        self.task_wrapper = TaskWrapper(async_proxmox=proxmox)
        self.sdn_config = sdn_config
        self.vm_id = vm_id
        self.all_vm_ids = all_vm_ids
        self.sdn_zone_id = sdn_zone_id
        self.instance = instance
        self.pool_id = pool_id

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
        """Return the default concurrency limit from the pool implementation.

        Returns:
            Maximum number of concurrent samples, or None for unlimited.
        """
        return cls.proxmox_pool.default_concurrency()

    @classmethod
    @override
    async def task_init(
        cls, task_name: str, config: SandboxEnvironmentConfigType | None
    ) -> None:
        # Note: config may be None or a task-level config.
        # In practice (e.g., inspect_cyber), there's ONE task with many samples,
        # each sample having its own config passed to sample_init.
        # We always create pools here regardless of config, since pool creation
        # only depends on the underlying infrastructure (defined in
        # PROXMOX_CONFIG_FILE), not on eval-specific config.
        await cls.create_proxmox_instance_pools()
        return None

    @classmethod
    async def create_proxmox_instance_pools(cls) -> None:
        """Initialize the Proxmox instance pools using the configured pool class."""
        await cls.proxmox_pool.initialize()

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

        # Get the pool_id for this specific sample
        pool_id = config.instance_pool_id

        # ACQUIRE instance from pool (blocks if all in use)
        instance = await cls.proxmox_pool.acquire_instance(pool_id)
        cls.logger.info(
            f"Acquired instance {instance.instance_id} from pool '{pool_id}'"
        )

        # Track variables for cleanup on failure
        infra_commands = None
        proxmox_ids_start = None

        try:
            # Create API using acquired instance's credentials
            async_proxmox_api = AsyncProxmoxAPI(
                host=f"{instance.host}:{instance.port}",
                user=f"{instance.user}@{instance.user_realm}",
                password=instance.password,
                verify_tls=instance.verify_tls,
            )

            infra_commands = InfraCommands(
                async_proxmox=async_proxmox_api, node=instance.node
            )

            # Sanity check previous cleanups worked
            # N.B. this code only makes sense in the "single eval per proxmox"
            # model and should not be merged to main!
            await cls._ensure_instance_clean(infra_commands, instance.instance_id)

            task_name_start = re.sub("[^a-zA-Z0-9]", "x", task_name[:3].lower())

            proxmox_ids_start = await infra_commands.find_proxmox_ids_start(
                task_name_start
            )

            # Ensure built-in VM templates exist on this instance
            await ProxmoxSandboxEnvironment.ensure_vms(
                async_proxmox_api=async_proxmox_api, config=config
            )

            async with concurrency(f"proxmox-{instance.host}", 1):
                (
                    vm_configs_with_ids,
                    sdn_zone_id,
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
                    node=instance.node,
                    sdn_config=config.sdn_config,
                    vm_id=vm_config_and_id[0],
                    all_vm_ids=vm_ids,
                    sdn_zone_id=sdn_zone_id,
                    instance=instance,
                    pool_id=pool_id,
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
                    "No default sandbox found: at least one VM must have "
                    "is_sandbox = True"
                )

            # borrowed from k8s provider
            def reorder_default_first(
                sandboxes: dict[str, SandboxEnvironment],
            ) -> dict[str, SandboxEnvironment]:
                # Inspect expects the default sandbox to be the first
                # sandbox in the dict.
                if "default" in sandboxes:
                    default = sandboxes.pop("default")
                    return {"default": default, **sandboxes}
                return sandboxes

            return reorder_default_first(sandboxes)

        except Exception as e:
            # Attempt to clean up any partial infrastructure before
            # releasing instance. This prevents leftover VMs/SDN from
            # causing conflicts when the instance is reused.
            cleanup_succeeded = False

            # Only attempt cleanup if we got far enough to allocate IDs.
            # If we have proxmox_ids_start, infrastructure creation was attempted.
            should_attempt_cleanup = (
                infra_commands is not None and proxmox_ids_start is not None
            )

            if should_attempt_cleanup:
                try:
                    cls.logger.info(
                        f"Attempting cleanup of partial infrastructure "
                        f"for instance {instance.instance_id} "
                        f"after sample_init failure: {e}"
                    )

                    # Use cleanup_no_id to discover and clean up all VMs/zones.
                    # This is safe because only one sample runs per instance at a time,
                    # so all inspect-tagged VMs belong to this failed sample.
                    await infra_commands.cleanup_no_id(skip_confirmation=True)  # type: ignore mypy hasn't clocked that infra_commands is not None here

                    cleanup_succeeded = True
                    cls.logger.info(
                        f"Successfully cleaned up partial infrastructure "
                        f"for instance {instance.instance_id}"
                    )
                except Exception as cleanup_ex:
                    # Log cleanup failure but don't mask the original error
                    cls.logger.warning(
                        f"Failed to clean up partial infrastructure "
                        f"for instance {instance.instance_id}: {cleanup_ex}. "
                        f"This may leave resources on the server."
                    )
            else:
                # Early failure or no SDN requested - instance is clean
                cleanup_succeeded = True

            # Only return instance to pool after successful cleanup.
            # Dirty instances would cause cascading failures across samples.
            if cleanup_succeeded:
                cls.logger.info(
                    f"Releasing instance {instance.instance_id} "
                    f"from pool '{pool_id}' back to queue"
                )
                await cls.proxmox_pool.release_instance(pool_id, instance)
            else:
                cls.logger.warning(
                    f"NOT releasing instance {instance.instance_id} "
                    f"from pool '{pool_id}' - "
                    f"cleanup failed, instance may be dirty"
                )

            raise

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
    async def _ensure_instance_clean(
        cls, infra_commands: InfraCommands, instance_id: str
    ) -> None:
        """Ensure instance has no leftover VNETs. Clean up if needed.

        Logs errors but does not raise - if the instance is dirty,
        the subsequent setup will fail and the error handler will deal with it.
        """
        try:
            vnets = await infra_commands.sdn_commands.read_all_vnets()

            if vnets:
                cls.logger.warning(
                    f"Instance {instance_id} has {len(vnets)} leftover VNETs! "
                    f"Cleaning up before proceeding..."
                )
                await infra_commands.cleanup_no_id(skip_confirmation=True)
                cls.logger.info(f"Pre-cleaned instance {instance_id}")
        except Exception as e:
            cls.logger.error(
                f"Failed to check/clean instance {instance_id}: {type(e).__name__}: {e}. "
                f"Proceeding anyway - setup will fail if instance is dirty."
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
        # Get instance and pool_id from first environment (for returning to pool)
        any_vm_sandbox_environment: ProxmoxSandboxEnvironment | None = None
        instance: ProxmoxInstanceConfig | None = None
        pool_id: str | None = None

        for env in environments.values():
            if isinstance(env, ProxmoxSandboxEnvironment):
                # we only need a single VM sandbox to have enough information
                # to tear them all down
                any_vm_sandbox_environment = env
                instance = env.instance
                pool_id = env.pool_id
                break

        cleanup_succeeded = False
        try:
            if any_vm_sandbox_environment is not None:
                async with concurrency(f"proxmox-{instance.host}", 1):
                    await any_vm_sandbox_environment.infra_commands.delete_sdn_and_vms(
                        sdn_zone_id=any_vm_sandbox_environment.sdn_zone_id,
                        vm_ids=any_vm_sandbox_environment.all_vm_ids,
                    )
                cleanup_succeeded = True
                instance_id = instance.instance_id if instance else "unknown"
                cls.logger.info(
                    f"Successfully cleaned up VMs for instance {instance_id}"
                )
        except Exception as ex:
            instance_id = instance.instance_id if instance else "unknown"
            cls.logger.error(f"Cleanup failed for instance {instance_id}: {ex}")
            raise
        finally:
            # Only return instances to the pool after successful cleanup.
            # Dirty instances would cause the next sample to fail when it
            # finds leftover VMs. This may exhaust the pool but prevents
            # cascading failures across samples.
            if instance is not None and pool_id is not None:
                if cleanup_succeeded:
                    cls.logger.info(
                        f"Releasing instance {instance.instance_id} "
                        f"from pool '{pool_id}' back to queue"
                    )
                    await cls.proxmox_pool.release_instance(pool_id, instance)
                else:
                    cls.logger.warning(
                        f"NOT releasing instance {instance.instance_id} "
                        f"from pool '{pool_id}' - "
                        f"cleanup failed, instance may be dirty\n"
                        f"instance={instance}\n"
                        f"pool_id={pool_id}"
                        f"cleanup_succeeded={cleanup_succeeded}"
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
        return
        # We do all our cleanup in sample cleanup so this will mostly do nothing
        # This will cleanup leftover VMs from failed per sample cleanups
        # if cleanup:
        #     for instance in cls.proxmox_pool.all_instances():
        #         async_proxmox_api = AsyncProxmoxAPI(
        #             host=f"{instance.host}:{instance.port}",
        #             user=f"{instance.user}@{instance.user_realm}",
        #             password=instance.password,
        #             verify_tls=instance.verify_tls,
        #         )
        #         infra_commands = InfraCommands(
        #             async_proxmox=async_proxmox_api, node=instance.node
        #         )
        #
        #         await infra_commands.cleanup()
        # else:
        #     print(
        #         "\nCleanup all sandbox releases with: "
        #         "[blue]inspect sandbox cleanup proxmox[/blue]\n"
        #     )

    @classmethod
    @override
    async def cli_cleanup(cls, id: str | None) -> None:
        if id is None:
            await cls.create_proxmox_instance_pools()
            for instance in cls.proxmox_pool.all_instances():
                async_proxmox_api = AsyncProxmoxAPI(
                    host=f"{instance.host}:{instance.port}",
                    user=f"{instance.user}@{instance.user_realm}",
                    password=instance.password,
                    verify_tls=instance.verify_tls,
                )
                infra_commands = InfraCommands(
                    async_proxmox=async_proxmox_api, node=instance.node
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
