# Inspect Proxmox Sandbox

## Purpose

This plugin for [Inspect](https://inspect.aisi.org.uk/) allows you to use virtual machines, 
running within a [Proxmox](https://www.proxmox.com/products/proxmox-virtual-environment/overview) instance, as [sandboxes](https://inspect.aisi.org.uk/sandboxing.html).

## Installing

Add this using [Poetry](https://python-poetry.org/)

```
poetry add git+ssh://git@github.com/UKGovernmentBEIS/inspect_proxmox_sandbox.git
```

or in [uv](https://github.com/astral-sh/uv),

```
uv add git+ssh://git@github.com/UKGovernmentBEIS/inspect_proxmox_sandbox.git
```

## Requirements

This plugin assumes you already have a Proxmox instance set up, and that you have admin access to it.

Set the following environment variables (e.g. in a [`.env`](https://dotenvx.com/docs/env-file) file)

```
PROXMOX_HOST=[IP address or domain name of the host]
PROXMOX_PORT=[port, e.g 8006]
PROXMOX_USER=[user, usually 'root']
PROXMOX_REALM=[authentication realm, usually 'pam' unless you have configured custom auth]
PROXMOX_PASSWORD=[password]
PROXMOX_NODE=[node name, usually 'proxmox']
PROXMOX_VERIFY_TLS=[1 = verify, 0 = do not verify]
```

Your Proxmox instance must allow additional storage types in `local` from the default.
You can run this on your Proxmox node to configure them:

```bash
pvesh set /storage/local -content iso,vztmpl,backup,snippets,images,rootdir,import
```

SDN requires you to configure dnsmasq, see the [Proxmox SDN documentation](https://pve.proxmox.com/pve-docs/chapter-pvesdn.html#pvesdn_install_dhcp_ipam). Note, the commands on that page must be run on the Proxmox node, not your local machine.

For details on how to set up a local Proxmox instance for testing, see [CONTRIBUTING.md](CONTRIBUTING.md#local-proxmox)

## Configuring

Here is a full example sandbox configuration. 

Note that some of the fields (e.g. subnets) are tuples, so the trailing comma is vital 
if there is only a single item in the tuple.

Most tools use only the first sandbox, so you should list the one you want the agent to operate from first.

Virtual machines must have the [qemu-guest-agent](https://pve.proxmox.com/wiki/Qemu-guest-agent) installed, unless they are not sandboxes. 
At least one VM in the configuration must be a sandbox.

```python
sandbox=SandboxEnvironmentSpec(
    "proxmox",
    ProxmoxSandboxEnvironmentConfig(
        # These config items will be taken from environment variables, if not specified here
        host=[IP address or domain name of the host]
        port=[port, e.g 8006]
        user=[user, usually 'root']
        user_realm=[authentication realm, 'pam' unless you have configured custom auth]
        password=[password]
        node=[node name, usually 'proxmox']
        verify_tls=[True: verify, False: do not verify]
        # End config from environment

        vms_config=(
            VmConfig(
                # A virtual machine that this provider will install and configure automatically.
                vm_source_config=VmSourceConfig(
                    built_in="ubuntu24.04" # currently supported: "ubuntu24.04"; see schema.py
                ),
                name="romeo", # name is optional, but recommended - it will be shown in the Proxmox GUI. Must be a valid DNS name.
                ram_mb=512, # optional, default is 2048 MB
                vcpus=4, # optional, default is 2. No attempt is made to check that this will fit in the Proxmox host.
                uefi_boot=True, # optional, default is False. Generally only needed for Windows VMs.
                is_sandbox=False, # optional, default is True. A virtual machine that is not a sandbox; the qemu-guest-agent need not be installed.
                disk_controller="scsi", # optional, default will be SCSI. Can also use "ide" for older VM images.
                nic_controller="virtio", # optional, default will be VirtIO. Can also use "e1000" for older VM images.
                # If you have more than one VNet, assign the VM to the VNet via nics.
                # You can assign more than one, to give the VM more than one network interface.
                # If you leave this blank, your VM will be assigned to the first VNet.
                nics=(
                    VmNicConfig(
                        # This alias *must* match the alias in one of the VnetConfigs
                        vnet_alias="my special vnet",
                        # Specifying a MAC address is optional - only needed if you
                        # are doing fancy things with DHCP in your eval
                        mac="00:16:3d:1d:eb:a0"
                    ),
                )
                # extra_proxmox_native_config = dict() TODO
            ),
            # A virtual machine from a local OVA, which will be uploaded from here to the Proxmox server.
            VmConfig(
                vm_source_config=VmSourceConfig(
                    ova=Path("./tests/oVirtTinyCore64-13.11.ova")
                ),
                os_type="win10" # optional, default "l26".
            ),
            # A virtual machine to clone from an existing template VM.
            # This is *not recommended* since it is dependent on configuring a 
            # customised Proxmox instance that contains the template VM before
            # the eval start.
            VmConfig(
                vm_source_config=VmSourceConfig(
                    existing_vm_template_tag="java_server"
                ),
            ),
            # A virtual machine that is connected to a predefined VNET.
            # This is *not recommended* since it is dependent on configuring a
            # customised Proxmox instance that contains SDN configurations before
            # the eval start.
            VmConfig(
                vm_source_config=VmSourceConfig(
                    built_in="ubuntu24.04"
                ),
                nics=(
                    VmNicConfig(
                        # If you reference a pre-existing VNET here, and
                        # set sdn_config=None in the ProxmoxSandboxEnvironmentConfig,
                        # it will look for the VNET alias in the existing Proxmox SDN.
                        vnet_alias="existing vnet alias",
                    ),
                )
            ),
            # A virtual machine with no network access.
            VmConfig(
                # ... snip ...           
                nics=()
            ),
        ),
        # You will need a separate SDN per sample, or the VMs will be able to see each other
        # IP ranges *must* be distinct, unfortunately.
        # If you don't care about any of this, you can set this field to the string "auto"
        # and you will get an IP range somewhere in 192.168.[2 - 253].0/24
        sdn_config=SdnConfig(
            vnet_configs=(
                VnetConfig(
                    # You can leave subnets blank if you are handling IPAM yourself (e.g. with your own pfsense instance as a VM)
                    subnets=(
                        SubnetConfig(
                            cidr=ip_network("192.168.20.0/24"),
                            gateway=ip_address("192.168.20.1"),
                            # If you set snat=False, VMs will see each other but not the wider Internet.
                            snat=True,
                            dhcp_ranges=(
                                DhcpRange(
                                    start=ip_address("192.168.20.50"),
                                    end=ip_address("192.168.20.100"),
                                ),
                            ),
                        ),
                    ),
                    alias="my special vnet"
                ),
            ),
            # Set use_pve_ipam_dnsnmasq to True if you want your instances to be able to access the Internet
            use_pve_ipam_dnsnmasq=True,
        ),
    ),
)
```

### Using Existing Proxmox VNETs (Advanced/Not Recommended)

**⚠️ WARNING**: This feature is intended for advanced users with specific integration requirements. For most use cases, you should let the sandbox manage its own network configuration using the standard `sdn_config` options.

If you have an existing Proxmox environment with pre-configured VNETs that you need to connect to, you can reference them by setting `sdn_config=None` and using the VNET aliases in your VM configurations:

```python
sandbox = SandboxEnvironmentSpec(
    type="proxmox",
    config=ProxmoxSandboxEnvironmentConfig(
        vms_config=(
            VmConfig(
                vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
                nics=(
                    VmNicConfig(
                        vnet_alias="existing-vnet-alias",  # Must match an existing VNET alias in Proxmox
                    ),
                ),
            ),
        ),
        sdn_config=None,  # Disable SDN creation - use existing VNETs only
    ),
)
```

## Using OVA files

Proxmox supports OVA import but not OVA export. It is possible to extract the disk images of VMs 
from a Proxmox server in qcow2 format (instructions for this can be found online).

Once you have the disk images locally, you can use the convenience script `src/proxmoxsandbox/scripts/ova/convert_ova.sh`
to convert it into an OVA.

This provider creates a template VM for every OVA-type VM specified in an eval.
Next time you run the eval, a linked clone of the template VM will be created.
This is for performance. If you change the OVA, as long as the filesize changes, 
a new template VM will be created. If you change the OVA but the filesize remains
the same, you should manually delete it from the Proxmox server.

These template VMs are *not* cleaned up because that needs to happen outside
the lifecycle of an Inspect eval. You need to do this manually at the moment.

## Observing the VMs

Note, if you are having problems, then setting Inspect's `sandbox_cleanup=False` will be helpful.

### Logging in

If you want to log into a sandbox VM, the Proxmox UI lets you open a console window, but you might not know the password.

You can use the following command on the Proxmox server (open Datacenter -> Proxmox node -> Shell):

```bash
export PROXMOX_NODE=proxmox # change this if necessary
export VM_ID=101 # change this to the correct VM ID
export NEW_PASSWORD=Password2.0 # choose a password
export VM_USERNAME=ubuntu # change as appropriate
pvesh create "/nodes/$PROXMOX_NODE/qemu/$VM_ID/agent/exec" --command bash --command "-c" --command "echo $VM_USERNAME:$NEW_PASSWORD | chpasswd"
```

## Snapshot

QEMU, the virtualization library used by Proxmox, allows you to snapshot a running virtual machine, 
including the running processes. See [snapshots.py](./src/proxmoxsandbox/experimental/snapshots.py) for example tools that use this.

## Sample eval

See [ctf4.py](./src/proxmoxsandbox/experimental/ctf4.py) for an example capture-the-flag eval with:

- A VM for the agent
- A victim VM which the agent must hack into and obtain the root password

## Identifying created resources

Every VM created by this sandbox provider is tagged `inspect`. 
(Tags will also be duplicated if they exist on a VM already, for `existing_backup_name`- and `existing_vm_template_tag`-type VMs)

SDN zones have the pattern `[3 letters from eval task name][random 3 digits][z]`. VNets are similar and can be identified from their containing zone.

Some resources will persist after the eval is complete:

- the built-in VM feature creates a template VM `inspect-ubuntu24.04`
- the built-in VM feature creates a SDN zone called `inspvmz`
- uploaded OVAs are left in place
- cloud-init ISOs are left in place

Environment cleanup is partially implemented. There is no way to tag all the resources
created by a particular eval. Therefore the cleanup process for `inspect sandbox cleanup proxmox` 
will delete:

- all VMs tagged `inspect` 
- any SDN zones created with names matching the pattern above.

## Versioning

The project follows [semantic versioning](https://semver.org/) and is aiming for a 1.0 release. Until then, backward-compatibility is not guaranteed.

## Feature Roadmap

- Proxmox server health and config check
- Normalize having a pfSense VM as the default route for networking
- Firewall off the SDN from the Proxmox server and from other SDNs
- Add more built-in VMs (Debian, Kali)
- Support cloud-init for VM definition
- Escape hatch for Proxmox API so you can specify arbitrary parameters during VM / SDN creation 

## Tech debt

- Large OVA uploads use PycURL, because neither aiohttp nor httpx worked with large uploads
- Inconsistent use of task_wrapper and tenacity

## Developing

See [CONTRIBUTING.md](CONTRIBUTING.md)
