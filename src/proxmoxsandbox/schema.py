"""Data models and schemas for the Proxmox sandbox configuration."""

import json
import os
import re
from os import getenv
from pathlib import Path
from typing import Annotated, Dict, Literal, Optional, Tuple, TypeAlias, Union

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic.networks import IPvAnyAddress, IPvAnyNetwork
from pydantic_extra_types.mac_address import MacAddress


class DhcpRange(BaseModel, frozen=True):
    """
    Represents a DHCP range with start and end IP addresses.

    Attributes:
        start: The starting IP address of the DHCP range
        end: The ending IP address of the DHCP range
    """

    start: IPvAnyAddress
    end: IPvAnyAddress

    def _to_proxmox_format(self) -> str:
        return f"start-address={self.start},end-address={self.end}"


class SubnetConfig(BaseModel, frozen=True):
    """
    Configuration for a subnet within a virtual network.

    Attributes:
        cidr: The subnet in CIDR notation
        gateway: The gateway IP address for the subnet
        snat: Whether source NAT is enabled for this subnet
        dhcp_ranges: DHCP ranges configured for this subnet
    """

    cidr: IPvAnyNetwork
    gateway: IPvAnyAddress
    snat: bool
    dhcp_ranges: Tuple[DhcpRange, ...]


class VnetConfig(BaseModel, frozen=True):
    """
    Configuration for a virtual network.

    Attributes:
        alias: A human-readable alias for the virtual network.
            The alias is also used in this configuration to link each VM in the Vnet.
        subnets: Subnet configurations for this virtual network.
    """

    alias: Optional[
        # original regex (?^i:[\(\)-_.\w\d\s]{0,256}) but that's not especially
        # Python-compatible
        Annotated[str, Field(pattern=r"[()-_.[a-z][A-Z][0-9]\s]{0,256}")]
    ] = None
    subnets: Tuple[SubnetConfig, ...] = ()


class SdnConfig(BaseModel, frozen=True):
    """
    Software-defined networking configuration.

    Attributes:
        vnet_configs: Configurations for VNets.
        use_pve_ipam_dnsnmasq: Whether to use Proxmox VE's built-in IPAM and DNSmasq
            Set to False if you are using e.g. your own pfsense instance for IPAM
            (recommended)
    """

    vnet_configs: Tuple[VnetConfig, ...]
    use_pve_ipam_dnsnmasq: bool = True


SdnConfigType: TypeAlias = Union[SdnConfig, Literal["auto"], None]


class VmSourceConfig(BaseModel, frozen=True):
    """
    Configuration for the source of a virtual machine.

    Exactly one source type must be specified.

    Attributes:
        existing_vm_template_tag: Clone VM from existing Proxmox template with this tag
        ova: Create VM from this OVA file in the local (not Proxmox) filesystem.
        built_in: Use this provider's built-in VM template (currently "ubuntu24.04"
            is supported)
    """

    existing_vm_template_tag: str | None = None
    ova: Path | None = None
    # Ubuntu 24.04 is supported because an OVA is publicly available from a reliable
    # source.
    # From Proxmox 9.0 onwards, qcow2 and raw are also supported, allowing Debian 13,
    # Kali, and others.
    built_in: Literal["ubuntu24.04", "debian13", "kali2025.4"] | None = None

    @model_validator(mode="after")
    def _validate_single_source(self) -> "VmSourceConfig":
        set_sources = [
            name
            for name, value in {
                "existing_vm_template_tag": self.existing_vm_template_tag,
                "ova": self.ova,
                "built_in": self.built_in,
            }.items()
            if value is not None
        ]

        if len(set_sources) != 1:
            raise ValueError(
                "Exactly one source must be set. "
                + f"Found {len(set_sources)}: {', '.join(set_sources) or 'none'}"
            )

        return self


