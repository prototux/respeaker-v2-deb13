#!/usr/bin/env bash
# Assemble the raw disk image. No loop devices, no mounts: the partition
# table is written by sfdisk on the file and the ext4 filesystem is created
# directly at its offset from the rootfs tarball.
#
# Layout (512-byte sectors), identical on SD card and eMMC:
#        0-33     GPT
#       64-32767  "loader": idbloader (TPL+SPL) at 64, U-Boot FIT at 16384
#   32768-end     "rootfs": ext4, label "rootfs"
. /work/scripts/lib.sh

# Always rebuilt: it only takes a minute.

# Fixed identifiers, so that the image is bit-for-bit reproducible. The first
# boot replaces the GPT ones (see rootfs/overlay/usr/lib/respeaker/firstboot).
DISK_GUID=52455350-4541-4b45-5232-000000000001
LOADER_UUID=52455350-4541-4b45-5232-000000000002
ROOT_PARTUUID=52455350-4541-4b45-5232-000000000003
ROOT_FS_UUID=52455350-4541-4b45-5232-000000000004
ROOT_HASH_SEED=52455350-4541-4b45-5232-000000000005

name="respeaker-core-v2-debian-$DEBIAN_SUITE-$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y%m%d)"
img="$OUT/$name.img"
rm -f "$img" "$img.xz" "$OUT/$name.sha256"

log "image: $img (${IMAGE_SIZE:-2G})"
truncate -s "${IMAGE_SIZE:-2G}" "$img"
sfdisk -q "$img" <<EOF
label: gpt
label-id: $DISK_GUID
first-lba: 64
start=64, size=32704, type=8DA63339-0007-60C0-C436-083AC8230908, uuid=$LOADER_UUID, name=loader
start=32768, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, uuid=$ROOT_PARTUUID, name=rootfs, attrs=LegacyBIOSBootable
EOF

dd if="$BUILD/artifacts/u-boot-rockchip.bin" of="$img" bs=512 seek=64 conv=notrunc status=none
uboot_end=$(( 64 + $(stat -c %s "$BUILD/artifacts/u-boot-rockchip.bin") / 512 ))
[ "$uboot_end" -le 32768 ] || die "bootloader overlaps the root partition"

read -r start sectors < <(partx -g -o START,SECTORS -n 2 "$img")

log "image: ext4 root filesystem, $((sectors / 2048)) MiB"
# No metadata_csum_seed/orphan_file: older bootloaders (the factory U-Boot
# 2017.09) may not read filesystems with features they do not know.
E2FSPROGS_FAKE_TIME="$SOURCE_DATE_EPOCH" mke2fs -q -F -t ext4 -L rootfs -m 1 \
	-O ^metadata_csum_seed,^orphan_file \
	-U "$ROOT_FS_UUID" \
	-E "hash_seed=$ROOT_HASH_SEED,root_owner=0:0,offset=$((start * 512))" \
	-d "$BUILD/artifacts/rootfs.tar" \
	"$img" "$((sectors / 2))k"

(cd "$OUT" && sha256sum "$name.img" > "$name.sha256")
if [ "${COMPRESS:-1}" = 1 ]; then
	log "image: compressing"
	# Fixed thread count and block size: same .xz whatever the host's CPU count
	xz -T4 --block-size=32MiB -6 -k "$img"
	(cd "$OUT" && sha256sum "$name.img.xz" >> "$name.sha256")
fi
cat "$OUT/$name.sha256"
