# ReSpeaker Core v2 — Debian 13 image builder

**Just want to flash a board?** Follow [docs/HOWTO-FLASH.md](docs/HOWTO-FLASH.md).

Builds a bootable Debian 13 (trixie) image for the Seeed **ReSpeaker Core v2.0**
(Rockchip RK3229, 1 GiB DDR3, 4 GB eMMC, AP6212 Wi-Fi/BT, 2× AC108 ADCs) on a
current software stack:

| Component | Version | Notes |
|---|---|---|
| U-Boot | 2026.07 (mainline) | open-source TPL DRAM init, no Rockchip blobs |
| OP-TEE | 4.10.0 | secure monitor; Linux needs it for PSCI (SMP, reboot) |
| Linux | 6.18 LTS | mainline, plus the RK3228 audio codec driver from Armbian |
| AC108 driver | out-of-tree module | port of Seeed's original Core v2 driver, `drivers/ac108` |
| Debian | trixie | from a fixed snapshot.debian.org date |

The same image runs **live from an SD card** or can be **written to the eMMC**.

> **Status:** tested on one board: this image's own bootloader with the DRAM
> at 600 MHz (installed to the eMMC with `respeaker-install-emmc`), Wi-Fi,
> Bluetooth, HDMI (console and modes), the speaker output, the 8 capture
> channels (6 microphones + playback loopback), Ethernet, the USB host ports,
> the 12-LED ring, the status LEDs, the user button and CPU frequency
> scaling/temperature all work. Not tested yet:
> - the DRAM at 666 and 786 MHz (`DDR_FREQ`);
> - the USB serial console on the OTG port: the controller no longer hangs,
>   but in the one test so far the board did not show up on the PC (possibly
>   a charge-only cable or the PC port). Feedback welcome; useful details:
>   `cat /sys/class/extcon/*/state` and
>   `cat /sys/kernel/config/usb_gadget/respeaker/UDC` with the cable plugged
>   in, and `dmesg` from the moment it is plugged;
> - the Grove I2C port (I2C2): I have no Grove device to test it with. It
>   should work in theory: it is a standard Rockchip I2C controller, on the
>   same pins as in the vendor kernel, with a fixed bus number (`/dev/i2c-2`)
>   and the SoC's internal pull-ups enabled (the board has none on that
>   port). If you try it, feedback is welcome, whether it works or not:
>   `sudo i2cdetect -y 2` should show nothing with the port empty, and the
>   module's address with one plugged in.
>
> The serial console (UART header, 115200) shows each boot stage.

## Building

Requirements: Linux with Docker (or podman's docker shim), ~15 GB of disk
space, network access. Everything else runs in a pinned Debian container.

```sh
./build.sh
```

There is nothing to download by hand: the build fetches everything itself
and checks it against the hashes and commits pinned in
[`config/versions.env`](config/versions.env). That covers the container
image, the Linux, U-Boot and OP-TEE sources, a few Armbian kernel patches,
Seeed's Bluetooth firmware, and the Debian packages (from
snapshot.debian.org). Downloads are cached in `build/dl/`, so later builds
work from the cache.

The result is in `out/`:

```
respeaker-core-v2-debian-trixie-20261001.img      raw image (2 GiB)
respeaker-core-v2-debian-trixie-20261001.img.xz   compressed
respeaker-core-v2-debian-trixie-20261001.sha256
```

Options (environment variables):

| Variable | Default | |
|---|---|---|
| `BUILD_MEM` | half the RAM, at most `12g` | hard memory cap of the build container (no swap) |
| `JOBS` | derived from `BUILD_MEM` (~1 GiB per job) | parallel compile jobs |
| `IMAGE_SIZE` | `2G` | the root filesystem grows to the whole card/eMMC on first boot |
| `COMPRESS` | `1` | `0` skips the `.xz` |

