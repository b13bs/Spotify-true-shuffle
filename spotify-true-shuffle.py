#!/usr/bin/env python3
"""Create Spotify playlists from liked songs in true random order."""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import datetime
import http.server
import json
import logging
import os
import re
import secrets
import socketserver
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import Any


APP_NAME = "Spotify True Shuffle"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"
AUTH_TIMEOUT_SECONDS = 180
CACHE_FILE_NAME = ".spotify-true-shuffle-token.json"
LOG_FILE_NAME = "spotify-true-shuffle.log"
PLAYLIST_MARKER = "True random"
PLAYLIST_PREFIX = "\U0001f3b2 True random"
SCOPES = (
    "user-library-read",
    "playlist-modify-public",
)
API_BASE = "https://api.spotify.com/v1"
ACCOUNTS_BASE = "https://accounts.spotify.com"


class SpotifyTrueShuffleError(RuntimeError):
    """Expected runtime failure with a user-readable message."""


@dataclasses.dataclass
class Config:
    action: str
    client_id: str
    client_secret: str
    redirect_uri: str
    cache_file: Path
    log_file: Path
    log_level: str
    quiet: bool
    auth_timeout_seconds: int


class QuietAwareLogger:
    def __init__(self, logger: logging.Logger, quiet: bool) -> None:
        self.logger = logger
        self.quiet = quiet

    def user(self, message: str, *args: Any) -> None:
        self.logger.info(message, *args)
        if not self.quiet:
            print(message % args if args else message)


def parse_args() -> Config:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        prog="spotify-true-shuffle.py",
        description="Create a Spotify playlist from all liked songs in random order.",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=("create", "list"),
        help="Action to run.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="Logging verbosity written to the log file.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stdout output for unattended cron runs.",
    )

    args = parser.parse_args()

    return Config(
        action=args.action,
        client_id=os.environ.get("SPOTIFY_CLIENT_ID", ""),
        client_secret=os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
        redirect_uri=DEFAULT_REDIRECT_URI,
        cache_file=(script_dir / CACHE_FILE_NAME).resolve(),
        log_file=(script_dir / LOG_FILE_NAME).resolve(),
        log_level=args.log_level,
        quiet=args.quiet,
        auth_timeout_seconds=AUTH_TIMEOUT_SECONDS,
    )