class VmNicConfig(BaseModel, frozen=True):
    """
    Configuration for a virtual machine network interface.

    Attributes:
        vnet_alias: The alias of the VNet to connect to. This can be either:
            - An alias defined in the sdn_config
            - An existing VNET alias in Proxmox (when sdn_config is None)
        mac: The MAC address for the network interface (optional)
        ipv4: The static IPv4 address for the network interface (optional).
            If specified, a DHCP static mapping (host reservation) will be created.
            Requires a MAC address to be specified as well.
            Please read the notes in README.md for Proxmox server patching requirements
    """

    vnet_alias: str
    mac: Optional[MacAddress] = None
    ipv4: Optional[IPvAnyAddress] = None

    @model_validator(mode="after")
    def _validate_ipv4_requires_mac(self) -> "VmNicConfig":
        if self.ipv4 is not None and self.mac is None:
            raise ValueError(
                "ipv4 address requires a mac address to be specified for "
                + "DHCP static mapping"
            )
        return self


# Proxmox QEMU OS types. See https://pve.proxmox.com/wiki/Manual:_qm.conf
OsType: TypeAlias = Literal[
    "l24",  # Linux 2.4 kernel
    "l26",  # Linux 2.6+  kernel
    "other",
    "solaris",
    "w2k",  # Windows 2000
    "w2k3",  # Windows 2003
    "w2k8",  # Windows 2008
    "win10",  # Windows 10/2016/2019
    "win11",  # Windows 11/2022/2025
    "win7",  # Windows 7/2008r2
    "win8",  # Windows 8/2012
    "wvista",  # Windows Vista/2008
    "wxp",  # Windows XP/2003
]


class ReadinessRetry(BaseModel, frozen=True, extra="forbid", allow_inf_nan=False):
    """Retry timing in seconds; timeout includes attempts, sleeps, and repairs."""

    interval: float = Field(default=2, gt=0)
    backoff: float = Field(default=1.5, ge=1)
    max_interval: float = Field(default=30, gt=0)
    attempt_timeout: float = Field(default=60, gt=0)
    timeout: float = Field(default=600, gt=0)

    @model_validator(mode="after")
    def _validate_interval(self) -> "ReadinessRetry":
        if self.max_interval < self.interval:
            raise ValueError("max_interval must be >= interval")
        return self


class ReadinessCommand(BaseModel, frozen=True, extra="forbid"):
    """Guest command and success predicate (exit code AND optional stdout regex).

    Arguments are not implicitly passed to a shell. Use sh -c / PowerShell
    explicitly for scripts. Commands must be safe to repeat. No command output
    is included in the startup display, as it may contain credentials.
    """

    argv: Tuple[str, ...] = Field(min_length=1)
    timeout: int = Field(default=30, gt=0)
    expected_exit_code: int = 0
    stdout_regex: str | None = None

    @model_validator(mode="after")
    def _validate_command(self) -> "ReadinessCommand":
        if not self.argv[0] or any("\x00" in arg for arg in self.argv):
            raise ValueError("argv needs an executable and must not contain NUL")
        if self.stdout_regex is not None:
            try:
                re.compile(self.stdout_regex)
            except re.error as exc:
                raise ValueError("Invalid stdout_regex") from exc
        return self


class ReadinessRepair(BaseModel, frozen=True, extra="forbid", allow_inf_nan=False):
    """Run an ordered guest-command sequence after a check remains unready.

    The sequence stops at the first unsuccessful command. Attempts are bounded
    and never establish readiness themselves. Timings are in seconds, measured
    from the check's first eligibility and then from each repair's completion.
    """

    commands: Tuple[ReadinessCommand, ...] = Field(min_length=1)
    after: float = Field(default=300, ge=0)
    interval: float = Field(default=300, gt=0)
    max_attempts: int = Field(default=1, gt=0)
    timeout: float = Field(default=120, gt=0)


class ReadinessCheck(BaseModel, frozen=True, extra="forbid"):
    """Named startup check with independent retry and optional repair policy.

    A running check is always included. Sandboxes also always include an agent
    ping. Explicit checks of those kinds override their default policies.
    """

    name: str = Field(min_length=1, pattern=r"^[^\x00-\x1f\x7f]+$")
    kind: Literal["running", "qemu_agent", "command"] = "command"
    command: ReadinessCommand | None = None
    retry: ReadinessRetry = Field(default_factory=ReadinessRetry)
    repair: ReadinessRepair | None = None

    @model_validator(mode="after")
    def _validate_check(self) -> "ReadinessCheck":
        if (self.kind == "command") != (self.command is not None):
            raise ValueError("Only command checks must specify command")
        if self.repair is not None and self.repair.after >= self.retry.timeout:
            raise ValueError("repair.after must be less than retry.timeout")
        return self