The build runs in stages that are skipped when their inputs did not change:
`optee uboot kernel modules rootfs image`. Run some only with e.g.
`./build.sh kernel modules rootfs image`; `./build.sh clean` starts over
(downloads are kept); `./build.sh shell` opens a shell in the build container.

The memory cap matters on small machines: if a compile job ever exceeds it,
the kernel's OOM killer stops the build inside the container instead of
killing processes of your desktop session.

The container runs `--privileged`. That is needed to register the `qemu-arm`
binfmt handler (armhf package scripts are run under emulation while building
the rootfs) and for the chroot mounts. The handler is removed at the end if the
build added it.

### Reproducibility

Every input is pinned in [`config/versions.env`](config/versions.env): the
build container by digest, Debian packages by snapshot date, sources by
SHA-256 or commit. Every embedded timestamp is set to `SOURCE_DATE_EPOCH`, and
the identifiers (GPT GUIDs, filesystem UUID, password salt) are fixed. Two
builds of the same checkout produce the same `.img`: compare the `.sha256`
files.

To move to newer versions, edit `config/versions.env` (and the hashes).

## Flashing and booting

Write the image to an SD card (any size ≥ 2 GB):

```sh
xzcat out/respeaker-core-v2-*.img.xz | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress
```

or use balenaEtcher / Raspberry Pi Imager with the `.img.xz`.

Then check what the card actually holds. Cheap or worn cards can return
corrupted data without any error, and a corrupted kernel or root filesystem
shows up as random-looking crashes at boot:

```sh
img=$(ls out/respeaker-core-v2-*.img.xz)
sudo blockdev --flushbufs /dev/sdX   # read back from the card, not the page cache
xzcat "$img" | sudo cmp -n "$(xz --robot -l "$img" | awk '/^totals/{print $5}')" - /dev/sdX && echo OK
```

Etcher and Raspberry Pi Imager verify the card on their own.

