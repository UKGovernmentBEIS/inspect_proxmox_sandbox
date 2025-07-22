# Changelog

## 0.8.0 - 2025-07-22

- Allow pre-existing VNet in VM config

## 0.7.0 - 2025-07-14

- Increase timeout for VM CRUD operations
- Remove "restore from backup" functionality as it was unused

## 0.6.1 - 2025-05-16

- Bugfix: if no timeout is specified by Inspect for long-running exec, RetryError happens after 30s.
- Docs: correct OVA docs to reflect recent 0.6.0 change

## 0.6.0 - 2025-05-14

- Performance enhancement: OVAs generate template VMs, per-sample VMs are now linked clones.

## 0.5.1 - 2025-05-13

- Fix sandbox_cleanup=False being ignored when sample setup fails
 
## 0.5.0 - 2025-04-17

- Add machine type option (Linux, Windows, etc.)
- Upgrade scripted Proxmox build to version 8.4

## 0.4.0 - 2025-04-07

- Allow vmdk in convert_ova.sh
- Allow ide disk controller and e1000 network controller
- Sample CTF eval

## 0.3.1 - 2025-04-04

- Create 250G root space by default
- Scripts moved into src/proxmoxsandbox/scripts

## 0.3.0 - 2025-03-31 

Initial release
