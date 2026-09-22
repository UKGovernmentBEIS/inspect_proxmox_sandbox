# Build environment for the inspect-proxmox-host debs: plain trixie with debhelper and linters.
FROM debian:trixie
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential debhelper devscripts lintian shellcheck systemd \
    && rm -rf /var/lib/apt/lists/*
