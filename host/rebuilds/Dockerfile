# Build environment for the rebuilt Proxmox packages: trixie plus the pve-no-subscription repo.
FROM debian:trixie
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates wget \
    && wget -q https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg \
        -O /usr/share/keyrings/proxmox-archive-keyring.gpg \
    && echo "136673be77aba35dcce385b28737689ad64fd785a797e57897589aed08db6e45  /usr/share/keyrings/proxmox-archive-keyring.gpg" | sha256sum -c \
    && printf 'Types: deb\nURIs: http://download.proxmox.com/debian/pve\nSuites: trixie\nComponents: pve-no-subscription\nSigned-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n' \
        > /etc/apt/sources.list.d/pve-no-subscription.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        build-essential devscripts equivs git quilt python3 lintian \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
