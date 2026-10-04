# Common environment for the build stages. Sourced, never executed.
set -euo pipefail

TOP=/work
# shellcheck source=../config/versions.env
. "$TOP/config/versions.env"

BUILD="$TOP/build"
DL="$BUILD/dl"
OUT="$TOP/out"
JOBS="${JOBS:-2}"
DDR_FREQ="${DDR_FREQ_OVERRIDE:-$DDR_FREQ}"

BOARD=respeaker-core-v2
DTB_NAME=rk3229-respeaker-core-v2

export ARCH=arm
export CROSS_COMPILE=arm-linux-gnueabihf-
export SOURCE_DATE_EPOCH
export TZ=UTC LC_ALL=C.UTF-8
# Kernel build metadata that would otherwise leak the build host and time.
export KBUILD_BUILD_TIMESTAMP="$(date -u -d "@$SOURCE_DATE_EPOCH" '+%a %b %e %H:%M:%S UTC %Y')"
export KBUILD_BUILD_USER=respeaker
export KBUILD_BUILD_HOST=image-builder
export KBUILD_BUILD_VERSION=1
umask 022

mkdir -p "$BUILD/artifacts" "$DL" "$OUT"

log() { printf '\033[1;32m>>> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# fetch URL SHA256 -> prints the local path
fetch() {
	local url="$1" sha="$2" f="$DL/${1##*/}"
	if [ ! -f "$f" ] || ! echo "$sha  $f" | sha256sum -c --status; then
		log "downloading $url" >&2
		curl -fL --retry 5 -o "$f.part" "$url"
		echo "$sha  $f.part" | sha256sum -c --status || die "checksum mismatch for $url"
		mv "$f.part" "$f"
	fi
	echo "$f"
}

# git_fetch REPO COMMIT DEST: shallow checkout of exactly one commit
git_fetch() {
	local repo="$1" commit="$2" dest="$3"
	if [ "$(git -C "$dest" rev-parse HEAD 2>/dev/null)" != "$commit" ]; then
		log "fetching $repo @ $commit"
		rm -rf "$dest"
		git init -q "$dest"
		git -C "$dest" fetch -q --depth 1 "$repo" "$commit"
		git -C "$dest" -c advice.detachedHead=false checkout -q FETCH_HEAD
	fi
}

# unpack TARBALL DEST: fresh extraction (stripping the top-level directory)
unpack() {
	rm -rf "$2"
	mkdir -p "$2"
	tar -xf "$1" -C "$2" --strip-components=1
}

# A stage is redone when its stamp is missing or any of its inputs changed.
# stage_done NAME INPUT... / stage_mark NAME INPUT...
stage_hash() {
	local name="$1"; shift
	{ cat "$TOP/config/versions.env" "$TOP/scripts/lib.sh" "$TOP/scripts/$name.sh"
	  echo "DDR_FREQ=$DDR_FREQ"
	  if [ $# -gt 0 ]; then find "$@" -type f -print0 | sort -z | xargs -0 cat; fi; } | sha256sum | cut -d' ' -f1
}
# The hash is taken when the stage starts: inputs edited while it runs must
# not be recorded as built.
stage_done() {
	STAGE_HASH=$(stage_hash "$@")
	[ "$(cat "$BUILD/.stamp-$1" 2>/dev/null)" = "$STAGE_HASH" ]
}
stage_mark() { echo "$STAGE_HASH" > "$BUILD/.stamp-$1"; }
