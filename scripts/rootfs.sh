#!/usr/bin/env bash
# Debian armhf root filesystem, as a reproducible tarball.
. /work/scripts/lib.sh

inputs=("$TOP/rootfs" "$TOP/board/boot" "$TOP/scripts/rootfs-customize.sh"
	"$BUILD/kernel-install" "$BUILD/artifacts/u-boot-rockchip.bin")
stage_done rootfs "${inputs[@]}" && { log "rootfs: up to date"; exit 0; }

# shellcheck source=../rootfs/packages.env
. "$TOP/rootfs/packages.env"

# armhf maintainer scripts run under qemu. The binfmt handler is global to
# the host kernel: register it only if missing, and remove it afterwards.
binfmt=/proc/sys/fs/binfmt_misc
registered=
cleanup() {
	if [ -n "$registered" ]; then
		echo -1 > "$binfmt/qemu-arm" || true
	fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM
[ -e "$binfmt/register" ] || mount -t binfmt_misc binfmt_misc "$binfmt"
if [ ! -e "$binfmt/qemu-arm" ]; then
	log "rootfs: registering qemu-arm binfmt handler (removed when done)"
	# F: the interpreter is opened now, so it also works inside the chroot
	echo ':qemu-arm:M::\x7fELF\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x28\x00:\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff:/usr/bin/qemu-arm:F' \
		> "$binfmt/register"
	registered=1
fi
arch-test armhf || die "cannot run armhf binaries"

rootfs="$BUILD/rootfs"
rm -rf "$rootfs"
snap=http://snapshot.debian.org/archive
log "rootfs: bootstrapping Debian $DEBIAN_SUITE from snapshot $DEBIAN_SNAPSHOT"
mmdebstrap --mode=root --variant=minbase --architectures=armhf \
	--include="$(echo $ROOTFS_PACKAGES | tr ' ' ',')" \
	--aptopt='Acquire::Check-Valid-Until "false"' \
	--aptopt='Acquire::Retries "5"' \
	--dpkgopt='path-exclude=/usr/share/doc/*' \
	--dpkgopt='path-include=/usr/share/doc/*/copyright' \
	--dpkgopt='path-exclude=/usr/share/man/*' \
	--dpkgopt='path-exclude=/usr/share/info/*' \
	--dpkgopt='path-exclude=/usr/share/locale/*' \
	--dpkgopt='path-include=/usr/share/locale/locale.alias' \
	--customize-hook="$TOP/scripts/rootfs-customize.sh \"\$1\"" \
	"$DEBIAN_SUITE" "$rootfs" \
	"deb $snap/debian/$DEBIAN_SNAPSHOT $DEBIAN_SUITE main non-free-firmware" \
	"deb $snap/debian/$DEBIAN_SNAPSHOT $DEBIAN_SUITE-updates main non-free-firmware" \
	"deb $snap/debian-security/$DEBIAN_SNAPSHOT $DEBIAN_SUITE-security main non-free-firmware"

# mmdebstrap puts the host's versions of these back after the hooks
echo respeaker > "$rootfs/etc/hostname"
rm -f "$rootfs/etc/resolv.conf"
ln -s /run/NetworkManager/resolv.conf "$rootfs/etc/resolv.conf"

log "rootfs: creating tarball"
# Sorted, numeric owners, every mtime clamped: same input, same bytes.
tar --sort=name --numeric-owner --mtime="@$SOURCE_DATE_EPOCH" --clamp-mtime \
	--pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime \
	--xattrs --xattrs-include='*' \
	-C "$rootfs" -cf "$BUILD/artifacts/rootfs.tar" .
rm -rf "$rootfs"
stage_mark rootfs "${inputs[@]}"
