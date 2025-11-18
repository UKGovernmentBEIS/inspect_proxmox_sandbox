#!/bin/bash

# Run tests for inspect-proxmox-sandbox

set -e

# Activate venv and run pytest
# Run all tests in test_multi_instance_pools.py by default
.venv/bin/python -m pytest tests/proxmoxsandboxtest/test_multi_instance_pools.py -v "$@"
