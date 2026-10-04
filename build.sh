#!/usr/bin/env bash
# Build a ReSpeaker Core v2 (RK3229) Debian image.
#
# Runs everything inside a pinned Debian container, so the only host
# requirements are bash and docker (or podman with its docker shim).
#
#   ./build.sh                 build everything (incremental)
#   ./build.sh clean           remove build/ (keeps downloads)
#   ./build.sh shell           open a shell in the build container
#   ./build.sh <stage>...      run some stages only: optee uboot kernel modules rootfs image
#
# Environment knobs:
#   BUILD_MEM=8g    memory cap of the build container (default: half of RAM, max 12g)
#   JOBS=N          parallel compile jobs (default: derived from BUILD_MEM)
#   IMAGE_SIZE=2G   size of the raw image (it grows to the whole disk on first boot)
#   COMPRESS=0      skip the .img.xz
#   DDR_FREQ=300    DRAM clock in MHz (default in config/versions.env)
set -euo pipefail

TOP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config/versions.env
. "$TOP/config/versions.env"

DOCKER="${DOCKER:-docker}"
command -v "$DOCKER" >/dev/null || { echo "error: $DOCKER not found" >&2; exit 1; }

if [ "${1:-}" = clean ]; then
	# build/ is created by root inside the container
	"$DOCKER" run --rm -v "$TOP:/work" "$BUILDER_IMAGE" \
		find /work/build -mindepth 1 -maxdepth 1 ! -name dl -exec rm -rf {} +
	exit 0
fi

# Keep the host safe from the OOM killer: the container gets a hard memory
# cap with no swap, and the job count is derived from that cap (~1 GiB/job,
# which leaves plenty of room for the kernel's final link).
mem_total_mib=$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo)
if [ -z "${BUILD_MEM:-}" ]; then
	mib=$((mem_total_mib / 2))
	[ "$mib" -gt 12288 ] && mib=12288
	[ "$mib" -lt 2048 ] && mib=2048
	BUILD_MEM="${mib}m"
fi
case "$BUILD_MEM" in
	*g|*G) mem_mib=$(( ${BUILD_MEM%[gG]} * 1024 )) ;;
	*m|*M) mem_mib=${BUILD_MEM%[mM]} ;;
	*) echo "error: BUILD_MEM must end in m or g" >&2; exit 1 ;;
esac
if [ -z "${JOBS:-}" ]; then
	JOBS=$(( mem_mib / 1024 - 1 ))
	cpus=$(nproc)
	[ "$JOBS" -gt "$cpus" ] && JOBS=$cpus
	[ "$JOBS" -lt 1 ] && JOBS=1
fi

# The builder image is tagged by the hash of its inputs, so editing the
# Dockerfile or the pins rebuilds it and nothing else does.
tag_hash=$( { cat "$TOP/docker/Dockerfile"; echo "$BUILDER_IMAGE $DEBIAN_SNAPSHOT $DEBIAN_SUITE"; } | sha256sum | cut -c1-12)
BUILDER_TAG="respeaker-corev2-builder:$tag_hash"
if ! "$DOCKER" image inspect "$BUILDER_TAG" >/dev/null 2>&1; then
	echo ">>> building container $BUILDER_TAG"
	"$DOCKER" build \
		--build-arg BUILDER_IMAGE="$BUILDER_IMAGE" \
		--build-arg DEBIAN_SNAPSHOT="$DEBIAN_SNAPSHOT" \
		--build-arg DEBIAN_SUITE="$DEBIAN_SUITE" \
		-t "$BUILDER_TAG" "$TOP/docker"
fi

tty_flags=()
[ -t 0 ] && [ -t 1 ] && tty_flags=(-it)

cmd=(/work/scripts/build-all.sh "$@")
[ "${1:-}" = shell ] && cmd=(bash)

echo ">>> memory cap $BUILD_MEM, $JOBS jobs"
# --privileged is needed for two things only: registering the qemu-arm
# binfmt handler (to run armhf maintainer scripts while building the rootfs)
# and the chroot mounts done by mmdebstrap.
exec "$DOCKER" run --rm "${tty_flags[@]}" --privileged \
	--memory "$BUILD_MEM" --memory-swap "$BUILD_MEM" \
	-e JOBS="$JOBS" \
	-e DDR_FREQ_OVERRIDE="${DDR_FREQ:-}" \
	-e IMAGE_SIZE="${IMAGE_SIZE:-2G}" \
	-e COMPRESS="${COMPRESS:-1}" \
	-e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
	-v "$TOP:/work" -w /work \
	"$BUILDER_TAG" "${cmd[@]}"