def setup_logging(config: Config) -> logging.Logger:
    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("spotify_true_shuffle")
    logger.setLevel(getattr(logging, config.log_level))
    logger.handlers.clear()
    handler = logging.FileHandler(config.log_file, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    return logger


def validate_config(config: Config) -> None:
    if not config.client_id or not config.client_secret:
        raise SpotifyTrueShuffleError(
            "Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET environment variables."
        )


def safe_url_for_log(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc == "accounts.spotify.com":
        return urllib.parse.urlunparse(parsed._replace(query=""))
    return url


def payload_summary(payload: Any | None, form_payload: bool) -> str:
    if payload is None:
        return "none"
    if form_payload and isinstance(payload, dict):
        return "form keys=" + ",".join(sorted(payload.keys()))
    if isinstance(payload, dict) and isinstance(payload.get("uris"), list):
        return f"json uris={len(payload['uris'])}"
    if isinstance(payload, dict):
        return "json keys=" + ",".join(sorted(payload.keys()))
    return type(payload).__name__


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: Any | None = None,
    form_payload: bool = False,
) -> Any:
    logger = logging.getLogger("spotify_true_shuffle")
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        if form_payload:
            body = urllib.parse.urlencode(payload).encode("utf-8")
            request_headers.setdefault(
                "Content-Type", "application/x-www-form-urlencoded"
            )
        else:
            body = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
    )
    started = time.monotonic()
    logger.debug(
        "HTTP request method=%s url=%s payload=%s",
        method,
        safe_url_for_log(url),
        payload_summary(payload, form_payload),
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
            logger.debug(
                "HTTP response status=%s url=%s bytes=%s elapsed=%.3fs",
                response.status,
                safe_url_for_log(url),
                len(raw),
                time.monotonic() - started,
            )
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw_error = exc.read().decode("utf-8", errors="replace")
        logger.warning(
            "HTTP error status=%s url=%s elapsed=%.3fs body=%s",
            exc.code,
            safe_url_for_log(url),
            time.monotonic() - started,
            raw_error,
        )
        raise SpotifyTrueShuffleError(
            f"HTTP {exc.code} from {url}: {raw_error}"
        ) from exc
    except urllib.error.URLError as exc:
        logger.warning(
            "Network error url=%s elapsed=%.3fs error=%s",
            safe_url_for_log(url),
            time.monotonic() - started,
            exc,
        )
        raise SpotifyTrueShuffleError(f"Network error for {url}: {exc}") from exc


def token_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def load_token(cache_file: Path) -> dict[str, Any] | None:
    if not cache_file.exists():
        return None
    with cache_file.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_token(cache_file: Path, token: dict[str, Any]) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with cache_file.open("w", encoding="utf-8") as handle:
        json.dump(token, handle, indent=2, sort_keys=True)
        handle.write("\n")
    cache_file.chmod(stat.S_IRUSR | stat.S_IWUSR)


def token_with_expiry(token: dict[str, Any]) -> dict[str, Any]:
    updated = dict(token)
    updated["expires_at"] = int(time.time()) + int(token.get("expires_in", 3600)) - 60
    return updated


def token_has_required_scopes(token: dict[str, Any]) -> bool:
    granted_scopes = set(str(token.get("scope", "")).split())
    return set(SCOPES).issubset(granted_scopes)


def refresh_token(config: Config, token: dict[str, Any]) -> dict[str, Any]:
    if "refresh_token" not in token:
        raise SpotifyTrueShuffleError("Cached token has no refresh_token.")
    logging.getLogger("spotify_true_shuffle").info("Refreshing Spotify access token")
    response = request_json(
        f"{ACCOUNTS_BASE}/api/token",
        method="POST",
        headers={
            "Authorization": token_auth_header(config.client_id, config.client_secret),
        },
        payload={
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
        },
        form_payload=True,
    )
    if "refresh_token" not in response:
        response["refresh_token"] = token["refresh_token"]
    return token_with_expiry(response)


class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    server: "OAuthCallbackServer"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        values = urllib.parse.parse_qs(parsed.query)
        self.server.auth_code = values.get("code", [None])[0]
        self.server.auth_state = values.get("state", [None])[0]
        self.server.auth_error = values.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"Spotify authorization received. You can close this browser tab.\n"
        )

    def log_message(self, format: str, *args: Any) -> None:
        return


class OAuthCallbackServer(socketserver.TCPServer):
    allow_reuse_address = True
    auth_code: str | None = None
    auth_state: str | None = None
    auth_error: str | None = None


