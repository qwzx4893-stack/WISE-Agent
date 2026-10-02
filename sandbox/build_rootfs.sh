#!/usr/bin/env bash
# ==========================================================================
# build_rootfs.sh — produce a minimal Debian rootfs for Agent OS sandbox.
# ==========================================================================
#
# Goals:
#   * idempotent (safe to re-run)
#   * mobile-friendly (no systemd / no X11 / no desktop)
#   * works inside ProotSandbox (agent_core.py)
#   * picks the best available bootstrapper:
#         mmdebstrap → debootstrap → docker export → linuxcontainers tarball
#
# Usage:
#   sudo bash sandbox/build_rootfs.sh                  # default location
#   AGENT_ROOTFS=/path/to/root bash sandbox/build_rootfs.sh
#   AGENT_DEBIAN_RELEASE=bookworm bash sandbox/build_rootfs.sh
#   AGENT_FORCE_REBUILD=1 bash sandbox/build_rootfs.sh
# ==========================================================================
set -euo pipefail

ROOTFS="${AGENT_ROOTFS:-${HOME}/agent-os/sandbox/rootfs}"
RELEASE="${AGENT_DEBIAN_RELEASE:-bookworm}"
MIRROR="${AGENT_DEBIAN_MIRROR:-http://deb.debian.org/debian}"
ARCH="${AGENT_DEBIAN_ARCH:-$(dpkg --print-architecture 2>/dev/null || echo amd64)}"
FORCE_REBUILD="${AGENT_FORCE_REBUILD:-0}"
STAMP_FILE="${ROOTFS}/.agent-os-stamp"

# Mobile-optimised package set. Anything that pulls in systemd, X11, or
# graphical desktop services must be kept *out* of this list.
#
# NB: ``npm`` and ``nodejs`` are installed in the post-bootstrap step
# rather than in the debootstrap include list because their maintainer
# scripts often fail in a freshly-extracted minbase before /dev or
# /proc are wired up.
BASE_PACKAGES=(
    ca-certificates
    coreutils
    findutils
    grep sed gawk diffutils less
    bash
    procps psmisc lsof
    curl wget
    git
    jq
    ripgrep
    tar gzip bzip2 xz-utils unzip zip
    openssl
    sqlite3
    sudo
    tzdata
    locales
    make build-essential pkg-config
    python3 python3-pip python3-venv
    python3-setuptools python3-wheel
)
# Installed after the base bootstrap completes (see post_install).
EXTRA_PACKAGES=(
    nodejs
    npm
)

log()  { printf '\033[1;36m[rootfs]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[rootfs]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[rootfs]\033[0m %s\n' "$*" >&2; exit 1; }

ensure_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        die "must run as root (use sudo). got uid=$(id -u)"
    fi
}

stamp_ok() {
    [[ -f "${STAMP_FILE}" ]] && [[ "${FORCE_REBUILD}" != "1" ]]
}

write_stamp() {
    {
        printf 'release=%s\n' "${RELEASE}"
        printf 'arch=%s\n'    "${ARCH}"
        printf 'built_at=%s\n' "$(date -u +%FT%TZ)"
        printf 'method=%s\n'  "${1:-unknown}"
    } > "${STAMP_FILE}"
}

bootstrap_with_mmdebstrap() {
    log "bootstrapping with mmdebstrap (${RELEASE}/${ARCH})"
    mmdebstrap \
        --variant=minbase \
        --architectures="${ARCH}" \
        --include="$(IFS=,; echo "${BASE_PACKAGES[*]}")" \
        "${RELEASE}" "${ROOTFS}" "${MIRROR}"
    write_stamp mmdebstrap
}

bootstrap_with_debootstrap() {
    log "bootstrapping with debootstrap (${RELEASE}/${ARCH})"
    debootstrap \
        --arch="${ARCH}" \
        --variant=minbase \
        --include="$(IFS=,; echo "${BASE_PACKAGES[*]}")" \
        "${RELEASE}" "${ROOTFS}" "${MIRROR}"
    write_stamp debootstrap
}

