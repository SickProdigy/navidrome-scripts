#!/usr/bin/env python3
"""Update Navidrome track ratings for a specific artist.

This script targets a single artist and changes tracks with a given
rating (default from `.env` or CLI) to a new rating. It supports loading
defaults from a `.env` file and authenticates through Navidrome's
Subsonic-compatible API.

Usage examples:
  # Use values from .env (including ARTIST)
  python update-artist-track-ratings.py --env-file .env --dry-run

  # Override ARTIST from CLI
  python update-artist-track-ratings.py --env-file .env --artist "Zippy Kid" --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from dotenv import load_dotenv


for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

SUBSONIC_VERSION = "1.16.1"
CLIENT_NAME = "navidrome-scripts"
SCRIPT_VERSION = "v0.2.0"
REQUEST_TIMEOUTS = [10, 20, 30, 45, 60]
RETRY_DELAYS = [3, 5, 10, 20, 30]


class SafeAsciiFormatter(logging.Formatter):
    ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        rendered = self.ansi_escape.sub("", rendered)
        return rendered.encode("ascii", "backslashreplace").decode("ascii")


@dataclass(frozen=True)
class CandidateTrack:
    track_id: str
    title: str
    rating: int
    album_name: str = ""


def configure_logging(log_dir: str, log_level: str) -> Path:
    path = Path(log_dir)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    path.mkdir(parents=True, exist_ok=True)

    logfile = path / f"update-artist-track-ratings_{int(time.time())}.log"
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, log_level))

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(SafeAsciiFormatter("%(message)s"))
    root.addHandler(console_handler)

    file_handler = logging.FileHandler(logfile, "a", encoding="ascii", errors="backslashreplace")
    file_handler.setFormatter(SafeAsciiFormatter("[%(asctime)s] %(message)s"))
    root.addHandler(file_handler)
    return logfile


def encode_password(password: str) -> str:
    return "enc:" + password.encode("utf-8").hex()


def build_subsonic_params(
    username: Optional[str],
    password: Optional[str],
    token: Optional[str],
    no_auth: bool,
) -> Dict[str, str]:
    params = {"v": SUBSONIC_VERSION, "c": CLIENT_NAME, "f": "json"}
    if no_auth:
        return params

    if not username:
        return params

    params["u"] = username
    if password:
        params["p"] = encode_password(password)
    elif token:
        params["jwt"] = token
    return params


def as_list(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def format_connection_error(exc: Exception) -> str:
    text = str(exc)
    match = re.search(r"host='(?P<host>[^']+)'\s*,\s*port=(?P<port>\d+)", text)
    if match:
        hostport = f"{match.group('host')}:{match.group('port')}"
    else:
        hostport = "Navidrome"
    if "Read timed out" in text:
        return f"{hostport}; read timed out"
    return f"{hostport}; {text.split('.')[0][:120]}"


class NavidromeClient:
    def __init__(self, base_url: str, auth_params: Dict[str, str]) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_params = auth_params
        self.session = requests.Session()

    def request(self, endpoint: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/rest/{endpoint}.view"
        query = {**self.auth_params, **params}
        for attempt, delay in enumerate(RETRY_DELAYS, start=1):
            timeout = REQUEST_TIMEOUTS[min(attempt - 1, len(REQUEST_TIMEOUTS) - 1)]
            try:
                resp = self.session.get(url, params=query, timeout=timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= len(RETRY_DELAYS):
                    logging.error("GET %s failed after %s attempts: %s", endpoint, attempt, format_connection_error(exc))
                    return None
                logging.warning("GET %s connection issue: %s. Retrying in %ss...", endpoint, format_connection_error(exc), delay)
                time.sleep(delay)
                continue
            except requests.RequestException as exc:
                logging.error("GET %s failed: %s", endpoint, exc)
                return None

            if resp.status_code >= 500 and attempt < len(RETRY_DELAYS):
                logging.warning("GET %s returned HTTP %s. Retrying in %ss...", endpoint, resp.status_code, delay)
                time.sleep(delay)
                continue

            if resp.status_code != 200:
                logging.error("GET %s returned HTTP %s", endpoint, resp.status_code)
                return None

            try:
                data = resp.json()
            except ValueError:
                logging.error("GET %s did not return JSON", endpoint)
                return None

            wrapper = data.get("subsonic-response")
            if not isinstance(wrapper, dict):
                return data

            if wrapper.get("status") == "failed":
                err = wrapper.get("error") or {}
                logging.error("Subsonic error from %s: %s", endpoint, err.get("message", "unknown error"))
                return None

            return wrapper

        return None

    def get_artist(self, artist_id: str) -> Optional[Dict[str, Any]]:
        data = self.request("getArtist", {"id": artist_id})
        artist = data.get("artist") if data else None
        return artist if isinstance(artist, dict) else None

    def get_album(self, album_id: str) -> Optional[Dict[str, Any]]:
        data = self.request("getAlbum", {"id": album_id})
        album = data.get("album") if data else None
        return album if isinstance(album, dict) else None

    def search_songs(self, artist: str) -> List[Dict[str, Any]]:
        data = self.request("search2", {"query": artist})
        if not data:
            return []
        results = data.get("searchResult2")
        if not isinstance(results, dict):
            return []
        songs = as_list(results.get("song"))
        artist_lower = artist.casefold()
        filtered = [
            song for song in songs
            if str(song.get("artist", "")).casefold() == artist_lower
            or str(song.get("artistId", "")).casefold() == artist_lower
        ]
        return filtered or songs

    def set_rating(self, track_id: str, rating: int) -> bool:
        data = self.request("setRating", {"id": track_id, "rating": str(rating)})
        return bool(data and data.get("status") == "ok")


def track_candidate(track: Dict[str, Any], from_rating: int, album_name: str = "") -> Optional[CandidateTrack]:
    track_id = track.get("id") or track.get("songId") or track.get("song_id") or track.get("trackId")
    if not track_id:
        return None

    rating = None
    for key in ("rating", "stars", "ratingScore", "userRating"):
        if key not in track:
            continue
        try:
            rating = int(track[key])
        except (TypeError, ValueError):
            pass

    if rating != from_rating:
        return None

    title = track.get("title") or track.get("name") or "<unknown>"
    return CandidateTrack(str(track_id), str(title), rating, album_name)


def iter_artist_id_candidates(
    client: NavidromeClient,
    artist_id: str,
    from_rating: int,
    max_albums: Optional[int] = None,
) -> Iterable[CandidateTrack]:
    artist = client.get_artist(artist_id)
    if not artist:
        return

    albums = as_list(artist.get("album"))
    total_albums = len(albums)
    if max_albums is not None:
        albums = albums[:max_albums]
        logging.info("Testing first %s of %s album(s).", len(albums), total_albums)
    else:
        logging.info("Fetching tracks from %s album(s).", total_albums)

    for index, album in enumerate(albums, start=1):
        album_id = album.get("id")
        album_name = album.get("name") or album.get("title") or album_id or "<unknown>"
        if not album_id:
            continue
        logging.info("Album %s/%s: %s", index, len(albums), album_name)
        album_info = client.get_album(str(album_id))
        if not album_info:
            continue
        for song in as_list(album_info.get("song")):
            candidate = track_candidate(song, from_rating, str(album_name))
            if candidate:
                yield candidate


def iter_artist_name_candidates(
    client: NavidromeClient,
    artist: str,
    from_rating: int,
) -> Iterable[CandidateTrack]:
    logging.info("Searching songs for artist name: %s", artist)
    for song in client.search_songs(artist):
        candidate = track_candidate(song, from_rating)
        if candidate:
            yield candidate


def flush_batch(
    batch: List[CandidateTrack],
    client: NavidromeClient,
    to_rating: int,
    dry_run: bool,
    batch_number: int,
) -> tuple[int, int]:
    if not batch:
        return (0, 0)

    logging.info("Batch %s: %s track(s)", batch_number, len(batch))
    changed = 0
    failed = 0
    for track in batch:
        if dry_run:
            logging.info("Preview: %s -> %s | %s (%s)", track.rating, to_rating, track.title, track.track_id)
            continue

        ok = client.set_rating(track.track_id, to_rating)
        if ok:
            changed += 1
            logging.info("Updated: %s -> %s | %s (%s)", track.rating, to_rating, track.title, track.track_id)
        else:
            failed += 1
            logging.warning("Failed: %s -> %s | %s (%s)", track.rating, to_rating, track.title, track.track_id)

    batch.clear()
    return (changed, failed)


def read_int_env(name: str, default: str) -> int:
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Update Navidrome track ratings for a single artist")
    parser.add_argument("--env-file", help="Path to .env file to load defaults from")
    parser.add_argument("--base-url", help="Navidrome base URL, e.g. http://localhost:4533")
    parser.add_argument("--token", help="Subsonic JWT token if your Navidrome setup exposes one")
    parser.add_argument("--username", help="Username for Subsonic auth")
    parser.add_argument("--password", help="Password for Subsonic auth")
    parser.add_argument("--artist", help="Artist name to target (exact or partial). If omitted, reads ARTIST from .env")
    parser.add_argument("--artist-id", action="store_true", help="Treat the provided ARTIST value as an artist ID")
    parser.add_argument("--no-auth", action="store_true", help="Allow unauthenticated requests (may fail); only use if your server allows it")
    parser.add_argument("--from-rating", type=int, default=None, help="Only change tracks currently with this rating")
    parser.add_argument("--to-rating", type=int, default=None, help="New rating to apply")
    parser.add_argument("--dry-run", action="store_true", help="Don't perform updates, just print what would be changed")
    parser.add_argument("--max-albums", type=int, help="Only inspect the first N albums for a quick artist-id smoke test")
    parser.add_argument("--batch-size", type=int, default=100, help="Flush updates after this many matching tracks (default 100)")
    parser.add_argument("--log-dir", default="logs", help="Directory for timestamped run logs (default logs)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity")
    args = parser.parse_args(argv)

    if args.env_file:
        load_dotenv(args.env_file, override=True)
    else:
        load_dotenv(override=True)

    logfile = configure_logging(args.log_dir, args.log_level)
    start_time = time.time()
    logging.info("Version: update-artist-track-ratings %s", SCRIPT_VERSION)
    logging.info("Log file: %s", logfile)
    if args.dry_run:
        logging.info("Preview mode, no changes will be made.")

    base_url = args.base_url or os.getenv("BASE_URL")
    token = args.token or os.getenv("TOKEN") or os.getenv("API_TOKEN")
    username = args.username or os.getenv("USERNAME") or os.getenv("USER")
    password = args.password or os.getenv("PASSWORD") or os.getenv("PASS")
    artist = args.artist or os.getenv("ARTIST")
    try:
        from_rating = args.from_rating if args.from_rating is not None else read_int_env("FROM_RATING", "5")
        to_rating = args.to_rating if args.to_rating is not None else read_int_env("TO_RATING", "2")
    except ValueError as exc:
        logging.error("Error: %s", exc)
        return 2

    if not base_url:
        logging.error("Error: base URL not provided. Set --base-url or BASE_URL in a .env file.")
        return 2
    if not artist:
        logging.error("Error: artist not provided. Set --artist or ARTIST in .env.")
        return 2
    if args.batch_size <= 0:
        logging.error("Error: --batch-size must be greater than zero.")
        return 2
    if not args.no_auth and not username:
        logging.error("Error: username not provided. Set USERNAME, or pass --no-auth to attempt unauthenticated requests.")
        return 2
    if not args.no_auth and not (password or token):
        logging.error("Error: no authentication secret provided. Set PASSWORD or TOKEN, or pass --no-auth to attempt unauthenticated requests.")
        return 2

    auth_params = build_subsonic_params(username, password, token, args.no_auth)
    client = NavidromeClient(base_url, auth_params)
    prefers_id = args.artist_id or (isinstance(artist, str) and len(artist) >= 8 and " " not in artist)

    logging.info("Searching for tracks by artist: %s", artist)
    logging.info("Ratings: %s -> %s | batch-size:%s", from_rating, to_rating, args.batch_size)

    if prefers_id:
        candidates = iter_artist_id_candidates(client, artist, from_rating, max_albums=args.max_albums)
    else:
        candidates = iter_artist_name_candidates(client, artist, from_rating)

    batch: List[CandidateTrack] = []
    total_found = 0
    total_changed = 0
    total_failed = 0
    batch_number = 1

    for candidate in candidates:
        total_found += 1
        batch.append(candidate)
        if len(batch) >= args.batch_size:
            changed, failed = flush_batch(batch, client, to_rating, args.dry_run, batch_number)
            total_changed += changed
            total_failed += failed
            batch_number += 1

    if batch:
        changed, failed = flush_batch(batch, client, to_rating, args.dry_run, batch_number)
        total_changed += changed
        total_failed += failed

    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    action = "Previewed" if args.dry_run else "Updated"
    logging.info(
        "Done. Found: %s | %s: %s | Failed: %s | Time: %sm %ss | Log: %s",
        total_found,
        action,
        total_found if args.dry_run else total_changed,
        total_failed,
        minutes,
        seconds,
        logfile,
    )
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