def interactive_auth(config: Config, log: QuietAwareLogger) -> dict[str, Any]:
    if config.quiet:
        raise SpotifyTrueShuffleError(
            "No cached token is available. Run once without --quiet to authorize."
        )

    redirect = urllib.parse.urlparse(config.redirect_uri)
    if redirect.hostname not in {"127.0.0.1", "localhost"}:
        raise SpotifyTrueShuffleError(
            "First-time auth only supports localhost redirect URIs."
        )
    if not redirect.port:
        raise SpotifyTrueShuffleError("Redirect URI must include a port.")

    state = uuid.uuid4().hex
    params = urllib.parse.urlencode(
        {
            "client_id": config.client_id,
            "response_type": "code",
            "redirect_uri": config.redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
        }
    )
    authorize_url = f"{ACCOUNTS_BASE}/authorize?{params}"
    log.logger.info(
        "Starting interactive authorization redirect_uri=%s scopes=%s timeout_seconds=%s",
        config.redirect_uri,
        ",".join(SCOPES),
        config.auth_timeout_seconds,
    )
    log.user("Open this URL to authorize Spotify access:")
    log.user(authorize_url)

    with OAuthCallbackServer((redirect.hostname, redirect.port), OAuthCallbackHandler) as server:
        server.timeout = 1
        log.logger.debug("Listening for OAuth callback on %s:%s", redirect.hostname, redirect.port)
        with contextlib.suppress(Exception):
            webbrowser.open(authorize_url)

        deadline = time.time() + config.auth_timeout_seconds
        while time.time() < deadline and not (
            server.auth_code or server.auth_error
        ):
            server.handle_request()

        if server.auth_error:
            raise SpotifyTrueShuffleError(f"Spotify authorization failed: {server.auth_error}")
        if not server.auth_code:
            raise SpotifyTrueShuffleError("Timed out waiting for Spotify authorization.")
        if server.auth_state != state:
            raise SpotifyTrueShuffleError("OAuth state mismatch.")

        log.logger.info("Received Spotify authorization callback")
        response = request_json(
            f"{ACCOUNTS_BASE}/api/token",
            method="POST",
            headers={
                "Authorization": token_auth_header(
                    config.client_id, config.client_secret
                ),
            },
            payload={
                "grant_type": "authorization_code",
                "code": server.auth_code,
                "redirect_uri": config.redirect_uri,
            },
            form_payload=True,
        )
    return token_with_expiry(response)


