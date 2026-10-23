# home-movie-collage

Turn a folder of home movies into a random, heavily zoomed, silent video collage.

It picks short clips at random from your videos (longer videos come up more often, and the same
video never twice in a row), zooms deep into a fixed spot of each, sometimes speeds clips up,
slows them down or rotates them, and joins them with hard cuts or crossfades. Every run is
different, unless you re-run with the same `--seed`, sources and options, which reproduces it.

It handles footage from phones and camcorders: iPhone HDR is tone-mapped to normal video,
interlaced camcorder footage is deinterlaced, and portrait or anamorphic videos come out the
right way up and the right shape.

## Install

You need **Python 3.9 or newer**, **ffmpeg 7 or newer** (which does the video work) and
**pipx** (which installs the command). Type the commands below into a terminal: *Terminal* on
macOS, *PowerShell* on Windows, or your terminal app on Linux.

**1. ffmpeg**

| System | Command |
|---|---|
| macOS | `brew install ffmpeg` (needs [Homebrew](https://brew.sh)) |
| Windows | `winget install Gyan.FFmpeg`, then open a new terminal |
| Linux | see below |

On Linux, your distribution's ffmpeg works if it is version 7 or newer and includes the
libx264 encoder (and libvpx-vp9 for `.webm`), as on Arch or on Fedora with RPM Fusion. Ubuntu
22.04 and 24.04 and Debian 12 ship older versions, so there use
[Homebrew on Linux](https://docs.brew.sh/Homebrew-on-Linux) (`brew install ffmpeg`) or a
[static build](https://ffmpeg.org/download.html#build-linux) placed on your `PATH`.

Check with `ffmpeg -version`. The command also checks when it starts and tells you if
something is missing or too old.

**2. Python and pipx**

| System | Command |
|---|---|
| macOS | `brew install pipx` (brings Python with it) |
| Windows | `winget install Python.Python.3.12`, open a new terminal, then `py -m pip install --user pipx` |
| Linux | `sudo apt install pipx` (or your package manager's pipx) |

Then run `pipx ensurepath` (on Windows, `py -m pipx ensurepath`) and open a new terminal.

**3. home-movie-collage**

```
pipx install home-movie-collage
```

To update later: `pipx upgrade home-movie-collage`. To remove it: `pipx uninstall
home-movie-collage`. `home-movie-collage --version` shows which version you have.

If you already work in Python virtual environments, `pip install home-movie-collage` inside one
works too. A plain `pip install` outside one is refused on many systems (an
"externally-managed-environment" error), which is why pipx is the recommended way. To try the
newest unreleased changes instead:
`pipx install --force https://github.com/williamthazard/home-movie-collage/archive/refs/heads/main.zip`.

## Use

```
home-movie-collage ~/Movies/home-videos
```

On Windows, a folder looks like `C:\Users\you\Videos\Family`. Tip: drag a folder onto the
terminal window to type its path for you.

That writes `collage.mp4` in the current folder, about a minute long, plus
`collage.mp4.plan.json`, which lists every shot (source file, start time, zoom, speed and so
on). Some examples:

```
# two minutes, a named output file
home-movie-collage ~/Movies/home-videos -d 120 -o summer.mp4

# square, for posting; or portrait
home-movie-collage ~/Movies/home-videos --size square -o square.mp4
home-movie-collage ~/Movies/home-videos --size portrait -o portrait.mp4

# every clip 2 s long at 3x zoom, all joined with half-second crossfades
home-movie-collage ~/Movies/home-videos --clip 2 --zoom 3 --fade 0.5 --fade-prob 1

# several folders, and a WebM file for the web
home-movie-collage ~/Movies/2019 ~/Movies/2020 -o both-years.webm

# the same collage again (the seed is printed on every run)
home-movie-collage ~/Movies/home-videos --seed 1234
```

`home-movie-collage --help` lists every option. The main ones:

| Option | Default | What it does |
|---|---|---|
| `-d SECONDS` | 60 | target length; the collage runs up to one clip longer |
| `-o FILE` | `collage.mp4` | output; ends in `.mp4`, `.mov`, `.m4v`, `.mkv`, `.avi`, `.ts` or `.webm` |
| `--size` | `landscape` | `landscape` (1280x720), `square` (720x720), `portrait` (720x1280) or any `WxH` |
| `--clip-min`, `--clip-max` / `--clip` | 1.5 to 6 s | clip length range, or one fixed length |
| `--zoom-min`, `--zoom-max` / `--zoom` | 2.5x to 7x | zoom range, or one fixed zoom |
| `--fade-prob` | 0.35 | chance a clip crossfades in rather than cutting |
| `--fade-min`, `--fade-max` / `--fade` | 0.4 to 2.5 s | crossfade length range, or one fixed length; clips too short for the shortest fade get a hard cut |
| `--speed-prob` | 0.4 | chance a clip is sped up or slowed down (0.33x to 3x) |
| `--rotate-prob` | 0.4 | chance a clip is rotated (up to 180 degrees either way) |
| `--seed N` | random | reproduce an earlier collage |
| `-j N` | 2 | clips rendered at once; more is faster but can make the computer sluggish |

## Good to know

- **Getting videos out of Apple Photos:** select them, then *File → Export → Export Unmodified
  Originals*. That keeps full quality and HDR; the plain *Export* re-encodes them.
- **iCloud:** videos that are only in iCloud (not downloaded to the Mac) are skipped, and the
  run says how many. Right-click the folder in Finder and choose *Keep Downloaded* to avoid it.
- **It never overwrites your videos.** `-o` only replaces an earlier collage (one with a
  `.plan.json` beside it), and earlier collages in a source folder are not used as sources.
- **It is silent:** the collage has no audio.
- **Speed:** two clips render at a time, which keeps the computer usable meanwhile; `-j 4` or
  more is faster but can make it sluggish, especially with 4K footage. HDR clips take 3 to 4
  times longer than normal ones. What it learns about each video is cached (in
  `~/Library/Caches/home-movie-collage` on macOS, `~/.cache/home-movie-collage` on Linux, or
  `%LOCALAPPDATA%\home-movie-collage` on Windows), so later runs start faster; it is safe to
  delete.
- **Very long collages:** up to about 2000 clips work on macOS, and about 1100 on Windows.
- **Tested on** macOS with ffmpeg 9.0 and Python 3.9 to 3.15. Windows and Linux should work but
  have not been tested yet; if something goes wrong, please open an issue.

## License

MIT; see [LICENSE](https://github.com/williamthazard/home-movie-collage/blob/main/LICENSE).
