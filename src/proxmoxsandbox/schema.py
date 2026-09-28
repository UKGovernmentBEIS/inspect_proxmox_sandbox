"""Data models and schemas for the Proxmox sandbox configuration."""

import json
import os
import re
from os import getenv
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Dict,
    Literal,
    Optional,
    Tuple,
    TypeAlias,
    Union,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic.networks import IPvAnyAddress, IPvAnyNetwork
from pydantic_extra_types.mac_address import MacAddress

from proxmoxsandbox._impl.dependency_graph import reject_cycles


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


# Same regex as pve_verify_dns_name in PVE's JSONSchema.pm
PVE_DNS_NAME_RE = re.compile(
    r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?$"
)


class HealthCheck(BaseModel, frozen=True, extra="forbid", allow_inf_nan=False):
    """
    Guest healthcheck evaluated during sample startup, gating this VM's readiness.

    Field names follow docker compose's `healthcheck` so the semantics are
    guessable, but the defaults do not: compose's 30s/3-retries gives up after
    ~90s, far too short for a booting VM. Durations are seconds, not compose
    duration strings; `interval` and `start_period` take fractions, `timeout`
    is whole seconds.

    Success is exit code 0. `test` is an argument vector with no CMD/CMD-SHELL
    sentinel; use ("sh", "-c", script) or an explicit PowerShell invocation when
    a shell is needed. Requires a running qemu-guest-agent in the guest, even when
    is_sandbox is False; declaring a healthcheck enables the agent device.

    Attempts run through the same command wrapper as sandbox().exec(). A failed
    attempt is a non-zero exit, a guest-side timeout, a `test` that is not
    executable, or output over the exec size limit; each consumes one of
    `retries`. Attempts that cannot reach the guest agent at all do not count
    toward `retries`, but 25 in a row fail the VM, so a guest whose agent dies
    is reported after roughly 25 * (interval + a few seconds). Once passed, the
    healthcheck is not re-run for the rest of the sample.

    Attributes:
        test: Command to run inside the guest.
        interval: Seconds between attempts.
        timeout: Per-attempt limit in whole seconds, enforced inside the guest.
        retries: Consecutive failures tolerated before the sample fails.
        start_period: Grace window in seconds after the first attempt during which
            failures do not count towards retries.
    """

    test: Tuple[str, ...] = Field(min_length=1)
    interval: float = Field(default=5, gt=0)
    # int, unlike the other durations: it is handed to exec(), whose signature
    # Inspect's SandboxEnvironment fixes as `timeout: int | None`, and which
    # enforces it in-guest as `timeout -k 5s {timeout}s`.
    timeout: int = Field(default=30, gt=0)
    retries: int = Field(default=60, gt=0)
    start_period: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_test(self) -> "HealthCheck":
        if not self.test[0]:
            raise ValueError("test needs an executable as its first element")
        if any("\x00" in arg for arg in self.test):
            raise ValueError("test arguments must not contain NUL")
        return self


