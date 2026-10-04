#!/usr/bin/env bash
# mmdebstrap customize hook: turns the bare Debian tree ($1) into the
# ReSpeaker image. Runs outside the chroot; anything that must run inside
# goes through "chr".
. /work/scripts/lib.sh

R="$1"
chr() { chroot "$R" "$@"; }
krel=$(cat "$BUILD/artifacts/kernelrelease")
inst="$BUILD/kernel-install"

log "rootfs: board files"
rsync -a --chown=root:root "$TOP/rootfs/overlay/" "$R/"
rm -f "$R/etc/apt/sources.list"

log "rootfs: kernel $krel"
mkdir -p "$R/usr/lib/modules"
cp -a "$inst/lib/modules/$krel" "$R/usr/lib/modules/"
# Every out-of-tree driver must be there (see drivers/)
for d in "$TOP"/drivers/*/; do
	ls "$R/usr/lib/modules/$krel/extra/" | grep -q "^snd-soc-$(basename "$d")\.ko" ||
		die "out-of-tree module for $(basename "$d") missing from the kernel install"
done
depmod -b "$R" "$krel"
# Layout understood by both bootloaders: the factory U-Boot (uEnv.txt,
# always loads an initrd) and ours (boot.scr, see board/boot/boot.cmd).
install -m 0644 "$inst/boot/zImage" "$R/boot/vmlinuz-$krel"
install -D -m 0644 "$inst/boot/dtbs/$DTB_NAME.dtb" "$R/boot/dtb/$krel/$DTB_NAME.dtb"
install -m 0644 "$inst/boot/config-$krel" "$inst/boot/System.map-$krel" "$R/boot/"
# Empty initramfs: the kernel finds no /init in it and mounts root= itself.
printf '' | cpio --quiet -o -H newc --reproducible | gzip -9n > "$R/boot/initrd.img-$krel"
cat > "$R/boot/uEnv.txt" <<EOF
uname_r=$krel
dtb=$DTB_NAME.dtb
cmdline=ro rootwait
EOF
install -m 0644 "$TOP/board/boot/boot.cmd" "$R/boot/boot.cmd"
"$BUILD/artifacts/mkimage" -A arm -T script -C none -n "ReSpeaker Core v2" \
	-d "$R/boot/boot.cmd" "$R/boot/boot.scr" >/dev/null

# The bootloader, for respeaker-install-emmc
install -D -m 0644 "$BUILD/artifacts/u-boot-rockchip.bin" \
	"$R/usr/lib/u-boot/respeaker-core-v2/u-boot-rockchip.bin"

log "rootfs: firmware"
# brcmfmac looks for brcmfmac43430-sdio.<board compatible>.txt first
fw="$R/usr/lib/firmware/brcm"
[ -f "$fw/brcmfmac43430-sdio.AP6212.txt" ] || die "AP6212 NVRAM file missing from firmware-brcm80211"
ln -sf brcmfmac43430-sdio.AP6212.txt "$fw/brcmfmac43430-sdio.seeed,respeaker-core-v2.txt"
# Bluetooth patch for the AP6212, from Seeed's image. The kernel tries
# brcm/BCM43430A1.<board compatible>.hcd before the generic (Raspberry Pi)
# BCM43430A1.hcd from bluez-firmware.
deb=$(fetch "$SEEED_RKFW_URL" "$SEEED_RKFW_SHA256")
rm -rf "$BUILD/rkfw" && mkdir -p "$BUILD/rkfw"
dpkg-deb -x "$deb" "$BUILD/rkfw"
hcd="$BUILD/rkfw/system/etc/firmware/bcm43438a1.hcd"
echo "$SEEED_BT_HCD_SHA256  $hcd" | sha256sum -c --status || die "unexpected bcm43438a1.hcd in $deb"
install -m 0644 "$hcd" "$fw/BCM43430A1.seeed,respeaker-core-v2.hcd"
rm -rf "$BUILD/rkfw"
# Debian defaults to its own re-signed regulatory.db; the kernel only trusts
# the upstream keys (sforshee, wens) built into it.
chr update-alternatives --set regulatory.db /lib/firmware/regulatory.db-upstream

log "rootfs: user"
# Default login respeaker / respeaker. The hash uses a fixed salt to keep the
# image reproducible.
for g in netdev plugdev bluetooth i2c spi gpio; do
	chr groupadd -f -r "$g"
done
chr useradd -m -s /bin/bash -G sudo,adm,audio,video,plugdev,netdev,bluetooth,dialout,i2c,spi,gpio respeaker
chr usermod -p '$6$respeakercorev2$QWEJ8mttK11491QIwJxN3jsHx1HKFFxIH6hnwv1dnltOhlqbyNCxUT3PDckM.dLHpmZnwGZKEtzsdVk558viB/' respeaker
chr passwd -q -l root
# Last password change: the build date, for every account. (0 would force a
# password change at the first login.)
day=$((SOURCE_DATE_EPOCH / 86400))
awk -F: -v OFS=: -v day="$day" '
	$3 != "" { $3 = day }
	{ print }' "$R/etc/shadow" > "$R/etc/shadow.new"
cat "$R/etc/shadow.new" > "$R/etc/shadow"
rm -f "$R/etc/shadow.new"

log "rootfs: services"
# USB serial console on the OTG port (see serial-getty@ttyGS0.service.d)
chr systemctl enable respeaker-firstboot.service respeaker-usb-gadget.service \
	respeaker-activity-led.service

log "rootfs: cleanup"
# Per-machine state is created on the first boot.
printf 'uninitialized\n' > "$R/etc/machine-id"
rm -f "$R"/etc/ssh/ssh_host_*
rm -f "$R"/etc/{passwd,group,shadow,gshadow,subuid,subgid}-
rm -rf "$R"/var/lib/apt/lists/* "$R"/var/cache/apt/*
rm -f "$R"/var/cache/debconf/*-old "$R"/var/lib/dpkg/*-old
rm -f "$R/var/cache/ldconfig/aux-cache" "$R/var/lib/systemd/catalog/database"
find "$R/var/log" -type f -delete

cat > "$R/etc/respeaker-build" <<EOF
debian=$DEBIAN_SUITE snapshot $DEBIAN_SNAPSHOT
linux=$LINUX_VERSION
u-boot=$UBOOT_VERSION
ddr-mhz=$DDR_FREQ
optee=$OPTEE_COMMIT
EOF
