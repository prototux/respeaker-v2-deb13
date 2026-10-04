#!/usr/bin/env bash
# Mainline U-Boot: TPL (DRAM init, no Rockchip blob) + SPL + OP-TEE + U-Boot,
# packed by binman into u-boot-rockchip.bin, to be written at sector 64.
. /work/scripts/lib.sh

inputs=("$TOP/board/u-boot" "$TOP/board/dts" "$BUILD/artifacts/tee.bin")
stage_done uboot "${inputs[@]}" && { log "uboot: up to date"; exit 0; }

tarball=$(fetch "$UBOOT_URL" "$UBOOT_SHA256")
src="$BUILD/u-boot"
unpack "$tarball" "$src"

# Same board DTS as Linux, plus the U-Boot-only bits (DRAM timings, SPL boot
# order) that U-Boot picks up automatically from <name>-u-boot.dtsi.
cp "$TOP/board/dts/$DTB_NAME.dts" "$src/dts/upstream/src/arm/rockchip/"
cp "$TOP/board/u-boot/$DTB_NAME-u-boot.dtsi" "$src/arch/arm/dts/"
dmc="$TOP/board/u-boot/dmc/ddr3-$DDR_FREQ.dtsi"
[ -f "$dmc" ] || die "no DRAM timings for DDR_FREQ=$DDR_FREQ (see board/u-boot/dmc/)"
cp "$dmc" "$src/arch/arm/dts/$DTB_NAME-dmc.dtsi"
log "uboot: DDR3 at $DDR_FREQ MHz"

for p in "$TOP"/board/u-boot/patches/*.patch; do
	[ -e "$p" ] || continue
	log "uboot: applying ${p##*/}"
	patch -d "$src" -p1 --no-backup-if-mismatch < "$p"
done

log "uboot: configuring"
make -C "$src" evb-rk3229_defconfig
(cd "$src" && scripts/kconfig/merge_config.sh -m .config "$TOP/board/u-boot/respeaker-core-v2.config")
make -C "$src" olddefconfig

# Make sure every requested option survived olddefconfig.
while IFS= read -r line; do
	case "$line" in ''|'#'*) continue ;; esac
	grep -qxF "$line" "$src/.config" || die "u-boot: '$line' was dropped by Kconfig"
done < "$TOP/board/u-boot/respeaker-core-v2.config"

log "uboot: building"
make -C "$src" -j"$JOBS" TEE="$BUILD/artifacts/tee.bin"

install -D -m 0644 "$src/u-boot-rockchip.bin" "$BUILD/artifacts/u-boot-rockchip.bin"
install -D -m 0755 "$src/tools/mkimage" "$BUILD/artifacts/mkimage"
stage_mark uboot "${inputs[@]}"