class VmConfig(BaseModel, frozen=True):
    """
    Configuration for a virtual machine.

    Attributes:
        vm_source_config: The source configuration for the VM
        name: The name of the VM. Must be a valid DNS name and unique within a
            sample: it is the Proxmox VM name, the key used with Inspect's
            sandbox(), and the identifier other VMs use in depends_on. Defaults
            to "default", which is only allowed on the first is_sandbox VM (it is
            always reachable as sandbox("default") anyway), so every other VM
            must be given a name.
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
        vga: The emulated display device. Defaults to "none" (no display); set
            to "std" for guests that need a graphical console. See "Note on vga".
        cpu: The qemu CPU model (e.g. "host", "qemu64", "x86-64-v2"). If unset,
            defaults to "host". Older guest kernels (notably FreeBSD/pfSense) can
            panic on nested virtualization with "host"; use "qemu64" for those.
        depends_on: names of VMs that must be ready before this VM is created.
            Ready means the dependency's healthcheck passed if it has one,
            otherwise that Proxmox reports it running (and, if it needs a guest
            agent, that the agent answers). May name a VM later in vms_config; that
            VM is simply created first.
        healthcheck: optional guest command polled until it exits 0. Gates this
            VM's readiness for depends_on. Requires
            qemu-guest-agent even when is_sandbox is False.

    Note on nics configuration:
    - If set, the VM will be connected to these VNets (one interface per VNet)
    - If set as the empty tuple (), the VM will not have any NICs
    - If left as the default None:
        If the vm_source_config is existing_vm_template_tag,
            the NICs will be left as configured in the template.
        If the vm_source_config is ova or built_in, it will be connected to the first
            VNet.

    Note on vga:
    - Defaults to "none": no emulated display device. This closes the QEMU VGA
        out-of-bounds write path (gitlab.com/qemu-project/qemu/-/work_items/4215),
        which a privileged guest can trigger regardless of whether a console is
        attached. Serial-console access is unaffected (every VM gets serial0).
    - Set to "std" only for guests that require a graphical console (e.g. Windows
        installs with no serial login). This re-adds the vulnerable device.
    """

    vm_source_config: VmSourceConfig
    name: str = "default"
    ram_mb: Optional[int] = 2048
    vcpus: Optional[int] = 2
    nics: Optional[Tuple[VmNicConfig, ...]] = None
    is_sandbox: bool = True
    uefi_boot: bool = False
    disk_controller: Optional[Literal["scsi", "ide"]] = None
    nic_controller: Optional[Literal["virtio", "e1000"]] = None
    firewall: bool = False
    os_type: Optional[OsType] = "l26"
    vga: Literal["none", "std"] = "none"
    cpu: Optional[str] = None
    depends_on: Tuple[str, ...] = ()
    healthcheck: Optional[HealthCheck] = None

    @model_validator(mode="before")
    @classmethod
    def _reject_await_before_next_vm(cls, values: Any) -> Any:
        # await_before_next_vm was replaced by depends_on/healthcheck. Extra
        # keys are otherwise ignored, so without this a config that still sets
        # it would load clean and silently stop waiting.
        if isinstance(values, dict) and "await_before_next_vm" in values:
            raise ValueError(
                "await_before_next_vm has been removed. To make a later VM wait "
                "for this one, give the later VM depends_on=(<this VM's name>,), "
                "and give this VM a healthcheck if running is not enough."
            )
        return values

    @field_validator("name", mode="before")
    @classmethod
    def _none_means_default(cls, value: Any) -> Any:
        # Configs recorded before name had a default serialised it as null.
        return "default" if value is None else value

    @field_validator("name")
    @classmethod
    def _validate_name_is_dns_name(cls, name: str) -> str:
        if not PVE_DNS_NAME_RE.match(name):
            raise ValueError(
                f"VM name {name!r} is not a valid DNS name (Proxmox requires "
                + "dot-separated labels of letters, digits and hyphens, not "
                + "starting or ending with a hyphen)"
            )
        return name

    @property
    def requires_guest_agent(self) -> bool:
        """Whether sandbox access or a healthcheck needs QGA enabled in Proxmox."""
        return self.is_sandbox or self.healthcheck is not None


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
        vms_config: Configurations for virtual machines
    """

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    # Which pool to use (references pool_id in PROXMOX_CONFIG_FILE)
    instance_pool_id: str = "default"

    # Eval-specific configuration
    sdn_config: SdnConfigType = "auto"
    vms_config: Tuple[VmConfig, ...] = (
        VmConfig(vm_source_config=VmSourceConfig(built_in="ubuntu24.04")),
    )

    def vm_names(self) -> Tuple[str, ...]:
        """Names in vms_config order."""
        return tuple(vm.name for vm in self.vms_config)

    @model_validator(mode="after")
    def _validate_vm_names(self) -> "ProxmoxSandboxEnvironmentConfig":
        first_sandbox = next(
            (i for i, vm in enumerate(self.vms_config) if vm.is_sandbox), None
        )
        if first_sandbox is None:
            raise ValueError(
                "No default sandbox found: at least one VM must have is_sandbox=True"
            )
        seen: Dict[str, int] = {}
        for i, vm in enumerate(self.vms_config):
            if vm.name == "default" and i != first_sandbox:
                raise ValueError(
                    f"vms_config[{i}] is named 'default' (the default when no name "
                    f"is given), which is reserved for the first is_sandbox=True VM "
                    f"(Inspect's default sandbox); give it a name"
                )
            if vm.name in seen:
                raise ValueError(
                    f"Duplicate VM name {vm.name!r} at vms_config[{seen[vm.name]}] "
                    f"and vms_config[{i}]; names identify VMs to Inspect and to "
                    f"depends_on, so they must be unique within a sample"
                )
            seen[vm.name] = i
        return self

    @model_validator(mode="after")
    def _validate_dependencies(self) -> "ProxmoxSandboxEnvironmentConfig":
        names = self.vm_names()

        for name, vm in zip(names, self.vms_config):
            if len(vm.depends_on) != len(set(vm.depends_on)):
                raise ValueError(f"{name!r} lists a duplicate dependency")
            if name in vm.depends_on:
                raise ValueError(f"{name!r} depends on itself")
            for dep in vm.depends_on:
                if dep not in names:
                    raise ValueError(
                        f"{name!r} depends on unknown VM {dep!r}. "
                        f"Known names: {sorted(names)}"
                    )

        reject_cycles({name: vm.depends_on for name, vm in zip(names, self.vms_config)})
        return self
