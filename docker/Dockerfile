# Build environment for the ReSpeaker Core v2 image. Everything is installed
# from a fixed snapshot.debian.org date, so the toolchain never drifts.
ARG BUILDER_IMAGE
FROM ${BUILDER_IMAGE}

ARG DEBIAN_SNAPSHOT
ARG DEBIAN_SUITE

RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/%s %s main\n' \
        "$DEBIAN_SNAPSHOT" "$DEBIAN_SUITE" > /etc/apt/sources.list && \
    printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/%s %s-security main\n' \
        "$DEBIAN_SNAPSHOT" "$DEBIAN_SUITE" >> /etc/apt/sources.list && \
    printf 'Acquire::Retries "5";\nAcquire::http::Timeout "60";\n' > /etc/apt/apt.conf.d/80retries && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        arch-test bc binutils-arm-linux-gnueabihf bison bzip2 ca-certificates \
        cpio curl device-tree-compiler dosfstools e2fsprogs fdisk file flex \
        gcc gcc-arm-linux-gnueabihf git gnupg kmod libarchive13t64 libc6-dev \
        libgnutls28-dev libssl-dev make mmdebstrap patchutils python3 python3-dev \
        python3-cryptography python3-pyelftools python3-setuptools \
        qemu-user rsync swig uuid-runtime xz-utils zstd && \
    rm -rf /var/lib/apt/lists/*

ENV LANG=C.UTF-8
