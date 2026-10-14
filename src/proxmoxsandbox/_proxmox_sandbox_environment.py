import asyncio
import errno
import re
import time
from logging import INFO, NOTSET, getLogger
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Tuple, Type, Union

import httpx
from inspect_ai.util import (
    ExecResult,
    OutputLimitExceededError,
    SandboxConnection,
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
    SandboxEnvironmentLimits,
    concurrency,
    sandboxenv,
)
from pydantic import BaseModel
from typing_extensions import override

from proxmoxsandbox._impl.agent_commands import (
    AgentCommands,
)
from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.deadline import (
    CLEANUP_WAIT_SECONDS,
    within_budget,
)
from proxmoxsandbox._impl.guest_exec import (
    _PVE_FILE_READ_MAX,
    _PVE_FILE_READ_MAX_STR,
    _WRITE_CHUNK_SIZE,
    GuestExecutor,
    _split_chunks,
)
from proxmoxsandbox._impl.infra_commands import InfraCommands, ProxmoxTarget
from proxmoxsandbox._impl.iso_write import IsoWriter
from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox._impl.qga_responses import GuestAgentTamperError
from proxmoxsandbox._impl.sdn_commands import IpamMapping, is_ephemeral_zone
from proxmoxsandbox._impl.task_wrapper import TaskWrapper
from proxmoxsandbox._proxmox_pool import ProxmoxPoolABC, QueueBasedProxmoxPool
from proxmoxsandbox.schema import (
    OsType,
    ProxmoxInstanceConfig,
    ProxmoxSandboxEnvironmentConfig,
    VmConfig,
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
    name: str
    all_vms: dict[int, VmConfig]
    sdn_zone_id: str | None
    # Multi-instance pool fields
    instance: ProxmoxInstanceConfig | None
    pool_id: str | None
    # OS type for Windows support
    os_type: OsType | None
    # Set to True after the ISO write_file fast path fails on this VM.
    # Subsequent large writes go straight to chunked QGA instead of paying
    # the ~3 s of ISO build+upload+attach before falling back.
    _iso_fast_path_disabled: bool
    # Serialises concurrent ISO writes to this VM: they share the single
    # cold-added sata5 slot and would clobber each other's media-change.
    _iso_write_lock: asyncio.Lock

    def __init__(
        self,
        *,
        infra_commands: InfraCommands,
        agent_commands: AgentCommands,
        ipam_mappings: Tuple[IpamMapping, ...],
        vm_id: int,
        name: str,
        all_vms: dict[int, VmConfig],
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
        self.name = name
        self.all_vms = all_vms
        self.sdn_zone_id = sdn_zone_id
        self.instance = instance
        self.pool_id = pool_id
        self.os_type = os_type
        self._iso_fast_path_disabled = False
        self._iso_write_lock = asyncio.Lock()

    def _is_windows(self) -> bool:
        """Check whether this VM uses Windows paths and command rendering."""
        return self.os_type is not None and self.os_type.startswith("w")

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
    def _init_package_log_level(cls) -> None:
        """Set the package logger to INFO, or the root's level if lower.

        Must run after Inspect's `init_logger`; extensions are imported
        earlier, during `platform_init()`, when the root is still at its
        default.
        """
        if getLogger("proxmoxsandbox").level == NOTSET:
            getLogger("proxmoxsandbox").setLevel(
                min(INFO, getLogger().getEffectiveLevel())
            )

    @classmethod
    @override
    async def task_init(
        cls, task_name: str, config: SandboxEnvironmentConfigType | None
    ) -> None:
        cls._init_package_log_level()
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

        # The frozen config's host/port/node are env-var defaults, not the
        # acquired instance, so they misattribute pooled samples. Log the real
        # instance so a sample can be attributed to a Proxmox server.
        cls.logger.info(
            f"Acquired instance {instance.instance_id} from pool '{pool_id}': "
            f"host={instance.host} port={instance.port} node={instance.node}"
        )

        try:
            async_proxmox_api = AsyncProxmoxAPI.from_instance_config(instance)

            target = ProxmoxTarget(
                host=instance.host, port=instance.port, node=instance.node
            )
            try:
                infra_commands = InfraCommands.get_instance(target)
            except LookupError:
                infra_commands = InfraCommands.build(
                    async_proxmox_api, instance.node, instance.image_storage
                )
                InfraCommands.set_instance(target, infra_commands)

            # The pool guarantees one sample per instance at a time, so any
            # leftover provider-managed VNETs or inspect-tagged VMs here are
            # orphans from a previous sample whose init failed or whose cleanup
            # didn't run. User pre-existing VNETs are ignored by this check.
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
                ) = await infra_commands.create_sdn_and_vms(proxmox_ids_start, config)

            sandboxes: Dict[str, SandboxEnvironment] = {}

            agent_commands = AgentCommands(
                async_proxmox=infra_commands.async_proxmox, node=instance.node
            )

            all_vms = dict(vm_configs_with_ids)
            for vm_id, vm_config in vm_configs_with_ids:
                vm_sandbox_environment = ProxmoxSandboxEnvironment(
                    infra_commands=infra_commands,
                    agent_commands=agent_commands,
                    ipam_mappings=ipam_mappings,
                    vm_id=vm_id,
                    name=vm_config.name,
                    all_vms=all_vms,
                    sdn_zone_id=sdn_zone_id,
                    instance=instance,
                    pool_id=pool_id,
                    os_type=vm_config.os_type,
                )
                # The first sandbox VM is also Inspect's "default". Config
                # validation guarantees there is one and that no later VM took
                # the name.
                if vm_config.is_sandbox:
                    sandboxes[vm_config.name] = vm_sandbox_environment
                    if "default" not in sandboxes:
                        sandboxes["default"] = vm_sandbox_environment

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
            # No teardown here: Inspect only tells us whether the user wants
            # cleanup via task_cleanup(cleanup=...), so whatever got created
            # stays up (still tracked) for task_cleanup to sweep, or for the
            # user to poke at under --no-sandbox-cleanup. The next sample to
            # acquire this instance pre-cleans it in _ensure_instance_clean.
            cls.logger.info(
                f"sample_init failed on instance {instance.instance_id}: {e}. "
                f"Leaving any partial infrastructure for task_cleanup; "
                f"releasing instance back to pool '{pool_id}'"
            )
            await cls.proxmox_pool.release_instance(pool_id, instance)
            raise

    @classmethod
    async def _ensure_instance_clean(
        cls, infra_commands: InfraCommands, instance_id: str
    ) -> None:
        """Ensure instance has no leftover provider-managed VNETs or VMs.

        Only VNETs in zones matching the provider's ephemeral-zone naming
        convention, and non-template VMs tagged `inspect`, are considered
        leftovers. Pre-existing user VNETs (referenced via sdn_config=None)
        and the static `inspvm*` SDN are deliberately ignored — they are
        expected to persist across samples.

        Logs errors but does not raise - if the instance is dirty,
        the subsequent setup will fail and the error handler will deal with it.
        """
        try:
            vnets = await infra_commands.sdn_commands.read_all_vnets()
            leftover_vnets = [
                v for v in vnets if "zone" in v and is_ephemeral_zone(v["zone"])
            ]
            vms = await infra_commands.qemu_commands.list_vms()
            leftover_vms = [
                v for v in vms if QemuCommands.vm_is_inspect(v, template=False)
            ]

            if leftover_vnets or leftover_vms:
                cls.logger.warning(
                    f"Instance {instance_id} has {len(leftover_vnets)} "
                    f"leftover provider-managed VNETs and {len(leftover_vms)} "
                    f"leftover VMs! Cleaning up before proceeding..."
                )
                await infra_commands.cleanup_no_id(skip_confirmation=True)
                cls.logger.info(f"Pre-cleaned instance {instance_id}")
        except Exception as e:
            cls.logger.error(
                f"Failed to check/clean instance {instance_id}: "
                f"{type(e).__name__}: {e}. "
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
                    vms=any_vm_sandbox_environment.all_vms,
                )
                any_vm_sandbox_environment.infra_commands.deregister_resources(
                    vm_ids=any_vm_sandbox_environment.all_vms.keys(),
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
                        f"instance_id={instance.instance_id}\n"
                        f"host={instance.host}\n"
                        f"port={instance.port}\n"
                        f"node={instance.node}\n"
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
        config_type = type(config).__name__ if config is not None else None
        if isinstance(config, ProxmoxSandboxEnvironmentConfig):
            cls.logger.debug(
                f"task cleanup activated; {cleanup=}; "
                f"config_type={config_type}; "
                f"instance_pool_id={config.instance_pool_id}"
            )
        else:
            cls.logger.debug(
                f"task cleanup activated; {cleanup=}; config_type={config_type}"
            )

        if cleanup:
            # Sweep orphaned resources across all Proxmox instances that
            # were used during this task run.
            for target, infra_commands in InfraCommands._instances.items():
                try:
                    cls.logger.debug(f"task_cleanup for {target}")
                    await infra_commands.task_cleanup()
                except Exception as e:
                    cls.logger.warning(f"task_cleanup failed for {target}: {e}")
        else:
            print(
                "\nCleanup all sandbox releases with: "
                "[blue]inspect sandbox cleanup proxmox[/blue]\n"
            )

    @classmethod
    @override
    async def cli_cleanup(cls, id: str | None) -> None:
        cls._init_package_log_level()
        if id is None:
            await cls.create_proxmox_instance_pools()
            for instance in cls.proxmox_pool.all_instances():
                async_proxmox_api = AsyncProxmoxAPI.from_instance_config(instance)
                infra_commands = InfraCommands.build(
                    async_proxmox_api,
                    instance.node,
                    instance.image_storage,
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
        executor = GuestExecutor(
            self.agent_commands,
            self.vm_id,
            self.os_type,
        )
        return await executor.exec(cmd, input, cwd, env, user, timeout)

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
        except httpx.HTTPStatusError as ex:
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

    # Above this size, write_file uses the ISO fast path; below it, chunked
    # QGA. Not a true crossover: benchmarking (live ubuntu24.04) found ISO
    # wins down to ~8 KiB — it collapses to one in-guest exec, beating even
    # QGA's separate mkdir+write round-trips. Set deliberately above the
    # crossover so trivially-small writes stay on the simpler QGA path (no
    # storage upload, smaller failure surface) and keep that fallback path
    # exercised on the normal route. Linux only — Windows always chunks.
    ISO_WRITE_THRESHOLD_BYTES = 128 * 1024

    async def _try_iso_write(self, file: str, contents: str | bytes) -> bool:
        """Attempt ISO transfer; return False on ordinary failures or when skipped.

        Cancellation and malformed replies propagate and disable future ISO writes.
        """
        if (
            self._is_windows()
            or len(contents) < self.ISO_WRITE_THRESHOLD_BYTES
            or self._iso_fast_path_disabled
        ):
            return False
        # Hold the per-VM lock for the whole attach/copy/detach: it's a single
        # shared sata5 slot, so concurrent writes must serialise.
        async with self._iso_write_lock:
            # Re-check under the lock: a write we queued behind may have already
            # tripped the failure and disabled the fast path, in which case fall
            # straight through to chunked QGA.
            if self._iso_fast_path_disabled:
                return False
            try:
                content_bytes = (
                    contents
                    if isinstance(contents, bytes)
                    else contents.encode("utf-8")
                )
                iso_writer = IsoWriter(
                    async_proxmox=self.infra_commands.async_proxmox,
                    agent_commands=self.agent_commands,
                    storage_commands=self.qemu_commands.storage_commands,
                    node=self.infra_commands.node,
                )
                await iso_writer.write_file(self.vm_id, file, content_bytes)
                return True
            except (asyncio.CancelledError, GuestAgentTamperError):
                self._iso_fast_path_disabled = True
                raise
            except Exception as ex:
                # Persistent failure (iso_write already retried the first-call
                # "Can't open blockdev" race internally via detach + re-attach).
                # Usual causes: the VM template repurposed the sata5 slot; full
                # `local` storage so the ISO upload fails; or the guest kernel
                # refusing optical opens (dmesg shows AHCI / "Can't open
                # blockdev"). Disable for this VM's lifetime so we don't re-pay
                # ~3 s of ISO build+upload+attach on every subsequent large
                # write; fall through to chunked QGA.
                self._iso_fast_path_disabled = True
                self.logger.warning(
                    "iso_write fast path disabled for VM %s (writing %s); using "
                    "the chunked-QGA fallback for the rest of this VM's life. "
                    "Underlying error: %s",
                    self.vm_id,
                    file,
                    ex,
                )
                return False

    @override
    async def write_file(self, file: str, contents: str | bytes) -> None:
        # Writes contents to file, handling large files by splitting them into chunks
        # and recombining using cat (Linux) or copy /b (Windows).

        is_windows = self._is_windows()

        # Linux large-file fast path: hot-plug ISO instead of chunked QGA. Skips
        # the parent-mkdir round-trip below — the in-guest ISO script does its own
        # `mkdir -p -- "$(dirname target)"`, so doing it here too would waste one
        # whole env.exec() (~2s of QGA round-trips).
        if await self._try_iso_write(file, contents):
            return

        content_bytes = (
            contents if isinstance(contents, bytes) else contents.encode("utf-8")
        )

        # Create parent directory
        if is_windows:
            parent_dir = str(PureWindowsPath(file).parent)
            await self.exec(
                cmd=[
                    "cmd.exe",
                    "/c",
                    f'if not exist "{parent_dir}" mkdir "{parent_dir}"',
                ]
            )
        else:
            await self.exec(
                cmd=["mkdir", "-p", "--", str(Path(file).parent.as_posix())]
            )

        # If content is small enough, write directly
        if len(content_bytes) <= _WRITE_CHUNK_SIZE:
            await self._write_file_only(file, content_bytes)
            return

        # For large contents, split into chunks
        chunks, padding_width = _split_chunks(content_bytes)

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
                    cmd=[
                        "cmd.exe",
                        "/c",
                        f'if not exist "{temp_dir}" mkdir "{temp_dir}"',
                    ]
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
                        combine_script += (
                            f'copy /b "{file}"+"{chunk_file}" "{file}"\r\n'
                        )
                combine_script_path = f"{temp_dir}\\combine.bat"
                await self._write_file_only(combine_script_path, combine_script)
                await self.exec(cmd=["cmd.exe", "/c", combine_script_path])
            else:
                seq_fmt = f"%0{padding_width}.0f"
                combine_script = (
                    f"rm -f {file}\n"
                    f'for i in $(seq -f "{seq_fmt}" 0 {len(chunks) - 1}); do\n'
                    f'  cat "{temp_dir}/chunk_$i" >> {file}\n'
                    f"done\n"
                )
                combine_script_path = f"{temp_dir}/combine.sh"
                await self._write_file_only(combine_script_path, combine_script)
                await self.exec(cmd=["sh", combine_script_path])

        finally:
            cleanup = (
                ["cmd.exe", "/c", f'rmdir /s /q "{temp_dir}"']
                if is_windows
                else ["rm", "-rf", temp_dir]
            )
            try:
                await within_budget(
                    self.exec(cmd=cleanup), CLEANUP_WAIT_SECONDS, independent=True
                )
            except GuestAgentTamperError:
                raise
            except Exception as ex:
                self.logger.warning(
                    "Temporary-file cleanup on VM %s failed: %s", self.vm_id, ex
                )

    @override
    async def read_file(self, file: str, text: bool = True) -> Union[str | bytes]:  # type: ignore
        """Read a file from the sandbox environment.

        File size is limited to 16 MiB - this is a limitation of proxmox.
        This is a deviation from the Inspect spec which states 100 MiB.
        """
        if self.vm_id is None:
            raise ValueError("VM ID is not set")
        # PVE caps agent file-read at 16 MiB; cap at the (test-overridable)
        # Inspect limit, never above that.
        cap = min(SandboxEnvironmentLimits.MAX_READ_FILE_SIZE, _PVE_FILE_READ_MAX)
        try:
            data_bytes, truncated = await self.agent_commands.read_file_capped(
                vm_id=self.vm_id,
                filepath=file,
                count=cap,
            )
        except httpx.HTTPStatusError as ex:
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
        if truncated:
            # File exceeds the cap; report the active limit (16 MiB or a test override).
            limit_str = (
                SandboxEnvironmentLimits.MAX_READ_FILE_SIZE_STR
                if SandboxEnvironmentLimits.MAX_READ_FILE_SIZE <= _PVE_FILE_READ_MAX
                else _PVE_FILE_READ_MAX_STR
            )
            raise OutputLimitExceededError(limit_str, None)
        if text:
            return data_bytes.decode("utf-8")
        else:
            return data_bytes

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
        await self.qemu_commands.await_vm(
            vm_id=self.vm_id,
            requires_guest_agent=True,
            name=self.name,
        )
