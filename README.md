# Escape Room Badge Scanner

A Raspberry Pi-controlled puzzle for a home escape room: four RFID "badges" hidden
around the room each unlock a themed sound clip when placed on their matching scanner,
and placing all four at once triggers a finale song and opens a real garage door via
Hubitat. Built for a kid's birthday party.

## How it works

Four RC522 RFID readers share a single SPI bus on the Pi. Each reader's RST pin doubles
as an on/off switch - only one reader is ever powered on at a time, so the script cycles
through them (power one on, read it, power it off, move to the next) fast enough that it
feels simultaneous. See the comment block above `SCANNERS` in `badges.py` for the exact
pin mapping.

Each scanner is assigned one badge (a themed RFID tag):

| Scanner | Clip | Starts at | Plays for |
|---|---|---|---|
| 1 | Everything Is AWESOME!!! (Tegan and Sara, *The Lego Movie*) | 0:00 | 8s |
| 2 | kuromi! (prtcle) | 0:00 | 10s |
| 3 | Gotta Catch 'Em All! (*Pokemon Ultimate Journeys*) | 0:00 | 10s |
| 4 | Never Say Never (Justin Bieber, Jaden) | 0:00 | 10s |

Placing a badge on its correct scanner plays that scanner's clip through whatever
Chromecast-enabled speaker/display is currently connected to the Spotify account (see
[Spotify setup](#spotify-setup) - it's not hardcoded to one device by name). Whichever
badge happens to be placed *last* - completing the full set of four - skips its own clip
and goes straight to the finale instead, so the two songs never collide. Removing a badge
and placing all four correctly again re-triggers everything, restarting from scratch.

The finale (all four correct at once) opens the garage via a Hubitat Maker API call and
plays "We R Who We R" (Kesha) on the same speaker at a flat volume.

Scanner 3 also accepts a second, physically-duplicate badge purely as an end-to-end test
tool: tap it to confirm the whole chain (scanner -> Spotify -> speaker) is alive without
disturbing the real badge hidden in the room. See `target_uids` in `badges.py`.

## Hardware

- Raspberry Pi (tested on Raspberry Pi OS, desktop image)
- 4x MFRC522 RFID reader modules, sharing one SPI bus (CE0), each with its own RST pin
- 4+ RFID tags (one per scanner, plus the Scanner 3 test duplicate)
- A Hubitat hub with a relay device wired to the garage door opener, exposed via the Maker
  API
- A Chromecast-enabled speaker or display (e.g. Google Home/Nest) on the same WiFi as
  whatever device logs into Spotify
- Spotify Premium (required - the Web API can't drive playback on a free account)

## Setup

```bash
python3 -m venv nfc
source nfc/bin/activate
pip install -r requirements.txt
```

Enable SPI on the Pi (`sudo raspi-config` -> Interface Options -> SPI) if it isn't
already.

### Config files

Two config files hold real credentials and are gitignored - copy the `.example.json`
versions and fill them in:

```bash
cp spotify_config.example.json spotify_config.json
cp hubitat_config.example.json hubitat_config.json
chmod 600 spotify_config.json hubitat_config.json
```

**Hubitat**: `url` is your Maker API device endpoint
(`http://<hub-ip>/apps/api/<app-id>/devices/<device-id>/on`), `access_token` is the
Maker API app's access token. Both come from the Maker API app's config page on the hub.

### Spotify setup

1. Create an app at the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
   Redirect URI must be exactly `http://127.0.0.1:8888/callback`. Check "Web API" under
   APIs used.
2. Put the app's Client ID and Client Secret into `spotify_config.json`.
3. Run the one-time login helper:
   ```bash
   python authorize_spotify.py --url
   ```
   Open the printed link in a browser signed into the Premium account you want this to
   use, click Allow, then copy the resulting (broken) `127.0.0.1:8888/callback?code=...`
   URL from the address bar.
4. ```bash
   python authorize_spotify.py --code "<paste the url here>"
   ```
   This saves a refresh token into `spotify_config.json`. It renews itself from here on -
   you only do this once per Spotify app.

### Assigning badges

Each scanner's accepted tag ID(s) live in `SCANNERS` in `badges.py`. To find a new tag's
ID, run:

```bash
python badges.py --scan
```

and hold each tag to its scanner - the ID prints to the console. Put that number in the
matching scanner's `target_uids` list.

## Running it

```bash
python badges.py            # normal operation
python badges.py --check    # confirms all 4 readers respond on their SPI/RST wiring
python badges.py --scan     # prints tag IDs as you hold them to a scanner (no actions fire)
```

`testscanners.py` is a deeper diagnostic tool for wiring problems - isolation and
wake-alone tests, live per-scanner read-rate stats, and a `--find-leak` mode for tracking
down a scanner whose RST line isn't truly holding it in reset (a real bug this project
hit once). See its own docstring for usage.

### Running as a service

```bash
sudo cp badges.service /etc/systemd/system/badges.service
sudo systemctl daemon-reload
sudo systemctl enable --now badges.service
```

Adjust `User=` and the paths in `ExecStart=`/`WorkingDirectory=` in `badges.service`
first if your username or install path differs from `rob` / `/home/rob/Piproject`.

To stop it running at boot (e.g. between parties) without removing anything:

```bash
sudo systemctl disable --now badges.service
```

and bring it back later with `sudo systemctl enable --now badges.service`.

Logs: `sudo journalctl -u badges.service -f`

## Known quirks (learned the hard way)

- **RFID range is short and placement-sensitive.** A tag that reads reliably in one spot
  can silently stop reading after being shifted by a few millimeters. If a scanner seems
  dead, try the tag flat against the reader before assuming it's a wiring problem.
- **Spotify only shows a Cast speaker/display as a controllable device if something has
  connected to it recently** (opened Spotify on a phone and selected it, or similar). If
  it's sat idle a while, a clip/finale call will log "no speaker/display currently
  connected" and skip - reconnect from a phone and it reappears. Do this as a final step
  right before the room gets used, not hours ahead.
- **Only one thing can ever play on the target speaker at once.** A new clip or the
  finale always takes over immediately from whatever was playing - there's no queue.
- **Don't give a `.service` unit `After=graphical.target` if it's also
  `WantedBy=multi-user.target`.** graphical.target is ordered after multi-user.target, so
  that combination creates a dependency cycle; systemd silently deletes the service's
  start job every boot rather than erroring, which looks exactly like "it's enabled but
  just doesn't start." This project hit that bug for real - `badges.service` here uses
  `After=network-online.target` instead.
- **A Pi without a battery-backed RTC can log confusingly early timestamps** right after
  boot, before NTP has corrected the clock - a target or service can appear to have
  "started" earlier than the boot itself. Harmless, just confusing when reading logs.
- Two scripts can't read SPI/GPIO at once - stop `badges.service` before running
  `badges.py --scan`/`--check` or `testscanners.py` manually, or they'll fight each other.
