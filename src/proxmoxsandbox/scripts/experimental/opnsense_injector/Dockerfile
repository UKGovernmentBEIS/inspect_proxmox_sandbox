FROM mcr.microsoft.com/dotnet/runtime:8.0

RUN apt-get update && apt-get install -y \
    qemu-utils \
    wget \
    unzip \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN wget -q https://github.com/SvenGDK/UFS2Tool/releases/download/v4.0/linux-x64.zip \
    -O /tmp/ufs2tool.zip \
    && unzip /tmp/ufs2tool.zip -d /opt \
    && chmod +x /opt/linux-x64/UFS2Tool \
    && rm /tmp/ufs2tool.zip

COPY inject_config.sh /inject_config.sh
RUN chmod +x /inject_config.sh

ENTRYPOINT ["/inject_config.sh"]
