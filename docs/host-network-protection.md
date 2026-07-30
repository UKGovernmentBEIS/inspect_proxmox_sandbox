# Keeping the host available under a hostile guest

The [host firewall isolation](../README.md#host-firewall-isolation) keeps guests *off*
the host control plane. Two further host-level controls, installed automatically by the
provisioning scripts (`scripts/virtualized_proxmox/build_proxmox_auto.sh`,
`scripts/ec2/userdata.sh`), keep the host *available* when a sandbox guest floods the
network — e.g. an in-range `nmap`/`masscan` in a cyber eval. Both live on the host.

## 1. Control-plane conntrack immunity (NOTRACK)

Each sandbox network is an SDN "simple" zone with a host gateway + SNAT, so every guest
connection is routed/NAT'd by the host and consumes an entry in the host's global
`nf_conntrack` table (default 262144). A guest that opens many flows fills it, and the
kernel then drops *new* connections host-wide (`nf_conntrack: table full, dropping
packet`). Since this provider opens a fresh connection to `pveproxy:8006` per request,
Inspect's own control connection gets dropped — the eval loses the host and retries for
minutes. With the Proxmox firewall on, `br_netfilter` conntracks even intra-range
guest↔guest traffic, so internal scans do this too.

The provisioners exempt the control-plane ports (8006, 22) from connection tracking — a
`raw` `NOTRACK` rule in both directions — so those packets need no table slot and
survive a full table:

```bash
for _port in 8006 22; do
    iptables -w -t raw -C PREROUTING -p tcp --dport "$_port" -j CT --notrack 2>/dev/null \
        || iptables -w -t raw -I PREROUTING 1 -p tcp --dport "$_port" -j CT --notrack
    iptables -w -t raw -C OUTPUT -p tcp --sport "$_port" -j CT --notrack 2>/dev/null \
        || iptables -w -t raw -I OUTPUT 1 -p tcp --sport "$_port" -j CT --notrack
done
```

This deliberately does **not** raise `nf_conntrack_max`: a bigger table only delays
exhaustion, it doesn't protect the control plane.

## 2. Per-tap ingress rate policer (DoS bound)

NOTRACK stops the control-plane connection being *dropped*, but not the host CPU a flood
burns processing new flows in softirq. A guest scanning fast enough (many cores,
`masscan`) can still saturate the host and starve pveproxy — a DoS NOTRACK can't stop,
and which raising `nf_conntrack_max` makes *worse* (bigger table, longer to drain).

So the provisioners cap each sandbox VM's guest→host packet rate with a `tc` **ingress
policer on the tap**, which drops excess *before* conntrack. (A `connlimit` cap in
`FORWARD` doesn't work here: it runs *after* the per-packet conntrack processing, so the
host still pays the cost that causes the DoS.) Tap names are per-VM and dynamic, so a
udev rule applies the policer to each tap as it appears:

- `/usr/local/bin/inspect-proxmox-tap-policer.sh <tap>` — adds a `tc` ingress qdisc and a
  `matchall action police pkts_rate <PPS> drop` filter. It re-asserts for a few seconds
  because Proxmox's NIC bring-up wipes a qdisc applied at the udev `add` event.
- a templated systemd service (`inspect-proxmox-tap-policer@.service`) plus a udev rule
  (`KERNEL=="tap*i*"`) that starts it per tap.

The `PPS` cap is generous for real scanning but well below what saturates a multi-core
host; a determined flood just hits the ceiling instead of taking the host down. Requires
a kernel with `tc` packet-rate policing (`police pkts_rate`) — PVE 9 has it.

If you provision Proxmox some other way, replicate both controls on the node and persist
them across reboots (the NOTRACK rules are re-applied each boot by a systemd oneshot; the
policer is re-applied per tap by the udev rule).
