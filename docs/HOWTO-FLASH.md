# Flashing Debian 13 on a ReSpeaker Core v2

This guide puts the Debian 13 image on a Seeed ReSpeaker Core v2.0, first on
an SD card, then optionally on the internal eMMC. No soldering or special
tools are needed for the normal path.

## What you need

- A ReSpeaker Core v2.0.
- A microSD card of 2 GB or more, from a known brand. Cheap or worn cards are
  the most common cause of strange crashes at boot.
- A 5 V / 2 A micro-USB power supply, plugged into the **PWR_IN** port.
- One way to log in:
  - an Ethernet cable on the same network as your computer (the simplest);
  - or an HDMI screen and a USB keyboard;
  - or a 3.3 V USB-UART adapter on the UART header (115200 8N1). This one
    also shows the boot messages, which helps if something goes wrong.

## 1. Get the image

Download `respeaker-core-v2-debian-trixie-*.img.xz` from the repository's
Releases page if one is published, or build it yourself on Linux with Docker:

```sh
git clone https://github.com/prototux/respeaker-v2-deb13.git
cd respeaker-v2-deb13
./build.sh
```

Nothing needs to be downloaded by hand: the build fetches the sources and
Debian packages itself and checks them against pinned hashes. The first
build takes a while (kernel, bootloader, Debian). The image ends up in
`out/`.

## 2. Write it to the SD card

**Windows / macOS / Linux:** open [balenaEtcher](https://etcher.balena.io/)
or Raspberry Pi Imager ("Use custom"), pick the `.img.xz` file and the SD
card, and write. Both check the card after writing.

**Linux command line:** find the card with `lsblk` (here `/dev/sdX`; double
check, everything on it is erased), then:

```sh
img=respeaker-core-v2-debian-trixie-20261001.img.xz
xzcat "$img" | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress
# check what the card really holds:
sudo blockdev --flushbufs /dev/sdX
xzcat "$img" | sudo cmp -n "$(xz --robot -l "$img" | awk '/^totals/{print $5}')" - /dev/sdX && echo OK
```

If the check doesn't print `OK`, write the card again or use another card.

## 3. Boot from the SD card

1. Insert the SD card, connect Ethernet / HDMI / serial, then plug the power
   supply into **PWR_IN**.
2. The board boots the SD card on its own, whatever is on the eMMC: Seeed's
   factory system, a previous install of this image, or nothing. Nothing on
   the eMMC is changed at this point.
3. The first boot takes a bit longer: the system grows to fill the card and
   creates its SSH keys.

## 4. Log in

| How | What to do |
|---|---|
| Ethernet | `ssh respeaker@respeaker.local` (or the board's IP from your router). The image includes an SSH server (OpenSSH), enabled by default. |
| HDMI | a login prompt appears on the screen |
| Serial | open the adapter's port at 115200, press Enter |

User **`respeaker`**, password **`respeaker`**. Change it right away:

```sh
passwd
```

Connect to Wi-Fi with `nmtui` (menu: *Activate a connection*), or:

```sh
nmcli dev wifi connect "MySSID" password "MyPassword"
```

Quick checks that everything works:

```sh
aplay /usr/share/sounds/alsa/Front_Center.wav    # speaker / headphone jack
arecord -c 8 -r 16000 -f S16_LE -d 5 test.wav   # the 6 microphones (+2 loopback)
bluetoothctl --timeout 10 scan on               # Bluetooth
```

You can stop here and keep running from the SD card.

## 5. (Optional) Install to the eMMC

This copies the running SD card system to the internal eMMC, so the board
works without the SD card.

> **This erases the eMMC, including Seeed's factory Debian 9.** Back up
> anything you need from it first.

From the system booted off the SD card:

```sh
sudo respeaker-install-emmc
```

Type `yes` when asked. When it says `done`:

```sh
sudo poweroff
```

Remove the SD card and power the board again. It now boots from the eMMC with
this image's own bootloader, which also runs the memory at 600 MHz instead of
the 300 MHz the factory bootloader leaves it at.

## Updating to a newer image

Your settings and files are **not** kept: back them up first.

1. Write the new image to an SD card (step 2) and insert it.
2. Power on. The installed bootloader prefers the SD card when one is present,
   so the new image starts.
3. Run `sudo respeaker-install-emmc`, power off, remove the card, power on.

To update **only the bootloader** and keep the installed system:

```sh
sudo respeaker-install-emmc --bootloader
```

## Troubleshooting

**It crashes or panics at a different point on each boot, or reports
`EXT4-fs error`.** The SD card most likely holds corrupted data. Check it as
in step 2, rewrite it, or try another card.

**Nothing on the screen and no network.** Use the serial console: it shows
every boot stage, from the first bootloader lines to the login prompt. When
asking for help, include that log.

**The board doesn't start at all after installing to the eMMC.** The boot
ROM always prefers the eMMC, so a broken bootloader there hides the SD card.
The way out is to make the eMMC unreadable for a moment at power-on, so the
board falls back to the SD card; see "Unbootable eMMC loader" in the
[README](../README.md#flashing-and-booting). That involves shorting a pad on
the board, **at your own risk**.

**Going back to Seeed's factory system.** See the note under "Installing to
the eMMC" in the [README](../README.md#flashing-and-booting) (untested).

**Messages in `dmesg` that look like errors.** Many are expected; check the
list in [Known harmless log messages](../README.md#known-harmless-log-messages)
before opening an issue.
