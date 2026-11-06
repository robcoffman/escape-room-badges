import json
import os
import shutil
import subprocess
import sys
import threading
import time

import RPi.GPIO as GPIO
import requests
from mfrc522 import MFRC522

# Called once when ALL scanners hold their correct tag at the same time. URL + token live
# in a gitignored config file (same pattern as spotify_config.json) so the real access
# token never ends up in source control.
HUBITAT_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hubitat_config.json")

# Plays whenever a scanner sees its own correct badge (not for a wrong/unrecognized tag).
# Path is relative to this file, so it works no matter what directory the script is run from.
SOUND_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "success.mp3")
# Tried in order; first one installed wins. Both play headless and exit on their own.
SOUND_PLAYERS = [
    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
    ["cvlc", "--play-and-exit", "--quiet"],
]
_sound_player = next((p for p in SOUND_PLAYERS if shutil.which(p[0])), None)


def play_success_sound():
    """Fire-and-forget: starts playback in the background and never blocks the scan loop.

    Currently unused by the live code path (per-badge sounds moved to Spotify clips), kept
    around as a no-dependency fallback if Spotify ever needs to be ripped out again.
    """
    if _sound_player is None:
        print(f"No audio player found (tried {', '.join(p[0] for p in SOUND_PLAYERS)}) - sound not played.")
        return
    if not os.path.exists(SOUND_FILE):
        print(f"Sound file not found: {SOUND_FILE}")
        return
    try:
        subprocess.Popen(_sound_player + [SOUND_FILE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        print(f"Failed to play sound: {e}")


# Played once, from the beginning, on the finale (all four scanners correct at once).
# Also fires again on a repeat win (badge removed and correctly re-placed later) - each
# call always starts the track over from 0:00, it's never resumed from where it left off.
SPOTIFY_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spotify_config.json")
SPOTIFY_TRACK_URI = "spotify:track:3LUWWox8YYykohBbHUrrxd"  # Kesha - We R Who We R
SPOTIFY_FINALE_VOLUME = 70

# Device types that control playback rather than receiving it - never a valid target, even
# if Spotify briefly reports one of these as "active".
SPOTIFY_CONTROLLER_TYPES = {"Smartphone", "Computer", "Tablet"}


def _spotify_access_token(cfg):
    """Trade the saved refresh_token for a fresh access_token (tokens expire hourly).

    Spotify occasionally rotates the refresh_token when this is used; if so, the new one
    is saved back to disk so the next call keeps working.
    """
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "refresh_token", "refresh_token": cfg["refresh_token"]},
        auth=(cfg["client_id"], cfg["client_secret"]),
        timeout=10,
    )
    r.raise_for_status()
    tok = r.json()
    if "refresh_token" in tok:
        cfg["refresh_token"] = tok["refresh_token"]
        with open(SPOTIFY_CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    return tok["access_token"]


def _find_spotify_device(access_token):
    """Target whichever Cast speaker/display is currently connected - Robs Speaker, the
    living room display, or any other Google Home - rather than one hardcoded name, so it
    doesn't matter which one gets used on the day.

    Looks this up fresh every time rather than trusting a saved device_id: Cast devices can
    drop out of Spotify's device list between sessions (seen firsthand while testing this),
    so a cached ID can go stale. Requires that a phone has connected to it recently enough
    for Spotify to still see it - if nobody's opened Spotify near it in a while, it may not
    show up here at all.
    """
    r = requests.get(
        "https://api.spotify.com/v1/me/player/devices",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    r.raise_for_status()
    devices = r.json().get("devices", [])
    # "active" = whatever Spotify is currently connected to playing on, i.e. whichever
    # speaker/display a phone most recently joined. Controller devices (the phone itself)
    # are excluded so a clip never accidentally comes out of someone's pocket instead.
    eligible = [d for d in devices if d.get("type") not in SPOTIFY_CONTROLLER_TYPES]
    return next((d for d in eligible if d.get("is_active")), None)


def _set_spotify_volume(access_token, device_id, percent):
    requests.put(
        "https://api.spotify.com/v1/me/player/volume",
        params={"volume_percent": percent, "device_id": device_id},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )


# Token and device lookup are cached because per-badge clips can fire several times a
# minute (unlike the once-per-game finale), and both would otherwise add a network round
# trip on every single badge placement. The device is still re-checked periodically in
# case the speaker drops off Spotify's radar (seen happen firsthand while testing this).
_token_cache = {"access_token": None, "expires_at": 0}
_device_cache = {"device": None, "checked_at": 0}
DEVICE_CACHE_SECONDS = 30


def _cached_access_token():
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"]:
        return _token_cache["access_token"]
    with open(SPOTIFY_CONFIG_FILE) as f:
        cfg = json.load(f)
    token = _spotify_access_token(cfg)
    _token_cache["access_token"] = token
    _token_cache["expires_at"] = now + 3300  # a little under Spotify's real 1hr expiry
    return token


def _cached_device(access_token, force_refresh=False):
    now = time.time()
    if not force_refresh and _device_cache["device"] and now - _device_cache["checked_at"] < DEVICE_CACHE_SECONDS:
        return _device_cache["device"]
    device = _find_spotify_device(access_token)
    _device_cache["device"] = device
    _device_cache["checked_at"] = now
    return device


# Only one thing can ever play on the speaker at once. Every new play - a badge clip or
# the finale - cancels whatever stop-timer a previous clip left pending, so an old clip's
# timeout can never reach out and pause something newer that has since taken over the
# speaker (e.g. a short badge clip's timer firing after the finale has already started).
_pending_stop_timer = None


def _cancel_pending_stop():
    global _pending_stop_timer
    if _pending_stop_timer is not None:
        _pending_stop_timer.cancel()
        _pending_stop_timer = None


def _schedule_stop(access_token, device_id, delay_seconds):
    global _pending_stop_timer
    _cancel_pending_stop()

    def stop():
        try:
            requests.put(
                "https://api.spotify.com/v1/me/player/pause",
                params={"device_id": device_id},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
        except requests.exceptions.RequestException as e:
            print(f"Spotify: failed to stop clip: {e}")

    _pending_stop_timer = threading.Timer(delay_seconds, stop)
    _pending_stop_timer.start()


# One short clip per badge, always played from position_ms on placement. Skipped entirely
# for whichever badge happens to be the one that completes the full set (see
# would_complete_set below) so it never overlaps the finale song that follows it.
BADGE_CLIPS = {
    "Scanner 1": {"track_uri": "spotify:track:58vnVBWfKWIjSVvKTZJly2", "position_ms": 0, "duration": 8},   # Everything Is AWESOME!!! (Tegan and Sara / Lego Movie)
    "Scanner 2": {"track_uri": "spotify:track:0n4IFugtLhQpW9I0laMSbp", "position_ms": 0, "duration": 10},  # kuromi! (prtcle)
    "Scanner 3": {"track_uri": "spotify:track:0A613Z27yuowHxj24sawIy", "position_ms": 0, "duration": 10},  # Gotta Catch 'Em All! (Pokemon Ultimate Journeys)
    "Scanner 4": {"track_uri": "spotify:track:5GYbkDveRD2I8M5ZJ14hWn", "position_ms": 0, "duration": 10},  # Never Say Never (Justin Bieber, Jaden)
}
BADGE_CLIP_VOLUME = 70


def would_complete_set(scanner):
    """True if every OTHER scanner already holds its correct tag right now.

    Placing this scanner's correct tag would then finish the full set, so its own clip
    should be skipped in favor of jumping straight to the finale.
    """
    return all(other["last_id"] in other["target_uids"] for other in SCANNERS if other is not scanner)


def play_badge_clip(scanner):
    """Fire-and-forget: the timed stop happens on a background timer, never blocking
    the scan loop."""
    clip = BADGE_CLIPS.get(scanner["name"])
    if clip is None:
        return
    if not os.path.exists(SPOTIFY_CONFIG_FILE):
        print(f"Spotify config not found - skipping {scanner['name']}'s clip.")
        return
    try:
        access_token = _cached_access_token()
        device = _cached_device(access_token)
        if device is None:
            print(f"Spotify: no speaker/display currently connected - skipping {scanner['name']}'s clip.")
            return

        _set_spotify_volume(access_token, device["id"], BADGE_CLIP_VOLUME)
        requests.put(
            "https://api.spotify.com/v1/me/player/play",
            params={"device_id": device["id"]},
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            data=json.dumps({"uris": [clip["track_uri"]], "position_ms": clip["position_ms"]}),
            timeout=10,
        ).raise_for_status()
        print(f"Spotify: playing {scanner['name']}'s clip on {device['name']}.")
        _schedule_stop(access_token, device["id"], clip["duration"])
    except requests.exceptions.RequestException as e:
        print(f"Spotify: {e} - skipping {scanner['name']}'s clip.")


def play_finale_song():
    """Fire-and-forget from the caller's point of view: the volume raise happens on a
    background timer so this never blocks the scanning loop."""
    if not os.path.exists(SPOTIFY_CONFIG_FILE):
        print(f"Spotify config not found: {SPOTIFY_CONFIG_FILE} - skipping finale song.")
        return
    try:
        access_token = _cached_access_token()
        # Forced fresh (not the cache badge clips use) since this is rare enough that it's
        # worth the extra latency to be sure the speaker is really still there.
        device = _cached_device(access_token, force_refresh=True)
        if device is None:
            print("Spotify: no speaker/display currently connected (needs to have been "
                  "connected to recently) - skipping finale song.")
            return

        _set_spotify_volume(access_token, device["id"], SPOTIFY_FINALE_VOLUME)
        requests.put(
            "https://api.spotify.com/v1/me/player/play",
            params={"device_id": device["id"]},
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            data=json.dumps({"uris": [SPOTIFY_TRACK_URI]}),
            timeout=10,
        ).raise_for_status()
        print(f"Spotify: playing on {device['name']} at {SPOTIFY_FINALE_VOLUME}%.")
        # A badge clip's own stop-timer could still be pending from moments ago; kill it so
        # it can't reach out and pause this song partway through.
        _cancel_pending_stop()
    except requests.exceptions.RequestException as e:
        print(f"Spotify: {e} - skipping finale song.")


# Both scanners share one SPI bus (SCK/MOSI/MISO/SDA on CE0), so only one can talk at a time.
# Each scanner's RST pin works as an on/off switch: RST low = chip powered down and silent,
# RST high = chip active. The loop switches one scanner on, reads it, then switches to the other.
#
#   rst          - RST pin, physical (BOARD) numbering:
#                  22 = BCM 25, 18 = BCM 24, 16 = BCM 23, 15 = BCM 22
#   target_uids  - tags that count on this scanner ([] until set; run --scan to get IDs).
#                  Normally one real badge, but a scanner can accept more than one - e.g.
#                  Scanner 3 also has a duplicate badge kept aside purely for end-to-end
#                  testing (confirms the system's on and sound works) without disturbing
#                  the real hidden badge.
# The garage opens only when EVERY scanner holds one of its own target tags.
#
# Scanner 1 and Scanner 2 are swapped from the original wiring order (2026-09-26): the
# physical mounting positions changed, so what was wired as "Scanner 2" (RST pin 18) is now
# called Scanner 1, and what was "Scanner 1" (RST pin 22) is now Scanner 2. Pins 16/15 for
# Scanner 3/4 are unchanged.
SCANNERS = [
    {"name": "Scanner 1", "rst": 18, "target_uids": [584188857438]},
    {"name": "Scanner 2", "rst": 22, "target_uids": [584188922975]},
    {"name": "Scanner 3", "rst": 16, "target_uids": [584189054049, 584189185123]},  # 2nd id = test duplicate
    {"name": "Scanner 4", "rst": 15, "target_uids": [584189119586]},
]

BUS_DEVICE = 0        # SPI chip select shared by both scanners (CE0)
SETTLE_SECONDS = 0.05  # let a chip wake up after RST goes high
FIELD_SECONDS = 0.03   # let a tag power up after the antenna switches on
READ_TRIES = 3         # attempts per scanner per pass before counting it as empty
POLL_SECONDS = 0.1
MISS_LIMIT = 3         # consecutive empty reads before a tag counts as removed


def setup():
    """Put both scanners in reset, then return the shared reader."""
    GPIO.setmode(GPIO.BOARD)
    for scanner in SCANNERS:
        GPIO.setup(scanner["rst"], GPIO.OUT, initial=GPIO.LOW)
    return MFRC522(device=BUS_DEVICE, pin_rst=SCANNERS[0]["rst"])


def select(reader, scanner):
    """Power down every other scanner and wake this one.

    A chip that has been held in reset loses its setup (antenna off), so it has to be
    re-initialised every time it is switched on.
    """
    for other in SCANNERS:
        if other is not scanner:
            GPIO.output(other["rst"], GPIO.LOW)
    GPIO.output(scanner["rst"], GPIO.HIGH)
    time.sleep(SETTLE_SECONDS)
    reader.MFRC522_Init()
    time.sleep(FIELD_SECONDS)


def read_uid(reader):
    for _ in range(READ_TRIES):
        status, _ = reader.MFRC522_Request(reader.PICC_REQIDL)
        if status == reader.MI_OK:
            status, uid = reader.MFRC522_Anticoll()
            if status == reader.MI_OK:
                n = 0
                for byte in uid[:5]:
                    n = n * 256 + byte
                return n
        time.sleep(0.01)
    return None


def version(reader):
    return reader.Read_MFRC522(reader.VersionReg)


def owner_of(uid):
    """Name of the scanner this tag belongs on, or None if it isn't one of ours."""
    return next((s["name"] for s in SCANNERS if uid in s["target_uids"]), None)


def log_placement(scanner, uid):
    name = scanner["name"]
    print(f"[{name}] Tag detected! ID: {uid}")
    if uid in scanner["target_uids"]:
        print(f"[{name}] Correct tag for this scanner.")
        if would_complete_set(scanner):
            print(f"[{name}] That completes the set - skipping its clip, going straight to the finale.")
        else:
            play_badge_clip(scanner)
    elif owner_of(uid):
        print(f"[{name}] Wrong scanner - that tag belongs on {owner_of(uid)}.")
    else:
        print(f"[{name}] Unrecognized tag.")


def problem_with(scanner):
    """Why this scanner isn't satisfied, or None if it holds its correct tag."""
    uid = scanner["last_id"]
    name = scanner["name"]
    if not scanner["target_uids"]:
        return f"{name} has no target tag configured (run --scan to get its ID)"
    if uid is None:
        return f"{name} has no tag"
    if uid in scanner["target_uids"]:
        return None
    if owner_of(uid):
        return f"{name} has the tag for {owner_of(uid)}"
    return f"{name} has an unrecognized tag ({uid})"


def open_garage():
    try:
        with open(HUBITAT_CONFIG_FILE) as f:
            cfg = json.load(f)
        url = f"{cfg['url']}?access_token={cfg['access_token']}"
    except (OSError, json.JSONDecodeError, KeyError) as e:
        print(f"Hubitat config error ({HUBITAT_CONFIG_FILE}): {e}")
        return
    try:
        response = requests.get(url, timeout=5)
        print(f"Hubitat response: {response.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"Failed to reach Hubitat: {e}")


def check_combination(state):
    """Open the garage and play the finale song once when every scanner holds its correct
    tag; log why not otherwise.

    Logs only when the situation changes. After firing, it won't fire again until at
    least one tag has been removed and all four become correct again - at which point it
    fires again, restarting the song from the beginning.
    """
    problems = [p for p in map(problem_with, SCANNERS) if p]
    status = "; ".join(problems)

    if status != state["status"]:
        state["status"] = status
        if problems:
            print(f"Not opening the garage: {status}")

    if problems:
        state["opened"] = False
    elif not state["opened"]:
        state["opened"] = True
        print("All correct tags in place! Triggering Hubitat...")
        open_garage()
        play_finale_song()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    reader = setup()

    if mode == "--check":
        for scanner in SCANNERS:
            select(reader, scanner)
            v = version(reader)
            status = "OK" if v not in (0x00, 0xFF) else "NOT RESPONDING - check wiring"
            print(f"[{scanner['name']}] RST pin {scanner['rst']}: version {hex(v)} ({status})")
        return

    if mode == "--scan":
        print("Scan mode: hold tags to each scanner to see their IDs (Ctrl+C to quit).")
    else:
        print("Ready. The garage opens only when all scanners hold their correct tags.")

    for scanner in SCANNERS:
        scanner["last_id"] = None
        scanner["misses"] = 0
    state = {"status": None, "opened": False}

    while True:
        for scanner in SCANNERS:
            select(reader, scanner)
            uid = read_uid(reader)
            if uid:
                scanner["misses"] = 0
                if uid != scanner["last_id"]:
                    scanner["last_id"] = uid
                    if mode == "--scan":
                        print(f"[{scanner['name']}] Tag ID: {uid}")
                    else:
                        log_placement(scanner, uid)
            else:
                scanner["misses"] += 1
                if scanner["misses"] >= MISS_LIMIT and scanner["last_id"] is not None:
                    scanner["last_id"] = None
                    print(f"[{scanner['name']}] Tag removed.")

        if mode != "--scan":
            check_combination(state)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        GPIO.cleanup()
