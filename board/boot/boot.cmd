# Boot script for mainline U-Boot on the ReSpeaker Core v2.
#
# The kernel version, DTB and extra kernel arguments come from uEnv.txt,
# which the factory (vendor) U-Boot also reads, so the same files boot with
# either bootloader. After editing this file, regenerate boot.scr with:
#   mkimage -A arm -T script -C none -d /boot/boot.cmd /boot/boot.scr

setenv uname_r
setenv dtb
setenv cmdline
load ${devtype} ${devnum}:${distro_bootpart} ${scriptaddr} ${prefix}uEnv.txt
env import -t ${scriptaddr} ${filesize}

# The DTB numbers the MMC controllers identically for U-Boot and Linux
# (mmc0/mmcblk0 = eMMC, mmc1/mmcblk1 = SD), so the root device is the
# partition this script was loaded from, even when the SD card and the eMMC
# hold copies of the same image.
if test "${devtype}" = "mmc"; then
	setenv rootdev /dev/mmcblk${devnum}p${distro_bootpart}
else
	part uuid ${devtype} ${devnum}:${distro_bootpart} rootuuid
	setenv rootdev PARTUUID=${rootuuid}
fi

# "ro": systemd checks the root filesystem, then remounts it read-write
setenv bootargs "console=ttyS2,115200n8 root=${rootdev} ro rootfstype=ext4 ${cmdline}"
load ${devtype} ${devnum}:${distro_bootpart} ${kernel_addr_r} ${prefix}vmlinuz-${uname_r}
load ${devtype} ${devnum}:${distro_bootpart} ${fdt_addr_r} ${prefix}dtb/${uname_r}/${dtb}
bootz ${kernel_addr_r} - ${fdt_addr_r}
