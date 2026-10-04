#!/usr/bin/env bash
# Runs inside the build container (see ../build.sh).
. /work/scripts/lib.sh

all=(optee uboot kernel modules rootfs image)
stages=("$@")
[ ${#stages[@]} -eq 0 ] && stages=("${all[@]}")

give_back() {
	# Hand the results back to the user who started the build.
	[ -n "${HOST_UID:-}" ] && chown -R "$HOST_UID:${HOST_GID:-$HOST_UID}" "$OUT" "$DL" 2>/dev/null || true
}
trap give_back EXIT

for s in "${stages[@]}"; do
	[ -x "$TOP/scripts/$s.sh" ] || die "unknown stage '$s' (stages: ${all[*]})"
	"$TOP/scripts/$s.sh"
done
