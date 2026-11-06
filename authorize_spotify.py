"""One-time Spotify login helper. Run this once per spotify_config.json you set up.

Usage:
  1. Fill in client_id and client_secret in spotify_config.json (from your app at
     https://developer.spotify.com/dashboard - redirect URI must be exactly
     http://127.0.0.1:8888/callback).

  2. python authorize_spotify.py --url
     Open the printed link in a browser signed into the Spotify Premium account you want
     this to use, and click Allow. It will redirect to a 127.0.0.1:8888 page that fails to
     load - that's expected, nothing needs to be listening there.

  3. Copy the full address-bar URL from that failed page (it has ?code=... on the end).

  4. python authorize_spotify.py --code "<paste the url or just the code here>"
     This exchanges it for an access/refresh token pair and saves the refresh_token into
     spotify_config.json. badges.py takes it from there - refreshing hourly on its own, so
     this is a one-time step, not something you repeat.
"""
import argparse
import json
import os
import re
import sys
from urllib.parse import urlencode

import requests

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spotify_config.json")
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state user-modify-playback-state"


def load_config():
    if not os.path.exists(CONFIG_FILE):
        sys.exit(f"{CONFIG_FILE} not found - copy spotify_config.example.json first.")
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    if "your-spotify-app" in cfg.get("client_id", ""):
        sys.exit(f"Fill in the real client_id/client_secret in {CONFIG_FILE} first.")
    return cfg


def print_authorize_url(cfg):
    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
    }
    print("Open this URL, sign in, and click Allow:\n")
    print("https://accounts.spotify.com/authorize?" + urlencode(params))


def extract_code(raw):
    match = re.search(r"[?&]code=([^&\s]+)", raw)
    return match.group(1) if match else raw.strip()


def exchange_code(cfg, raw_code_or_url):
    code = extract_code(raw_code_or_url)
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI},
        auth=(cfg["client_id"], cfg["client_secret"]),
    )
    if not r.ok:
        sys.exit(f"Token exchange failed ({r.status_code}): {r.text}\n"
                  "Codes are single-use and expire quickly - get a fresh one with --url if this is stale.")
    tok = r.json()
    cfg["refresh_token"] = tok["refresh_token"]
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"Saved refresh_token to {CONFIG_FILE}. badges.py is ready to use Spotify now.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", action="store_true", help="print the authorize URL to open in a browser")
    group.add_argument("--code", metavar="CODE_OR_URL", help="the redirected URL (or just its code=... value)")
    args = parser.parse_args()

    cfg = load_config()
    if args.url:
        print_authorize_url(cfg)
    else:
        exchange_code(cfg, args.code)


if __name__ == "__main__":
    main()
