# Examples

Two small web apps that show off the board's LED ring and microphone array.
Each is a single Python file using only the standard library, with a built-in
web server; open it from any browser on your network.

| Example | What it does | Port |
|---|---|---|
| [`ringstudio.py`](ringstudio.py) | Drives the 12-LED ring: 90+ patterns (Alexa, Google, spinners, fire, clock...) with a live preview and color/speed/brightness controls | 8080 |
| [`micview.py`](micview.py) | Shows the 6 microphones live on a top view of the board: levels, oscilloscopes, direction of the loudest sound, listening to any channel in the browser, and playing dropped audio files on the board's speaker | 8081 |

## Setup

On the board, install Python and the SPI bindings:

```sh
sudo apt install python3 python3-spidev
```

Then copy the examples to the board, from your computer:

```sh
scp examples/*.py respeaker@respeaker.local:
```

Or download them on the board directly:

```sh
curl -O https://raw.githubusercontent.com/prototux/respeaker-v2-deb13/main/examples/ringstudio.py
curl -O https://raw.githubusercontent.com/prototux/respeaker-v2-deb13/main/examples/micview.py
```

The default `respeaker` user can already use the LEDs (groups `spi` and
`gpio`) and the sound card (group `audio`), so no sudo is needed to run them.

## Ring Studio

```sh
python3 ringstudio.py
```

Open `http://respeaker.local:8080` and click a pattern. The program switches
the LED power on when it starts and off when it stops, so stop it with
Ctrl+C to turn the ring off.

Useful options:

- `--pattern NAME` starts with a pattern already running, e.g.
  `--pattern rainbow`.
- `--offset N` and `--reverse` remap the LEDs if your ring is mounted rotated
  or mirrored. The "Orientation" test pattern shows which LED is which.
- `--sim` runs without the LEDs, with only the browser preview. It works on
  any computer, which is handy for designing patterns.

## Mic View

```sh
python3 micview.py
```

Open `http://respeaker.local:8081` and talk or clap around the board:

- the microphone pads light up with their level, and the arrow points at
  the loudest sound;
- **Listen** on a channel, or a click on a microphone, plays it in your
  browser. Two selected channels play as left and right;
- dropping an audio file on the page plays it on the board's speaker or
  headphone jack. The browser decodes the file, so any format it supports
  works.

It records through the `mics` ALSA device (`/etc/asound.conf`). Other
programs can still record and play at the same time.

Useful options: `--rate 48000` for full-bandwidth listening (the default
16000 Hz is enough for speech), and `--sim` for a synthetic talker circling
the board, no sound card needed.

## Security note

Both servers accept connections from your whole network without any
password. **Anyone who can reach the board can listen to its microphones**
with Mic View, or drive the LEDs. On a network you don't fully trust, make
them listen locally only and use an SSH tunnel:

```sh
python3 micview.py --host 127.0.0.1                  # on the board
ssh -L 8081:localhost:8081 respeaker@respeaker.local   # on your computer
```

Then open `http://localhost:8081`.