class VmConfig(BaseModel, frozen=True):
    """
    Configuration for a virtual machine.

    Attributes:
        vm_source_config: The source configuration for the VM
        name: The name of the VM (optional). Must be a valid DNS name.
        ram_mb: RAM allocation in megabytes (default: 2048)
        vcpus: Number of virtual CPUs (default: 2)
        nics: Network interface configurations (optional)
        is_sandbox: if True, the VM will show up as a sandbox.
            It must have the qemu-guest-agent installed
        uefi_boot: if True, the VM will boot in UEFI mode. In theory, this is already
            specified by OVA, but Proxmox doesn't seem to respect it.
        disk_controller: The disk controller type. If unset, defaults to "scsi".
            For an OVA this selects the controller the imported disks are attached
            to. For an existing_vm_template_tag or built_in source it cannot be
            changed, so it is instead verified against the source VM and raises if
            they disagree.
        nic_controller: The NIC controller type. If unset, defaults to "virtio".
            This is applied to all virtual network interfaces.
        firewall: if True, enables the Proxmox firewall on all network interfaces.
            This is required for proper VM isolation. Defaults to False.
        os_type: The OS type. If unset, defaults to "l26". Only for OVA. See
            https://pve.proxmox.com/wiki/Manual:_qm.conf for more details
        cpu: The qemu CPU model (e.g. "host", "qemu64", "x86-64-v2"). If unset,
            defaults to "host". Older guest kernels (notably FreeBSD/pfSense) can
            panic on nested virtualization with "host"; use "qemu64" for those.
        await_before_next_vm: if True, wait for all of this VM's readiness checks
            before creating the next VM in
            vms_config. Defaults to False, so VMs boot concurrently. Set this on a VM
            that later ones depend on at boot time, e.g. a router or DHCP server.
            This legacy option is only valid when vms_config is a tuple.
        depends_on: VM identifiers that must be ready before this VM starts. Identifiers
            are keys in a dictionary vms_config. This option is only valid when
            vms_config is a dictionary.
        readiness_checks: Named startup checks. Proxmox running is always required;
            sandbox VMs additionally require a QEMU agent ping. Commands and repairs
            require the guest agent even for non-sandbox VMs. Successful checks are
            latched until startup completes, but any repair invalidates them all.

    Note on nics configuration:
    - If set, the VM will be connected to these VNets (one interface per VNet)
    - If set as the empty tuple (), the VM will not have any NICs
    - If left as the default None:
        If the vm_source_config is existing_vm_template_tag,
            the NICs will be left as configured in the template.
        If the vm_source_config is ova or built_in, it will be connected to the first
            VNet.
    """

    vm_source_config: VmSourceConfig
    name: Optional[str] = None
    ram_mb: Optional[int] = 2048
    vcpus: Optional[int] = 2
    nics: Optional[Tuple[VmNicConfig, ...]] = None
    is_sandbox: bool = True
    uefi_boot: bool = False
    disk_controller: Optional[Literal["scsi", "ide"]] = None
    nic_controller: Optional[Literal["virtio", "e1000"]] = None
    firewall: bool = False
    os_type: Optional[OsType] = "l26"
    cpu: Optional[str] = None
    await_before_next_vm: bool = False
    depends_on: Tuple[str, ...] = ()
    readiness_checks: Tuple[ReadinessCheck, ...] = ()

    @model_validator(mode="after")
    def _validate_readiness_checks(self) -> "VmConfig":
        checks = self.effective_readiness_checks()
        names = [check.name for check in checks]
        if len(names) != len(set(names)):
            raise ValueError(
                "Readiness check names must be unique (including defaults)"
            )
        for kind in ("running", "qemu_agent"):
            if sum(check.kind == kind for check in checks) > 1:
                raise ValueError(f"Only one {kind} readiness check is allowed")
        return self

    def effective_readiness_checks(self) -> Tuple[ReadinessCheck, ...]:
        """Return configured checks plus any missing mandatory built-in checks."""
        checks = list(self.readiness_checks)
        if not any(check.kind == "running" for check in checks):
            checks.insert(
                0,
                ReadinessCheck(
                    name="proxmox-running",
                    kind="running",
                    retry=ReadinessRetry(timeout=1200),
                ),
            )
        if self.is_sandbox and not any(check.kind == "qemu_agent" for check in checks):
            checks.insert(
                1,
                ReadinessCheck(
                    name="qemu-agent",
                    kind="qemu_agent",
                    retry=ReadinessRetry(timeout=300),
                ),
            )
        return tuple(checks)

    @property
    def requires_guest_agent(self) -> bool:
        """Whether sandbox access or a readiness check/repair needs QGA enabled."""
        return self.is_sandbox or any(
            check.kind in ("command", "qemu_agent") or check.repair is not None
            for check in self.readiness_checks
        )


