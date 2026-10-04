#!/usr/bin/env bash
# OP-TEE OS: the RK3229 secure monitor. Linux needs it for PSCI (bringing up
# the 3 secondary cores, reboot and poweroff).
. /work/scripts/lib.sh

stage_done optee && { log "optee: up to date"; exit 0; }

src="$BUILD/optee_os"
git_fetch "$OPTEE_REPO" "$OPTEE_COMMIT" "$src"

log "optee: building"
rm -rf "$BUILD/optee-out"
# Early console on UART2 (the debug header) at the same rate as U-Boot and
# Linux. Secure DRAM: 0x68400000-0x686fffff, reserved in the board DTS.
make -C "$src" -j"$JOBS" O="$BUILD/optee-out" \
	CROSS_COMPILE=arm-linux-gnueabihf- \
	CROSS_COMPILE_core=arm-linux-gnueabihf- \
	CROSS_COMPILE_ta_arm32=arm-linux-gnueabihf- \
	PLATFORM=rockchip-rk322x \
	CFG_ARM32_core=y \
	CFG_EARLY_CONSOLE_BAUDRATE=115200 \
	CFG_TEE_CORE_LOG_LEVEL=1 \
	CFG_TEE_TA_LOG_LEVEL=0 \
	CFG_WERROR=n \
	all

install -D -m 0644 "$BUILD/optee-out/core/tee-raw.bin" "$BUILD/artifacts/tee.bin"
stage_mark optee
