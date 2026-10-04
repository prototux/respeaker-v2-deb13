#!/usr/bin/env bash
# Mainline Linux: zImage, the board DTB and modules.
. /work/scripts/lib.sh

inputs=("$TOP/board/kernel" "$TOP/board/dts")
stage_done kernel "${inputs[@]}" && { log "kernel: up to date"; exit 0; }

tarball=$(fetch "$LINUX_URL" "$LINUX_SHA256")
src="$BUILD/linux"
obj="$BUILD/linux-obj"
inst="$BUILD/kernel-install"
unpack "$tarball" "$src"
rm -rf "$obj" "$inst"
mkdir -p "$obj"

while read -r name sha; do
	[ -n "$name" ] || continue
	p=$(fetch "https://raw.githubusercontent.com/armbian/build/$ARMBIAN_COMMIT/$ARMBIAN_PATCH_DIR/$name" "$sha")
	log "kernel: applying Armbian $name"
	filterdiff -x '*/rk322x.dtsi' "$p" | patch -d "$src" -p1 --no-backup-if-mismatch
done <<< "$ARMBIAN_KERNEL_PATCHES"

for p in "$TOP"/board/kernel/patches/*.patch; do
	[ -e "$p" ] || continue
	log "kernel: applying ${p##*/}"
	patch -d "$src" -p1 --no-backup-if-mismatch < "$p"
done

cp "$TOP/board/dts/$DTB_NAME.dts" "$src/arch/arm/boot/dts/rockchip/"
echo "dtb-\$(CONFIG_ARCH_ROCKCHIP) += $DTB_NAME.dtb" >> "$src/arch/arm/boot/dts/rockchip/Makefile"

kmake() { make -C "$src" O="$obj" -j"$JOBS" "$@"; }

log "kernel: configuring"
kmake multi_v7_defconfig
# multi_v7 supports dozens of SoC families; keep only Rockchip, which drops a
# lot of irrelevant drivers. Hidden symbols are simply re-selected by Kconfig.
for sym in $(sed -n 's/^CONFIG_ARCH_\([A-Z0-9_]*\)=y$/\1/p' "$obj/.config"); do
	case "$sym" in
		ROCKCHIP|MULTIPLATFORM|MULTI_V7|MULTI_V6_V7) ;;
		*) "$src/scripts/config" --file "$obj/.config" --disable "ARCH_$sym" ;;
	esac
done
"$src/scripts/kconfig/merge_config.sh" -m -O "$obj" "$obj/.config" "$TOP/board/kernel/respeaker-core-v2.config"
kmake olddefconfig

while IFS= read -r line; do
	case "$line" in ''|'#'*) continue ;; esac
	grep -qxF "$line" "$obj/.config" || die "kernel: '$line' was dropped by Kconfig"
done < "$TOP/board/kernel/respeaker-core-v2.config"

log "kernel: building with $JOBS jobs"
kmake zImage modules "rockchip/$DTB_NAME.dtb"

krel=$(kmake -s kernelrelease)
kmake INSTALL_MOD_PATH="$inst" INSTALL_MOD_STRIP=1 modules_install
rm -f "$inst/lib/modules/$krel/build" "$inst/lib/modules/$krel/source"
install -D -m 0644 "$obj/arch/arm/boot/zImage" "$inst/boot/zImage"
install -D -m 0644 "$obj/arch/arm/boot/dts/rockchip/$DTB_NAME.dtb" "$inst/boot/dtbs/$DTB_NAME.dtb"
install -D -m 0644 "$obj/.config" "$inst/boot/config-$krel"
install -D -m 0644 "$obj/System.map" "$inst/boot/System.map-$krel"
echo "$krel" > "$BUILD/artifacts/kernelrelease"
stage_mark kernel "${inputs[@]}"