**Booting from the SD card.** Insert the card and power on: the board boots
it whatever the eMMC holds (Seeed's factory system, this image, or nothing),
and nothing on the eMMC is changed. Only the bootloader that starts the card
differs, because the RK3229 boot ROM loads the *bootloader* from the eMMC
when there is one there:

- **Factory system on the eMMC:** Seeed's bootloader starts. It looks for
  `/boot/uEnv.txt` on the SD card first, which this image provides, so it
  boots the card's kernel and system. The DRAM then runs at the factory
  loader's speed, and the Rockchip TEE stays in use.
- **This image on the eMMC:** its bootloader starts and boots the SD card when
  one is inserted, the eMMC otherwise.
- **Empty eMMC:** the boot ROM loads this image's bootloader from the card
  itself.

The one case where the card is ignored is a broken bootloader on the eMMC;
see "Unbootable eMMC loader" below.

**Installing to the eMMC**, from the system booted off the SD card:

```sh
sudo respeaker-install-emmc                # copy the running system
sudo respeaker-install-emmc image.img.xz   # or write an image file
```

Then power off, remove the SD card and power on. This erases the factory
system. Seeed's flasher SD card can bring it back only while the factory
bootloader is still on the eMMC. Once this image's bootloader is installed,
it starts first and only boots cards that ship a `boot.scr` or
`extlinux.conf`. To go back, write Seeed's **sd** (not flasher) image to the
eMMC from this system instead (untested):
`sudo respeaker-install-emmc respeaker-debian-9-...-sd-...img.xz`.

Other ways to write the eMMC:

- From U-Boot's prompt (serial console, press a key during the 2 s countdown):
  `ums 0 mmc 0` exposes the eMMC as a USB drive on the OTG port, so you can `dd`
  the image from a PC. This is only available once this image's bootloader runs.
- Unbootable eMMC loader: the board has no recovery button, and the boot ROM
  always prefers a loader on the eMMC. Make the eMMC unreadable
  at power-on and the ROM falls back to the SD card. One way that worked: on
  the back (top right, inside the LED ring between LED 1 and MIC2) there is a
  row of eight small resistors, and just below it a small capacitor mounted
  parallel to them. Short that capacitor's end facing away from the resistor
  row to ground while powering on, then boot this image from SD and run
  `sudo respeaker-install-emmc` again (or `--bootloader` to rewrite only the
  bootloader).
  **At your own risk:** there is no schematic for this board, so what that
  pad actually shorts is unknown. Do it only during power-on, as briefly as
  possible.

## First login

- **Serial:** the UART header next to the speaker connector (3.3 V, 115200 8N1).
- **USB serial (untested, see the status note above):** plug the micro-USB
  OTG port into your PC, at any time: it should show up as a serial console
  (`/dev/ttyACM0`, 115200); press Enter to get the login prompt. The gadget is bound to the controller only while a PC is
  connected (the PHY is powered down otherwise, and the controller cannot
  start without it).
- **HDMI:** a login prompt on tty1.
- **Network:** Ethernet uses DHCP. Then `ssh respeaker@respeaker.local`.
- **Wi-Fi:** `nmtui`, or `nmcli dev wifi connect SSID password PASS` (no sudo
  needed for members of the `netdev` group, which includes the default user).

User **`respeaker`**, password **`respeaker`**. Change it with `passwd`,
especially before putting the board on a network you don't control. `root`
is locked; use `sudo`.

On the first boot the root filesystem grows to fill the SD card or eMMC, the
GPT identifiers are regenerated and SSH host keys are created.

## Hardware

| Feature | Driver | Status |
|---|---|---|
| 4× Cortex-A7, DVFS up to 1.2 GHz (vendor limit: 1.35 V) | cpufreq-dt, RK805 PMIC | mainline |
| eMMC, SD card | dw_mmc | mainline |
| Ethernet (100M, internal PHY) | stmmac | mainline |
| Wi-Fi AP6212 (BCM43430) | brcmfmac + firmware-brcm80211 | mainline |
| Bluetooth AP6212 | hci_bcm (serdev on UART1) + bluez-firmware | mainline |
| USB host ports | ehci / ohci | mainline |
| USB OTG port (serial console gadget) | dwc2 + Armbian patches | **untested**, feedback welcome |
| HDMI | rockchipdrm (+ power-domain fix, see below) | mainline |
| Mali-400 GPU | lima | mainline |
| Microphones (2× AC108, 8 ch) | `snd-soc-ac108` (built here) | ported driver |
| Headphone jack, speaker | `snd-soc-rk3228` (Armbian patch) | out of mainline |
| 12 RGB LEDs (APA102) | spidev, `/dev/spidev0.1` | userspace |
| 2 blue LEDs, user button | gpio-leds, gpio-keys | mainline |
| Grove port (I2C2, GPIO2_C4/C5) | i2c-rk3x, `/dev/i2c-2` (`sudo i2cdetect -y 2`) | enabled, **untested** |

The board device tree, [`board/dts/rk3229-respeaker-core-v2.dts`](board/dts/rk3229-respeaker-core-v2.dts),
was rewritten for mainline from the device tree of Seeed's Debian 9 image
(extracted from a running board). U-Boot and Linux use the same file.

### HDMI

Mainline `rk322x.dtsi` doesn't put the HDMI controller in the VIO power
domain, although its register clock comes from the VIO bus. Linux idles VIO
at boot, and the first HDMI register read then freezes the whole SoC when the
display driver loads, about 10 s into boot. The board DTS adds
`power-domains = <&power RK3228_PD_VIO>` to `&hdmi`, as mainline already does
for RK3288. If it ever hangs there again, add `modprobe.blacklist=rockchipdrm`
to `cmdline=` in `/boot/uEnv.txt` from another computer to get back in.

### DRAM speed

The bootloader's TPL brings up the DDR3 with open-source code. Mainline Linux
has no DDR frequency scaling for RK322x, so the rate set at boot is the rate
used for good. It is chosen with `DDR_FREQ` in `config/versions.env`:

| `DDR_FREQ` | Timings | Status |
|---|---|---|
| `300` | RK3229 EVB, upstream U-Boot | conservative fallback |
| **`600`** (default) | DDR3-1333, CL10 | the vendor kernel's normal rate; tested |
| `666` | DDR3-1333, CL10 | experimental |
| `786` | DDR3-1600, CL11 | experimental, the vendor's maximum |

The 600/666/786 timings live in `board/u-boot/dmc/`, together with a small
U-Boot patch adding those DPLL rates (`board/u-boot/patches/`). They were
computed with Rockchip's own DDR3 rules. The same method reproduces the EVB's
300 MHz values exactly and matches the mainline RK3288 boards at 666 MHz.
Above 600 MHz the vendor raises vdd_logic to 1.15 V, which the TPL cannot do,
hence "experimental".

To validate a frequency on your board, boot it and run e.g.
`sudo apt install memtester && sudo memtester 600M 5`. If it fails or the board
does not start, rebuild with `DDR_FREQ=300 ./build.sh`.

To check the rate in use: `sudo grep -E ' (dpll|ddrphy4x) ' /sys/kernel/debug/clk/clk_summary`.
`ddrphy4x` runs at twice the DRAM clock (1200000000 at 600 MHz).

Careful: while the factory system is still on the eMMC, the factory loader
starts the board. The DRAM then runs at the factory rate, and this image's
bootloader has not run yet. After `respeaker-install-emmc`, the first boot from
the eMMC is the first real test of these DRAM settings. If the board then stays
dead, boot from SD as described in "Other ways to write the eMMC" and run
`sudo respeaker-install-emmc --bootloader` from a `DDR_FREQ=300` image.

### Audio

The sound card keeps the vendor layout. The card ID is `seeed8micvoicec`, and
the mixer controls are `CH1..CH8 Capture Volume` (digital, per channel) and
`ADC1..ADC8 PGA Capture Volume` (analog gain):

- `hw:0,0`: capture, 8 channels: 6 microphones plus 2 loopback channels of
  the output (the AEC reference)
- `hw:0,1`: playback, stereo, to the headphone jack and speaker

Both share one I2S bus and must run at the same rate. `/etc/asound.conf` takes
care of that: the default ALSA device opens both at 48 kHz through
dmix/dsnoop, so several programs can play and record at once, at any rate and
format:

```sh
arecord -c 8 -r 16000 -f S16_LE mics.wav    # all 8 channels
aplay music.wav                             # headphone jack / speaker
alsamixer                                   # output volume (F3), mic gains (F4)
```

`Playback Volume` is the codec's output gain (−17.25 to +6 dB). The levels start at the values
Seeed's image shipped with (`/var/lib/alsa/asound.state`): microphones at +33 dB
digital gain and 0 dB analog, output at −1.5 dB. No PulseAudio/PipeWire is installed, and plain ALSA programs work.

### LED ring

The 12 APA102 LEDs are on `/dev/spidev0.1`. Their power switch is the GPIO line
named `LED_PWR_N` (gpiochip2 line 2, active low). Users in the `spi` and `gpio`
groups can drive both; the default user is in those groups.

## Known harmless log messages

These appear in `dmesg` or the journal on a working board. They are expected
and need no bug report.

| Message | Why it is harmless |
|---|---|
| `/cpus/cpu@f0x missing clock-frequency property` | Informational; the CPU clock comes from the clock driver. |
| `psci: PSCIv65535.65535 detected in firmware` | Only when booted by the factory eMMC loader: its Rockchip TEE does not answer the PSCI version query, but the calls Linux uses work. |
| `Fixed dependency cycle(s) with ...` (vop/hdmi, pmic/regulators) | The kernel resolving circular device-tree links, as designed. |
| `gpiochipN: Static allocation of GPIO base is deprecated` | Driver-internal deprecation notice. |
| `check access for rdinit=/init failed: -2, ignoring` | The initramfs is intentionally empty (the factory loader requires one); the kernel then mounts the root filesystem itself. |
| `rockchip-spi ...: Failed to request optional TX/RX DMA channel` | SPI falls back to PIO, plenty for the LED ring. |
| `dw-apb-uart 11020000.serial: failed to request DMA` | The Bluetooth UART runs without DMA. |
| `rockchip-thermal ...: Missing tshut-polarity property, using default (low)` | The hardware shutdown is set to reset the CRU, not to drive a pin, so the polarity is unused. |
| `dw_wdt ...: No valid TOPs array specified` | The watchdog uses its default timeout table. |
| `supply vbat/vddio not found` (hci_uart_bcm), `supply avdd-0v9/avdd-1v8 not found` (dwhdmi-rockchip), `supply vusb_d/vusb_a not found` (dwc2) | These rails are always on and not described in the device tree; dummy regulators are used. |
| `inno-hdmi-phy ...: error -ENXIO: IRQ index 0 not found` | That interrupt is optional and unused on RK3228. |
| `rk_gmac-dwmac ...: IRQ eth_wake_irq/sfty not found`, `Can not read property: tx_delay/rx_delay` | Optional interrupts; the delays only apply to RGMII, and the internal PHY uses RMII. |
| `snd_soc_ac108: loading out-of-tree module taints kernel` | The AC108 driver is built outside the kernel tree (`drivers/ac108`). |
| `brcmfmac ...: Direct firmware load for brcm/brcmfmac43430-sdio.seeed,respeaker-core-v2.bin failed with error -2` | The driver tries a board-specific firmware first, then loads the generic one (the next lines show the firmware version). |
| `brcmfmac: brcmf_c_process_txcap_blob: no txcap_blob available` | Optional file, not needed by this chip. |
| `rockchip-pm-domain ...: sync_state() pending due to 20020000.video-codec` (also `20030000.video-codec`, `20060000.rga`) | No driver is built for the video decoders and the 2D engine; their power domains stay off. |
| `random: ... uninitialized urandom read` | Early in boot, before the entropy pool is ready; gone a few seconds later (`crng init done`). |
| `systemd-journald.service: unit configures an IP firewall, but the local system does not support BPF/cgroup firewalling` | The kernel is built without BPF cgroup support; nothing on this image relies on it. |
| `GPT:Primary header thinks Alt. header is not at the end of the disk` | On the first boot only: the image is smaller than the card. The first boot then moves the GPT and grows the root filesystem. |
| `systemd-journald: ... corrupted or uncleanly shut down, renaming and replacing` | After a power cut: the journal starts a new file. Harmless, but shut down with `sudo poweroff` when you can. |

Messages that **do** indicate a problem: `EXT4-fs error`,
`dwc2_core_reset: HANG!`, `Bluetooth: hci0: command ... tx timeout`, any
`Oops`/`Kernel panic`, and `thermal ...: binding cdev ... failed`.

## Repository layout

```
build.sh                 host entry point (docker)
config/versions.env      every pinned input
docker/Dockerfile        build environment
scripts/                 build stages, run inside the container
board/dts/               board device tree (Linux + U-Boot)
board/u-boot/            U-Boot config fragment, DRAM timings
board/kernel/            kernel config fragment (+ patches/*.patch, applied in order)
board/boot/boot.cmd      U-Boot boot script
drivers/ac108/           AC108 ADC driver (out of tree)
rootfs/packages.env      Debian packages
rootfs/overlay/          files copied into the image
docs/HOWTO-FLASH.md      step-by-step guide for flashing a board
```

## License

GPL-2.0, see [LICENSE](LICENSE). The AC108 driver is derived from Seeed's
GPL-2.0 driver; the kernel and U-Boot patches follow their projects' licenses.
