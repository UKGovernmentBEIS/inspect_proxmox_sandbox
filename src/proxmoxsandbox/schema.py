"""Data models and schemas for the Proxmox sandbox configuration."""

from ipaddress import ip_address, ip_network
from os import getenv
from pathlib import Path
from typing import Annotated, Literal, Optional, Tuple, TypeAlias, Union

from pydantic import BaseModel, Field, model_validator
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
    gateway: Optional[IPvAnyAddress] = None
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
        allow_domains: Allowlist of domains that sandbox VMs may reach. All other
            egress is blocked. When non-empty, a gateway VM is automatically provisioned
            per eval sample; it enforces the allowlist using dnsmasq (DNS-level) +
            nftables (IP-level). The gateway VM is outside the sandbox VM, so root
            inside the sandbox cannot bypass it.

            Subdomains must be listed explicitly: "gnu.org" does NOT cover
            "ftp.gnu.org" — list each subdomain you need. Leave empty (the
            default) for unrestricted internet access.

            When allow_domains is non-empty:
            - snat on sandbox subnets is set to False (the gateway VM does NAT)
            - An extra internal VNet is created for the gateway VM's external interface
            - Sandbox VMs learn the gateway as their default router via DHCP
            - The gateway IP in SubnetConfig must not be set (leave it as its default).
              Setting it explicitly will raise a ValueError at construction time.

            Constraints (validated at construction time):
            - use_pve_ipam_dnsnmasq must be True
            - exactly one vnet_config must be provided
            - that vnet must have exactly one subnet
            - the subnet's DHCP range must not include network-address+2

            Requires Proxmox 9+. The gateway VM template is built once on first use
            (one-time cost: ~5–10 minutes). Per-eval overhead is ~30–60 s for the
            gateway clone startup before the sandbox VM boots.

            DNS upstream: Google (8.8.8.8) is hardcoded inside the gateway VM.
            Environments that block outbound port 53 to 8.8.8.8 will not be able to
            resolve allowed domains.

            Known limitations:
            - Filtering is IP-level, not URL-level.  All TCP/UDP ports to an
              allowed domain's IPs are permitted, not just HTTP/HTTPS.
            - Only apex domain IPs are pre-seeded in the nftables filter at
              provision time.  Subdomains (e.g. ftp.gnu.org when "gnu.org"
              is allowed) have their IPs resolved by dnsmasq at query time, but
              those IPs are NOT added to the filter — traffic will be dropped.
              This affects apt-get, pip, and similar tools that use subdomains.
              A future improvement is to enable dnsmasq's nftset= support.
            - IPv6 is blocked at two layers: sandbox VMs provisioned from
              built-in templates have accept-ra: false in their cloud-init
              network config (preventing SLAAC address assignment); custom VM
              sources (OVA, existing_vm_template_tag) must configure this
              independently.  The gateway's nftables inet forward chain drops
              any IPv6 that routes through it.  Note: dhcp6: false alone only
              disables DHCPv6; accept-ra is needed to block SLAAC.
            - DNS-over-TLS (port 853) is dropped at the gateway.
              DNS-over-HTTPS (port 443) is not intercepted but is blocked by
              the IP-level filter unless a DoH provider happens to share an IP
              with an explicitly allowed domain (unlikely in practice).
    """

    vnet_configs: Tuple[VnetConfig, ...]
    use_pve_ipam_dnsnmasq: bool = True
    allow_domains: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_allow_domains_constraints(self) -> "SdnConfig":
        """Fail fast on configs statically incompatible with allow_domains.

        Dynamic constraints (e.g. no free external CIDR) can only be checked at
        provision time; these are the structural ones that can be caught now.
        """
        if not self.allow_domains:
            return self
        if not self.use_pve_ipam_dnsnmasq:
            raise ValueError(
                "allow_domains requires use_pve_ipam_dnsnmasq=True "
                "(the gateway VM uses Proxmox IPAM for its static IP assignment)"
            )
        if len(self.vnet_configs) != 1:
            raise ValueError(
                f"allow_domains requires exactly one vnet_config, "
                f"got {len(self.vnet_configs)}"
            )
        subnet_list = self.vnet_configs[0].subnets
        if len(subnet_list) != 1:
            raise ValueError(
                f"allow_domains requires exactly one subnet per vnet, "
                f"got {len(subnet_list)}"
            )
        # The gateway VM is assigned network-address+2.  Validate that the DHCP
        # pool does not include that address, which would cause an IPAM conflict.
        subnet = subnet_list[0]
        gateway_vm_ip = ip_network(str(subnet.cidr)).network_address + 2
        for dhcp_range in subnet.dhcp_ranges:
            start = ip_address(str(dhcp_range.start))
            end = ip_address(str(dhcp_range.end))
            if start <= ip_address(gateway_vm_ip) <= end:
                raise ValueError(
                    f"allow_domains: gateway VM will be assigned {gateway_vm_ip} "
                    f"(subnet network address +2), but that address falls within "
                    f"the DHCP range {dhcp_range.start}–{dhcp_range.end}. "
                    "Adjust the DHCP range to exclude it (e.g. start at .10 or higher)."
                )
        if subnet.gateway is not None and ip_address(str(subnet.gateway)) != ip_address(gateway_vm_ip):
            raise ValueError(
                f"allow_domains: do not set the gateway in SubnetConfig manually. "
                f"It will be automatically set to {gateway_vm_ip} (network-address+2). "
                f"You specified {subnet.gateway}; either remove the gateway field or "
                f"set it to {gateway_vm_ip}."
            )
        return self


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
    built_in: Literal["ubuntu24.04", "debian13", "kali2025.3"] | None = None

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
        disk_controller: The disk controller type. If unset, defaults to "scsi"
        nic_controller: The NIC controller type. If unset, defaults to "virtio".
            This is applied to all virtual network interfaces.
        os_type: The OS type. If unset, defaults to "l26". Only for OVA. See
            https://pve.proxmox.com/wiki/Manual:_qm.conf for more details
        firewall: if True, enables the Proxmox VM-level firewall on all NICs.
            Proxmox firewall rules must be configured separately (e.g. via the UI or
            Proxmox API). Defaults to False.

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
    firewall: bool = False
    disk_controller: Optional[Literal["scsi", "ide"]] = None
    nic_controller: Optional[Literal["virtio", "e1000"]] = None
    os_type: Optional[
        Literal[
            "l24",
            "l26",
            "other",
            "solaris",
            "w2k",
            "w2k3",
            "w2k8",
            "win10",
            "win11",
            "win7",
            "win8",
            "wvista",
            "wxp",
        ]
    ] = "l26"


class ProxmoxSandboxEnvironmentConfig(BaseModel, frozen=True):
    """
    Configuration for a Proxmox sandbox environment.

    Attributes:
        host: The hostname or IP address of the Proxmox server
        port: The port number for the Proxmox API, usually 8006
        user: The username for Proxmox authentication, 'root' unless you have configured
            custom auth
        user_realm: The authentication realm for the Proxmox user, 'pam' unless you have
            configured custom auth
        password: The password for Proxmox authentication
        node: The name of the Proxmox node, usually 'proxmox'
        verify_tls: Whether to verify the Proxmox server's TLS certificate.
            1 = verify, 0 = do not verify
        sdn_config: Software-defined networking configuration
            "auto": Create a simple SDN with a single subnet.  The IP addresses will not
                be predictable as it depends on what subnets already exist.
            None: No SDN will be created. VMs can reference existing VNETs in Proxmox
                by their aliases. This is an advanced feature and not recommended for
                normal use.
            SdnConfig: Custom SDN configuration
        vms_config: Configurations for virtual machines
    """

    host: str = Field(default_factory=lambda: getenv("PROXMOX_HOST", "localhost"))
    port: int = Field(default_factory=lambda: int(getenv("PROXMOX_PORT", "8006")))
    user: str = Field(default_factory=lambda: getenv("PROXMOX_USER", "root"))
    user_realm: str = Field(default_factory=lambda: getenv("PROXMOX_REALM", "pam"))
    password: str = Field(
        default_factory=lambda: getenv("PROXMOX_PASSWORD", "password")
    )
    node: str = Field(default_factory=lambda: getenv("PROXMOX_NODE", "proxmox"))
    verify_tls: bool = Field(
        default_factory=lambda: getenv("PROXMOX_VERIFY_TLS", "1") == "1"
    )

    sdn_config: SdnConfigType = "auto"
    vms_config: Tuple[VmConfig, ...] = (
        VmConfig(vm_source_config=VmSourceConfig(built_in="ubuntu24.04")),
    )
