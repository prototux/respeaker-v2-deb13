#!/usr/bin/env bash
# Out-of-tree drivers, built against the kernel from the "kernel" stage.
. /work/scripts/lib.sh

# The kernel stage wipes kernel-install/, so redo this one after every kernel
# build (its stamp changes each time), not only when the version changes.
inputs=("$TOP/drivers" "$BUILD/artifacts/kernelrelease" "$BUILD/.stamp-kernel")
stage_done modules "${inputs[@]}" && { log "modules: up to date"; exit 0; }

src="$BUILD/linux"
obj="$BUILD/linux-obj"
inst="$BUILD/kernel-install"
krel=$(cat "$BUILD/artifacts/kernelrelease")

for d in "$TOP"/drivers/*/; do
	name=$(basename "$d")
	log "modules: building $name"
	# Build in a copy so that the source tree stays clean.
	rm -rf "$BUILD/drivers/$name"
	mkdir -p "$BUILD/drivers"
	cp -r "$d" "$BUILD/drivers/$name"
	make -C "$src" O="$obj" M="$BUILD/drivers/$name" -j"$JOBS" modules
	make -C "$src" O="$obj" M="$BUILD/drivers/$name" \
		INSTALL_MOD_PATH="$inst" INSTALL_MOD_DIR=extra INSTALL_MOD_STRIP=1 \
		modules_install
done
ls "$inst/lib/modules/$krel/extra/"
stage_mark modules "${inputs[@]}"