bootstrap_with_docker() {
    log "bootstrapping by exporting debian:${RELEASE}-slim from docker"
    local cid
    cid="$(docker create "debian:${RELEASE}-slim")"
    mkdir -p "${ROOTFS}"
    docker export "${cid}" | tar -C "${ROOTFS}" -xf -
    docker rm "${cid}" >/dev/null
    chroot "${ROOTFS}" bash -c '
        set -e
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y --no-install-recommends \
            '"$(printf '%s ' "${BASE_PACKAGES[@]}")"'
        apt-get clean
        rm -rf /var/lib/apt/lists/*
    '
    write_stamp docker-export
}

post_install() {
    log "post-install: installing extras + hardening"
    # Run via proot when chroot is unavailable (e.g. unprivileged Docker).
    local runner=(chroot "${ROOTFS}")
    if ! command -v chroot >/dev/null 2>&1 && command -v proot >/dev/null; then
        runner=(proot -r "${ROOTFS}" -0 -w /)
    fi
    "${runner[@]}" bash -eu <<CHROOT
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq || true
        apt-get install -y --no-install-recommends \
            $(printf '%s ' "${EXTRA_PACKAGES[@]}") || true
        # Locale + timezone defaults.
        if [ -f /etc/locale.gen ]; then
            sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
            locale-gen >/dev/null 2>&1 || true
        fi
        update-alternatives --install /usr/bin/python python /usr/bin/python3 1 || true
        mkdir -p /root/.cache/pip /root/.config /workspace /home/agent
        # Strip suid bits from binaries we won't use inside a sandbox.
        for f in /usr/bin/su /usr/bin/sudo /usr/bin/chage /usr/bin/expiry \
                 /usr/bin/gpasswd /usr/bin/passwd /usr/bin/mount \
                 /usr/bin/umount /usr/bin/newgrp /usr/sbin/unix_chkpwd; do
            [ -e "\$f" ] && chmod -s "\$f" 2>/dev/null || true
        done
        # No need for systemd/init inside proot.
        find /lib/systemd /usr/lib/systemd /etc/systemd 2>/dev/null \
            -mindepth 1 -delete 2>/dev/null || true
        apt-get clean
        rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/man/* \
               /usr/share/info/* /var/cache/apt/archives/*.deb \
               /var/log/*.log /tmp/* 2>/dev/null || true
CHROOT
    # /etc/resolv.conf so DNS works from inside proot/chroot.
    if [[ -e /etc/resolv.conf ]]; then
        cp /etc/resolv.conf "${ROOTFS}/etc/resolv.conf" 2>/dev/null || true
    else
        printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' \
            > "${ROOTFS}/etc/resolv.conf"
    fi
    log "rootfs ready at ${ROOTFS}"
    du -sh "${ROOTFS}" 2>/dev/null || true
}

main() {
    if stamp_ok; then
        log "rootfs already built (${ROOTFS}); set AGENT_FORCE_REBUILD=1 to redo"
        exit 0
    fi
    mkdir -p "$(dirname "${ROOTFS}")"
    ensure_root
    if [[ -d "${ROOTFS}" ]] && [[ "${FORCE_REBUILD}" == "1" ]]; then
        log "removing existing rootfs (force rebuild)"
        rm -rf "${ROOTFS}"
    fi
    mkdir -p "${ROOTFS}"
    if command -v mmdebstrap >/dev/null 2>&1; then
        bootstrap_with_mmdebstrap
    elif command -v debootstrap >/dev/null 2>&1; then
        bootstrap_with_debootstrap
    elif command -v docker >/dev/null 2>&1; then
        bootstrap_with_docker
    else
        die "need mmdebstrap, debootstrap or docker to build the rootfs.\n" \
            "    apt-get install -y mmdebstrap   # or debootstrap"
    fi
    post_install
    log "done."
}

main "$@"