VmConfigs: TypeAlias = Union[Tuple[VmConfig, ...], Dict[str, VmConfig]]


class HttpHeader(BaseModel):
    """
    A single HTTP header.

    Attributes:
        name: The header name, e.g. "Authorization"
        value: The header value. Modelled as a secret because header values
            often carry credentials (bearer tokens, API keys).
    """

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    name: str
    value: SecretStr


class ProxmoxInstanceConfig(BaseModel):
    """
    Configuration for a single Proxmox instance.

    Attributes:
        instance_id: Unique identifier for this instance
        pool_id: Image/AMI identifier - instances with the same pool_id share a queue
            Examples: AMI ID, S3 path, or "default" for blank instances
        host: The hostname or IP address of the Proxmox server
        port: The port number for the Proxmox API, usually 8006
        user: The username for Proxmox authentication
        user_realm: The authentication realm for the Proxmox user
        password: The password for Proxmox authentication
        node: The name of the Proxmox node
        verify_tls: Whether to verify the Proxmox server's TLS certificate
        image_storage: The Proxmox storage pool for VM disk images (e.g.
            "local-lvm"). Defaults to the PROXMOX_IMAGE_STORAGE environment variable,
            or "local-lvm" if not set.
        extra_headers: Additional HTTP headers to send with every request to
            this instance (e.g. credentials for a proxy or gateway in front of
            the Proxmox API, an API key, or tracing headers). The Proxmox
            authentication headers (Cookie, CSRFPreventionToken) cannot be
            overridden.
    """

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    instance_id: str
    pool_id: str
    host: str
    port: int
    user: str
    user_realm: str
    password: SecretStr
    node: str
    verify_tls: bool
    image_storage: str = Field(
        default_factory=lambda: getenv("PROXMOX_IMAGE_STORAGE", "local-lvm")
    )
    extra_headers: Tuple[HttpHeader, ...] = ()


def _load_single_instance_from_env() -> ProxmoxInstanceConfig:
    """
    Load a single Proxmox instance configuration from environment variables.

    Returns:
        ProxmoxInstanceConfig object
    """
    return ProxmoxInstanceConfig(
        instance_id="default",
        pool_id="default",
        host=getenv("PROXMOX_HOST", "localhost"),
        port=int(getenv("PROXMOX_PORT", "8006")),
        user=getenv("PROXMOX_USER", "root"),
        user_realm=getenv("PROXMOX_REALM", "pam"),
        password=getenv("PROXMOX_PASSWORD", "password"),
        node=getenv("PROXMOX_NODE", "proxmox"),
        verify_tls=getenv("PROXMOX_VERIFY_TLS", "1") == "1",
        image_storage=getenv("PROXMOX_IMAGE_STORAGE", "local-lvm"),
    )


