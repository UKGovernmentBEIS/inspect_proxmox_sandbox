import base64
import errno
import re
import shlex
import time
from logging import getLogger
from pathlib import Path, PureWindowsPath
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
from proxmoxsandbox._impl.infra_commands import InfraCommands, ProxmoxTarget
from proxmoxsandbox._impl.iso_write import IsoWriter
from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox._impl.sdn_commands import ZONE_REGEX, IpamMapping
from proxmoxsandbox._impl.task_wrapper import TaskWrapper
from proxmoxsandbox._proxmox_pool import ProxmoxPoolABC, QueueBasedProxmoxPool
from proxmoxsandbox.schema import (
    OsType,
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
    all_ipam_mappings: Tuple[IpamMapping, ...]
    vm_id: int
    all_vm_ids: Tuple[int, ...]
    sdn_zone_id: str | None
    # Multi-instance pool fields
    instance: ProxmoxInstanceConfig | None
    pool_id: str | None
    # OS type for Windows support
    os_type: OsType | None

    def __init__(
        self,
        infra_commands: InfraCommands,
        agent_commands: AgentCommands,
        ipam_mappings: Tuple[IpamMapping, ...],
        vm_id: int,
        all_vm_ids: Tuple[int, ...],
        sdn_zone_id: str | None,
        instance: ProxmoxInstanceConfig | None = None,
        pool_id: str | None = None,
        os_type: OsType | None = None,
    ):
        self.infra_commands = infra_commands
        self.agent_commands = agent_commands
        self.qemu_commands = infra_commands.qemu_commands
        self.task_wrapper = infra_commands.task_wrapper
        self.all_ipam_mappings = ipam_mappings
        self.vm_id = vm_id
        self.all_vm_ids = all_vm_ids
        self.sdn_zone_id = sdn_zone_id
        self.instance = instance
        self.pool_id = pool_id
        self.os_type = os_type

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

    def _is_windows(self) -> bool:
        """Check if this VM is running Windows based on os_type."""
        if self.os_type is None:
            return False
        # Windows os_types: w2k, w2k3, w2k8, win10, win11, win7, win8, wvista, wxp
        return self.os_type.startswith("w")

    def _build_batch_script(
        self,
        tmp_start: str,
        command: List[str],
        stdin: str | bytes | None,
        cwd: str | None,
        env: dict[str, str],
        user: str | None,
        timeout: int | None,
    ) -> str:
        """Build a batch script for Windows VMs."""
        lines = ["@echo off"]

        # Remove old output files
        lines.append(f'del /f /q "{tmp_start}script.stdout" 2>nul')
        lines.append(f'del /f /q "{tmp_start}script.stderr" 2>nul')
        lines.append(f'del /f /q "{tmp_start}script.returncode" 2>nul')

        # Set environment variables
        for key, value in env.items():
            # Escape special batch characters
            escaped_value = value.replace("%", "%%").replace("^", "^^")
            lines.append(f'set "{key}={escaped_value}"')

        # Change directory if specified
        if cwd is not None:
            lines.append(f'cd /d "{cwd}"')

        # Build the command - escape for batch
        escaped_args = []
        for arg in command:
            # Escape special characters for cmd.exe
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
        cmd_str = " ".join(escaped_args)

        # Execute command with output redirection
        # Note: stdin piping in batch is limited, skip for now
        lines.append(f'{cmd_str} > "{tmp_start}script.stdout" 2> "{tmp_start}script.stderr"')
        lines.append(f'echo %ERRORLEVEL% > "{tmp_start}script.returncode"')

        return "\r\n".join(lines)

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
        infra_commands: InfraCommands, config: ProxmoxSandboxEnvironmentConfig
    ) -> None:
        built_in_names = set()
        for vm_config in config.vms_config:
            if vm_config.vm_source_config.built_in is not None:
                built_in_names.add(vm_config.vm_source_config.built_in)
        for built_in_name in built_in_names:
            await infra_commands.built_in_vm.ensure_exists(built_in_name)

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
        # Pool creation only depends on infrastructure config (PROXMOX_CONFIG_FILE),
        # not on eval-specific config. Config may be None when the task delegates
        # per-sample config to sample_init.
        await cls.create_proxmox_instance_pools()

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

            target = ProxmoxTarget(
                host=instance.host, port=instance.port, node=instance.node
            )
            try:
                infra_commands = InfraCommands.get_instance(target)
            except LookupError:
                infra_commands = InfraCommands.build(
                    async_proxmox_api, instance.node, config.image_storage
                )
                InfraCommands.set_instance(target, infra_commands)

            # The pool guarantees one sample per instance at a time, so any
            # leftover provider-managed VNETs here are orphans from a previous
            # failed cleanup. User pre-existing VNETs are ignored by this check.
            await cls._ensure_instance_clean(infra_commands, instance.instance_id)

            task_name_start = re.sub("[^a-zA-Z0-9]", "x", task_name[:3].lower())

            proxmox_ids_start = await infra_commands.find_proxmox_ids_start(
                task_name_start
            )

            # Ensure built-in VM templates exist on this instance
            await ProxmoxSandboxEnvironment.ensure_vms(
                infra_commands=infra_commands, config=config
            )

            async with concurrency(f"proxmox-{instance.host}", 1):
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

            agent_commands = AgentCommands(
                async_proxmox=infra_commands.async_proxmox, node=instance.node
            )

            for idx, vm_config_and_id in enumerate(vm_configs_with_ids):
                vm_sandbox_environment = ProxmoxSandboxEnvironment(
                    infra_commands=infra_commands,
                    agent_commands=agent_commands,
                    ipam_mappings=ipam_mappings,
                    vm_id=vm_config_and_id[0],
                    all_vm_ids=vm_ids,
                    sdn_zone_id=sdn_zone_id,
                    instance=instance,
                    pool_id=pool_id,
                    os_type=vm_config_and_id[1].os_type,
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
                    await infra_commands.cleanup_no_id(skip_confirmation=True)  # type: ignore

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
        """Ensure instance has no leftover provider-managed ephemeral VNETs.

        Only VNETs in zones matching the provider's ephemeral-zone naming
        convention are considered leftovers. Pre-existing user VNETs
        (referenced via sdn_config=None) and the static `inspvm*` SDN are
        deliberately ignored — they are expected to persist across samples.

        Logs errors but does not raise - if the instance is dirty,
        the subsequent setup will fail and the error handler will deal with it.
        """
        try:
            vnets = await infra_commands.sdn_commands.read_all_vnets()
            leftover_vnets = [
                v
                for v in vnets
                if "zone" in v and re.match(ZONE_REGEX, v["zone"])
            ]

            if leftover_vnets:
                cls.logger.warning(
                    f"Instance {instance_id} has {len(leftover_vnets)} "
                    f"leftover provider-managed VNETs! "
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
            if any_vm_sandbox_environment is not None and not interrupted:
                await any_vm_sandbox_environment.infra_commands.delete_sdn_and_vms(
                    sdn_zone_id=any_vm_sandbox_environment.sdn_zone_id,
                    ipam_mappings=any_vm_sandbox_environment.all_ipam_mappings,
                    vm_ids=any_vm_sandbox_environment.all_vm_ids,
                )
                any_vm_sandbox_environment.infra_commands.deregister_resources(
                    vm_ids=any_vm_sandbox_environment.all_vm_ids,
                    sdn_zone_id=any_vm_sandbox_environment.sdn_zone_id,
                    ipam_mappings=any_vm_sandbox_environment.all_ipam_mappings,
                )
                cleanup_succeeded = True
                instance_id = instance.instance_id if instance else "unknown"
                cls.logger.info(
                    f"Successfully cleaned up VMs for instance {instance_id}"
                )
            elif interrupted:
                # Interrupted samples skip cleanup; task_cleanup will
                # sweep orphaned resources via InfraCommands tracking.
                cleanup_succeeded = True
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
                        f"pool_id={pool_id}\n"
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
        cls.logger.debug(f"task cleanup activated; {cleanup=}; {config=}")

        if cleanup:
            # Sweep orphaned resources across all Proxmox instances that
            # were used during this task run.
            for target, infra_commands in InfraCommands._instances.items():
                try:
                    cls.logger.debug(f"task_cleanup for {target}")
                    await infra_commands.task_cleanup()
                except Exception as e:
                    cls.logger.warning(
                        f"task_cleanup failed for {target}: {e}"
                    )
        else:
            print(
                "\nCleanup all sandbox releases with: "
                "[blue]inspect sandbox cleanup proxmox[/blue]\n"
            )

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
                infra_commands = InfraCommands.build(
                    async_proxmox_api,
                    instance.node,
                    ProxmoxSandboxEnvironmentConfig().image_storage,
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
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
        concurrency: bool = False,
    ) -> ExecResult[str]:
        if self.vm_id is None:
            raise ValueError("VM ID is not set")

        is_windows = self._is_windows()

        # Hardcoded path because the QEMU guest agent doesn't expand
        # environment variables like %TEMP%. C:\Windows\Temp always exists.
        if is_windows:
            tmp_start = f"C:\\Windows\\Temp\\{__name__}{time.time_ns()}_"
            self.logger.info(
                f"[WINDOWS_EXEC] Using Windows paths for VM {self.vm_id}, "
                f"os_type={self.os_type}, tmp_start={tmp_start}"
            )
        else:
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

        if is_windows:
            script = self._build_batch_script(
                tmp_start=tmp_start,
                command=cmd,
                stdin=input,
                cwd=cwd,
                env=env or {},
                user=user,
                timeout=timeout,
            )
            script_path = f"{tmp_start}script.bat"
            await self._write_file_only(script_path, script)
            exec_post_response = await self.agent_commands.exec_command(
                vm_id=self.vm_id, command=["cmd.exe", "/c", script_path]
            )
        else:
            script = self._build_shell_script(
                tmp_start=tmp_start,
                command=cmd,
                stdin=input,
                cwd=cwd,
                env=env or {},
                user=user,
                timeout=timeout,
            )
            await self._write_file_only(f"{tmp_start}script.sh", script)
            exec_post_response = await self.agent_commands.exec_command(
                vm_id=self.vm_id, command=["sh", f"{tmp_start}script.sh"]
            )

        exec_response_pid = exec_post_response["pid"]

        assert isinstance(exec_response_pid, int)
        self.logger.debug(
            f"VM {self.vm_id} exec pid={exec_response_pid}: {cmd[:100]}"
        )

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
        if is_windows:
            await self.agent_commands.exec_command(
                vm_id=self.vm_id,
                command=["cmd.exe", "/c", f'del /f /q "{tmp_start}*" 2>nul'],
            )
        else:
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

    # Platform-specific "file not found" messages from the QEMU guest agent.
    _FILE_NOT_FOUND_ERRORS = [
        "No such file or directory",  # Linux
        "cannot find the path",  # Windows
    ]

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
                ex_str = str(ex)
                matched = next(
                    (err for err in self._FILE_NOT_FOUND_ERRORS if err in ex_str), None
                )
                if matched:
                    raise FileNotFoundError(errno.ENOENT, matched, file)
                elif "Is a directory" in ex_str:
                    raise IsADirectoryError(errno.EISDIR, "Is a directory", file)
                else:
                    raise ex
            else:
                raise ex

    # Size at which the ISO hot-plug path takes over from chunked QGA writes.
    # Below this, QGA single-shot or a handful of chunks is faster than the
    # fixed ISO-build/upload/attach/mount overhead. Linux only — Windows still
    # uses chunking until that path is implemented.
    ISO_WRITE_THRESHOLD_BYTES = 1 * 1024 * 1024

    @override
    async def write_file(self, file: str, contents: str | bytes) -> None:
        # Writes contents to file, handling large files by splitting them into chunks
        # and recombining using cat (Linux) or copy /b (Windows).

        CHUNK_SIZE = (
            40 * 1024
        )  # 40KB chunks to be safe, to take base64 encoding into account
        # note this 40KB limit was based on the Proxmox <=8.3 limit of
        # 60Kb, but this was increased in Proxmox 8.4, so could
        # potentially be increased here. Would need to check the
        # version number to ensure backward compatibility.

        is_windows = self._is_windows()

        # Create parent directory
        if is_windows:
            parent_dir = str(PureWindowsPath(file).parent)
            await self.exec(
                cmd=["cmd.exe", "/c", f'if not exist "{parent_dir}" mkdir "{parent_dir}"']
            )
        else:
            await self.exec(
                cmd=["mkdir", "-p", "--", str(Path(file).parent.as_posix())]
            )

        # If content is small enough, write directly
        if len(contents) <= CHUNK_SIZE:
            await self._write_file_only(file, contents)
            return

        # Linux large-file fast path: hot-plug ISO instead of chunked QGA
        if not is_windows and len(contents) >= self.ISO_WRITE_THRESHOLD_BYTES:
            try:
                content_bytes = (
                    contents if isinstance(contents, bytes) else contents.encode("utf-8")
                )
                iso_writer = IsoWriter(
                    async_proxmox=self.infra_commands.async_proxmox,
                    agent_commands=self.agent_commands,
                    storage_commands=self.qemu_commands.storage_commands,
                    node=self.infra_commands.node,
                )
                await iso_writer.write_file(self.vm_id, file, content_bytes)
                return
            except Exception as ex:
                self.logger.warning(
                    f"iso_write fast path failed for vm {self.vm_id} target {file}; "
                    f"falling back to chunked QGA: {ex}"
                )

        # For large contents, split into chunks
        chunks = [
            contents[i : i + CHUNK_SIZE] for i in range(0, len(contents), CHUNK_SIZE)
        ]

        # Calculate padding width based on number of chunks
        padding_width = len(str(len(chunks) - 1))

        # Use appropriate temp directory
        if is_windows:
            tmp_start = f"C:\\Windows\\Temp\\{__name__}_write_file_{time.time_ns()}_"
            temp_dir = f"{tmp_start}split_{PureWindowsPath(file).name}"
        else:
            tmp_start = f"/tmp/{__name__}_write_file_{time.time_ns()}_"
            temp_dir = f"{tmp_start}split_{Path(file).name}"

        try:
            if is_windows:
                await self.exec(
                    cmd=["cmd.exe", "/c", f'if not exist "{temp_dir}" mkdir "{temp_dir}"']
                )
            else:
                await self.exec(cmd=["mkdir", "-p", "--", temp_dir])

            # Write chunks to temp files with zero-padded numbers
            for i, chunk in enumerate(chunks):
                if is_windows:
                    chunk_file = f"{temp_dir}\\chunk_{i:0{padding_width}d}"
                else:
                    chunk_file = f"{temp_dir}/chunk_{i:0{padding_width}d}"
                await self._write_file_only(chunk_file, chunk)

            if is_windows:
                # Batch script to combine chunks using copy /b
                combine_script = f'@echo off\r\ndel /f /q "{file}" 2>nul\r\n'
                for i in range(len(chunks)):
                    chunk_file = f"{temp_dir}\\chunk_{i:0{padding_width}d}"
                    if i == 0:
                        combine_script += f'copy /b "{chunk_file}" "{file}"\r\n'
                    else:
                        combine_script += f'copy /b "{file}"+"{chunk_file}" "{file}"\r\n'
                combine_script_path = f"{temp_dir}\\combine.bat"
                await self._write_file_only(combine_script_path, combine_script)
                await self.exec(cmd=["cmd.exe", "/c", combine_script_path])
            else:
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
            if is_windows:
                await self.exec(cmd=["cmd.exe", "/c", f'rmdir /s /q "{temp_dir}"'])
            else:
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
                ex_str = str(ex)
                matched = next(
                    (err for err in self._FILE_NOT_FOUND_ERRORS if err in ex_str), None
                )
                if matched:
                    raise FileNotFoundError(errno.ENOENT, matched, file)
                elif "Is a directory" in ex_str:
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
    async def connection(self, *, user: str | None = None) -> SandboxConnection:
        """
        Returns a connection to the sandbox.

        Raises:
           NotImplementedError: For sandboxes that don't provide connections
           ConnectionError: If sandbox is not currently running.
        """
        if self.vm_id is None:
            raise ConnectionError("Sandbox is not running")
        url = await self.qemu_commands.connection_url(self.vm_id)
        return SandboxConnection(type="proxmox", command=f"open '{url}'")

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
        await self.qemu_commands.await_vm(vm_id=self.vm_id, is_sandbox=True)