class SpotifyClient:
    def __init__(self, config: Config, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.token = self._get_token()
        self.created_playlist_ids: set[str] = set()

    def _get_token(self) -> dict[str, Any]:
        token = load_token(self.config.cache_file)
        if token:
            self.logger.debug("Loaded token from %s", self.config.cache_file)
            self.logger.debug(
                "Cached token scopes=%s expires_at=%s",
                token.get("scope", ""),
                token.get("expires_at", ""),
            )
            if int(token.get("expires_at", 0)) <= int(time.time()):
                self.logger.info("Cached access token is expired")
                token = refresh_token(self.config, token)
                save_token(self.config.cache_file, token)
            if not token_has_required_scopes(token):
                if self.config.quiet:
                    raise SpotifyTrueShuffleError(
                        "Cached token is missing required scopes. Run once without --quiet to reauthorize."
                    )
                self.logger.info("Cached token is missing required scopes; reauthorizing")
                token = interactive_auth(
                    self.config, QuietAwareLogger(self.logger, self.config.quiet)
                )
                save_token(self.config.cache_file, token)
            return token

        self.logger.info("No cached Spotify token found; starting authorization")
        token = interactive_auth(self.config, QuietAwareLogger(self.logger, self.config.quiet))
        save_token(self.config.cache_file, token)
        self.logger.info("Saved token cache to %s", self.config.cache_file)
        return token

    def _guard_write(self, path_or_url: str, method: str) -> None:
        if method == "GET":
            return
        self.logger.debug("Checking Spotify write guard method=%s path=%s", method, path_or_url)
        if path_or_url.startswith("http"):
            raise SpotifyTrueShuffleError(
                f"Refusing Spotify write to absolute URL: {method} {path_or_url}"
            )

        create_playlist = method == "POST" and path_or_url == "/me/playlists"
        add_to_created_playlist = method == "POST" and re.fullmatch(
            r"/playlists/[^/]+/items", path_or_url
        )
        if add_to_created_playlist:
            playlist_id = path_or_url.split("/")[2]
            add_to_created_playlist = playlist_id in self.created_playlist_ids

        if not (create_playlist or add_to_created_playlist):
            raise SpotifyTrueShuffleError(
                f"Refusing Spotify write outside allowed scope: {method} {path_or_url}"
            )

    def api(self, path_or_url: str, *, method: str = "GET", payload: Any | None = None) -> Any:
        self._guard_write(path_or_url, method)
        url = path_or_url if path_or_url.startswith("http") else API_BASE + path_or_url
        headers = {"Authorization": f"Bearer {self.token['access_token']}"}
        return request_json(url, method=method, headers=headers, payload=payload)

    def current_user(self) -> dict[str, Any]:
        return self.api("/me")

    def all_current_user_playlists(self) -> list[dict[str, Any]]:
        playlists: list[dict[str, Any]] = []
        url: str | None = f"{API_BASE}/me/playlists?limit=50"
        page_number = 0
        self.logger.info("Fetching current user's playlists")
        while url:
            page_number += 1
            page = self.api(url)
            playlists.extend(page.get("items", []))
            self.logger.debug(
                "Fetched playlist page=%s page_items=%s total_so_far=%s next=%s",
                page_number,
                len(page.get("items", [])),
                len(playlists),
                bool(page.get("next")),
            )
            url = page.get("next")
        self.logger.info("Fetched %s playlists", len(playlists))
        return playlists

    def playlist_details(self, playlist_id: str) -> dict[str, Any]:
        self.logger.debug("Fetching full playlist details playlist_id=%s", playlist_id)
        return self.api(f"/playlists/{urllib.parse.quote(playlist_id)}")

    def playlist_item_total(self, playlist_id: str) -> int | None:
        self.logger.debug("Fetching playlist item total playlist_id=%s", playlist_id)
        path = (
            f"/playlists/{urllib.parse.quote(playlist_id)}/items"
            "?limit=1&fields=total"
        )
        response = self.api(path)
        total = response.get("total")
        return total if isinstance(total, int) else None

    def all_liked_track_uris(self) -> list[str]:
        uris: list[str] = []
        url: str | None = f"{API_BASE}/me/tracks?limit=50"
        skipped = 0
        page_number = 0
        self.logger.info("Fetching liked songs")
        while url:
            page_number += 1
            page = self.api(url)
            page_uris = 0
            page_skipped = 0
            for item in page.get("items", []):
                track = item.get("track") or {}
                uri = track.get("uri")
                if uri and uri.startswith("spotify:track:"):
                    uris.append(uri)
                    page_uris += 1
                else:
                    skipped += 1
                    page_skipped += 1
            self.logger.debug(
                "Fetched liked-song page=%s page_tracks=%s page_skipped=%s total_tracks=%s next=%s",
                page_number,
                page_uris,
                page_skipped,
                len(uris),
                bool(page.get("next")),
            )
            url = page.get("next")
        self.logger.info(
            "Fetched %s liked Spotify track URIs; skipped %s non-track/unusable items",
            len(uris),
            skipped,
        )
        return uris

    def create_spotify_playlist(self, name: str, description: str) -> dict[str, Any]:
        self.logger.info("Creating playlist name=%r", name)
        playlist = self.api(
            "/me/playlists",
            method="POST",
            payload={
                "name": name,
                "description": description,
            },
        )
        self.created_playlist_ids.add(playlist["id"])
        self.logger.info(
            "Created playlist id=%s name=%r",
            playlist["id"],
            playlist.get("name", name),
        )
        return playlist

    def add_tracks(self, playlist_id: str, uris: list[str]) -> None:
        total_batches = (len(uris) + 99) // 100
        self.logger.info(
            "Adding %s tracks to playlist_id=%s in %s batches",
            len(uris),
            playlist_id,
            total_batches,
        )
        for start in range(0, len(uris), 100):
            batch_number = start // 100 + 1
            batch = uris[start : start + 100]
            self.logger.debug(
                "Adding playlist batch=%s/%s start=%s count=%s",
                batch_number,
                total_batches,
                start,
                len(batch),
            )
            self.api(
                f"/playlists/{urllib.parse.quote(playlist_id)}/items",
                method="POST",
                payload={"uris": batch},
            )
        self.logger.info("Finished adding tracks to playlist_id=%s", playlist_id)


def true_random_playlists(playlists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [playlist for playlist in playlists if PLAYLIST_MARKER in playlist.get("name", "")]


def next_playlist_name(playlists: list[dict[str, Any]], logger: logging.Logger) -> str:
    highest = 0
    matching_playlists = true_random_playlists(playlists)
    pattern = re.compile(r"\bTrue random\s+#(\d+)\b")
    for playlist in matching_playlists:
        match = pattern.search(playlist.get("name", ""))
        if match:
            highest = max(highest, int(match.group(1)))
    name = f"{PLAYLIST_PREFIX} #{highest + 1}"
    logger.info(
        "Selected playlist name=%r from %s existing true-random playlists",
        name,
        len(matching_playlists),
    )
    logger.debug("Highest existing true-random playlist number=%s", highest)
    return name


def created_at_from_description(description: str | None) -> str:
    if not description:
        return "unknown"
    match = re.search(r"\bCreated:\s*([0-9T:+-]+)", description)
    if not match:
        return "unknown"
    return match.group(1)


def playlist_track_total(playlist: dict[str, Any], item_total: int | None = None) -> str:
    if item_total is not None:
        return str(item_total)
    tracks = playlist.get("tracks") or {}
    total = tracks.get("total")
    return str(total) if total is not None else "unknown"


def list_playlists(client: SpotifyClient, output: QuietAwareLogger) -> None:
    playlists = true_random_playlists(client.all_current_user_playlists())
    output.logger.info("Listing %s true-random playlists", len(playlists))
    if not playlists:
        output.user("No playlists containing %r found.", PLAYLIST_MARKER)
        return
    for playlist in playlists:
        playlist_id = playlist.get("id")
        item_total = None
        if playlist_id:
            playlist = client.playlist_details(playlist_id)
            item_total = client.playlist_item_total(playlist_id)
        owner = playlist.get("owner") or {}
        output.user(
            "%s | %s tracks | created: %s | owner: %s | %s",
            playlist.get("name", "(unnamed)"),
            playlist_track_total(playlist, item_total),
            created_at_from_description(playlist.get("description")),
            owner.get("display_name") or owner.get("id") or "unknown",
            playlist.get("external_urls", {}).get("spotify", ""),
        )


def create_playlist(client: SpotifyClient, output: QuietAwareLogger) -> None:
    playlists = client.all_current_user_playlists()
    name = next_playlist_name(playlists, output.logger)
    output.user("Fetching all liked songs from Spotify...")
    uris = client.all_liked_track_uris()
    if not uris:
        raise SpotifyTrueShuffleError("No liked songs found.")

    output.logger.info("Shuffling %s liked Spotify track URIs", len(uris))
    secrets.SystemRandom().shuffle(uris)
    output.logger.debug("Shuffle complete using secrets.SystemRandom")
    playlist = client.create_spotify_playlist(
        name,
        (
            f"True-random shuffle of {len(uris)} liked songs. "
            f"Created: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}"
        ),
    )
    client.add_tracks(playlist["id"], uris)
    output.user(
        "Created %s with %s tracks: %s",
        name,
        len(uris),
        playlist.get("external_urls", {}).get("spotify", ""),
    )


def main() -> int:
    logger: logging.Logger | None = None
    try:
        config = parse_args()
        logger = setup_logging(config)
        validate_config(config)
        logger.info("Starting action=%s quiet=%s", config.action, config.quiet)
        logger.debug(
            "Configuration redirect_uri=%s cache_file=%s log_file=%s auth_timeout_seconds=%s",
            config.redirect_uri,
            config.cache_file,
            config.log_file,
            config.auth_timeout_seconds,
        )
        client = SpotifyClient(config, logger)
        output = QuietAwareLogger(logger, config.quiet)
        if config.action == "create":
            create_playlist(client, output)
        elif config.action == "list":
            list_playlists(client, output)
        logger.info("Completed action=%s", config.action)
        return 0
    except SpotifyTrueShuffleError as exc:
        if logger:
            logger.error("%s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if logger:
            logger.warning("Interrupted")
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