def _load_instances_from_env_or_file() -> Tuple[ProxmoxInstanceConfig, ...]:
    """
    Load Proxmox instance configurations from file or environment variables.

    Priority order:
    1. PROXMOX_CONFIG_FILE environment variable (JSON file)
    2. Single-instance environment variables (PROXMOX_HOST, etc.)

    Returns:
        Tuple of ProxmoxInstanceConfig objects
    """
    # Priority 1: Read from PROXMOX_CONFIG_FILE environment variable
    config_file = getenv("PROXMOX_CONFIG_FILE")
    if config_file and os.path.exists(config_file):
        with open(config_file) as f:
            data = json.load(f)
            instances_data = data.get("instances", [])
            return tuple(ProxmoxInstanceConfig(**inst) for inst in instances_data)

    # Priority 2: Single instance from env vars
    if getenv("PROXMOX_HOST"):
        return (_load_single_instance_from_env(),)

    # No configuration found - return empty tuple
    return ()


class ProxmoxSandboxEnvironmentConfig(BaseModel):
    """
    Configuration for a Proxmox sandbox environment.

    Attributes:
        instance_pool_id: Which pool to use for this sample (must match a pool_id in
            PROXMOX_CONFIG_FILE or defaults to "default" for single-instance mode)
        sdn_config: Software-defined networking configuration
        vms_config: Configurations for virtual machines. A tuple retains legacy ordered
            startup semantics. A dictionary enables dependency-based startup, with its
            keys serving as dependency identifiers.
    """

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    # Which pool to use (references pool_id in PROXMOX_CONFIG_FILE)
    instance_pool_id: str = "default"

    # Eval-specific configuration
    sdn_config: SdnConfigType = "auto"
    vms_config: VmConfigs = (
        VmConfig(vm_source_config=VmSourceConfig(built_in="ubuntu24.04")),
    )

    def _identity(self) -> tuple[object, ...]:
        vms_config = self.vms_config
        if isinstance(vms_config, dict):
            # Declaration order controls which ready VMs get the first creation slots.
            vms_identity: object = ("mapping", tuple(vms_config.items()))
        else:
            vms_identity = ("sequence", vms_config)
        return (self.instance_pool_id, self.sdn_config, vms_identity)

    def __hash__(self) -> int:
        """Hash the complete, order-sensitive sandbox configuration."""
        return hash(self._identity())

    def __eq__(self, other: object) -> bool:
        """Compare sandbox configurations using the same identity as hashing."""
        if not isinstance(other, ProxmoxSandboxEnvironmentConfig):
            return NotImplemented
        return self._identity() == other._identity()

    @model_validator(mode="after")
    def validate_vm_startup_config(self) -> "ProxmoxSandboxEnvironmentConfig":
        """Keep legacy tuple scheduling separate from dependency scheduling."""
        if isinstance(self.vms_config, tuple):
            if any(vm.depends_on for vm in self.vms_config):
                raise ValueError(
                    "depends_on requires vms_config to be a dictionary; tuple "
                    "vms_config uses await_before_next_vm"
                )
            return self

        vms_config = self.vms_config
        if any(vm.await_before_next_vm for vm in vms_config.values()):
            raise ValueError(
                "await_before_next_vm is not valid when vms_config is a dictionary; "
                "use depends_on"
            )

        vm_ids = set(vms_config)
        for vm_id, vm_config in vms_config.items():
            if len(vm_config.depends_on) != len(set(vm_config.depends_on)):
                raise ValueError(f"VM {vm_id!r} contains duplicate dependencies")
            unknown = set(vm_config.depends_on) - vm_ids
            if unknown:
                raise ValueError(
                    f"VM {vm_id!r} depends on unknown VMs: {sorted(unknown)}"
                )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(vm_id: str) -> None:
            if vm_id in visiting:
                raise ValueError(f"VM dependency graph contains a cycle at {vm_id!r}")
            if vm_id in visited:
                return
            visiting.add(vm_id)
            for dependency in vms_config[vm_id].depends_on:
                visit(dependency)
            visiting.remove(vm_id)
            visited.add(vm_id)

        for vm_id in vms_config:
            visit(vm_id)

        return self
