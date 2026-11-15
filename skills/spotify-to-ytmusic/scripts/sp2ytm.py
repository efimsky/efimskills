#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["spotipy>=2.24", "ytmusicapi>=1.10"]
# ///
"""Copy Spotify liked songs and playlists to YouTube Music.

Self-contained: needs only spotipy and ytmusicapi. All state (credentials,
tokens, match cache) lives in ~/.config/sp2ytm with owner-only permissions.
Run `sp2ytm.py -h` for the subcommands; SKILL.md describes the workflow.

The track-matching heuristic follows the approach used by
sigma67/spotify_to_ytmusic (title, artist, duration and album similarity,
preferring songs over videos).
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CONFIG_DIR = Path(os.environ.get("SP2YTM_HOME", Path.home() / ".config" / "sp2ytm"))
SPOTIFY_CONF = CONFIG_DIR / "spotify.json"
SPOTIFY_TOKEN = CONFIG_DIR / "spotify-token.json"
YTM_AUTH = CONFIG_DIR / "ytmusic-browser.json"
MATCH_CACHE = CONFIG_DIR / "match-cache.json"

REDIRECT_URI = "https://127.0.0.1"
SCOPES = "user-library-read playlist-read-private playlist-read-collaborative"
ORIGIN = "https://music.youtube.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0"
)


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> None:
    sys.exit(f"error: {msg}")


def write_private(path: Path, text: str) -> None:
    """Write a file only the owner can read; these files hold credentials."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


# --------------------------------------------------------------------------
# Spotify
# --------------------------------------------------------------------------


def spotify_auth():
    from spotipy import CacheFileHandler
    from spotipy.oauth2 import SpotifyOAuth

    if not SPOTIFY_CONF.is_file():
        die("Spotify is not configured; run `spotify-setup` first")
    conf = json.loads(SPOTIFY_CONF.read_text())
    return SpotifyOAuth(
        client_id=conf["client_id"],
        client_secret=conf["client_secret"],
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
        cache_handler=CacheFileHandler(cache_path=str(SPOTIFY_TOKEN)),
        open_browser=False,
    )


def spotify_client():
    import spotipy

    auth = spotify_auth()
    if not auth.validate_token(auth.cache_handler.get_cached_token()):
        die("no valid Spotify token; run `spotify-auth-url`, then `spotify-auth-code`")
    return spotipy.Spotify(auth_manager=auth)


def _inner_track(entry: dict) -> dict | None:
    # Spotify renamed the wrapper key from "track" to "item" in 2026, and the
    # inner object now also carries a boolean "track" flag, so check types.
    for key in ("item", "track"):
        value = entry.get(key)
        if isinstance(value, dict):
            return value
    return None


def _to_tracks(entries: list[dict]) -> tuple[list[dict], int]:
    tracks, skipped = [], 0
    for entry in entries:
        t = _inner_track(entry)
        if not t or t.get("type", "track") != "track" or not t.get("duration_ms") or not t.get("name"):
            skipped += 1  # podcast episode, removed or unavailable track
            continue
        tracks.append(
            {
                "id": t.get("id"),
                "artist": " ".join(a["name"] for a in t.get("artists") or [] if a.get("name")),
                "name": t["name"],
                "album": (t.get("album") or {}).get("name") or "",
                "duration": t["duration_ms"] / 1000,
            }
        )
    return tracks, skipped


def paged(fetch) -> tuple[list[dict], int]:
    """Collect every item of a paged Spotify listing; `fetch(offset)` returns a page.

    Pages by offset until the reported total is reached, because Spotify
    occasionally answers with an empty page (and no `next`) for a listing
    that is not actually finished.
    """
    entries, total, empty = [], None, 0
    while total is None or len(entries) < total:
        page = fetch(len(entries))
        total = page["total"]
        if page["items"]:
            entries += page["items"]
            empty = 0
        else:
            empty += 1
            if empty >= 4:
                log(f"  warning: Spotify returned {len(entries)} of {total} items")
                break
            time.sleep(2 * empty)
    return entries, total


def fetch_liked(sp) -> dict:
    entries, total = paged(lambda offset: sp.current_user_saved_tracks(limit=50, offset=offset))
    tracks, skipped = _to_tracks(entries)
    return {
        "name": "Spotify Liked Songs",
        "description": "Liked songs copied from Spotify",
        "tracks": tracks,
        "total": total,
        "skipped": skipped,
    }


def playlist_id_from(link: str) -> str:
    if m := re.search(r"playlist[/:]([A-Za-z0-9]{22})", link):
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{22}", link):
        return link
    die(f"not a Spotify playlist link or id: {link}")


def fetch_playlist(sp, link: str) -> dict:
    pid = playlist_id_from(link)
    meta = sp.playlist(pid, fields="name,description")
    entries, total = paged(lambda offset: sp.playlist_items(pid, offset=offset, limit=100))
    tracks, skipped = _to_tracks(entries)
    return {
        "name": meta["name"].strip(),
        "description": html.unescape(meta.get("description") or ""),
        "tracks": tracks,
        "total": total,
        "skipped": skipped,
    }


# --------------------------------------------------------------------------
# YouTube Music
# --------------------------------------------------------------------------


def _header_value(text: str, name: str) -> str | None:
    """Find a header in pasted request headers.

    Handles `name: value` on one line (Firefox, older Chrome) and the newer
    Chrome layout where the name and the value are on consecutive lines.
    """
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        low = line.lower()
        if low.startswith(name + ": "):
            return line[len(name) + 2 :].strip()
        if low in (name, name + ":") and i + 1 < len(lines):
            return lines[i + 1]
    return None


def sapisid_hash(cookie: str) -> str:
    m = re.search(r"(?:^|;\s*)__Secure-3PAPISID=([^;]+)", cookie) or re.search(
        r"(?:^|;\s*)SAPISID=([^;]+)", cookie
    )
    if not m:
        die("the cookie has no SAPISID value; copy it from a signed-in music.youtube.com tab")
    ts = str(int(time.time()))
    digest = hashlib.sha1(f"{ts} {m.group(1)} {ORIGIN}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


def build_ytm_auth(pasted: str, authuser: str | None) -> dict:
    cookie = _header_value(pasted, "cookie")
    if cookie is None and "SAPISID=" in pasted and "\n" not in pasted.strip():
        cookie = pasted.strip()  # a bare cookie value was pasted
    if not cookie:
        die("no cookie found in the input; paste the request headers or the cookie value")
    return {
        "user-agent": USER_AGENT,
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.5",
        "content-type": "application/json",
        "origin": ORIGIN,
        "x-origin": ORIGIN,
        "x-goog-authuser": authuser or _header_value(pasted, "x-goog-authuser") or "0",
        # ytmusicapi recomputes this per request; it only needs to be present
        # so the file is recognised as browser authentication.
        "authorization": sapisid_hash(cookie),
        "cookie": cookie,
    }


def ytm_client():
    from ytmusicapi import YTMusic

    if not YTM_AUTH.is_file():
        die("YouTube Music is not configured; run `ytm-auth` first")
    return YTMusic(str(YTM_AUTH))


def retry(fn, what: str, attempts: int = 5, base_delay: float = 5.0):
    last = None
    for n in range(attempts):
        try:
            return fn()
        except Exception as e:  # network hiccups and throttling look alike here
            last = e
            if n < attempts - 1:
                time.sleep(base_delay * (n + 1) ** 2)
    raise RuntimeError(f"{what} failed after {attempts} attempts: {type(last).__name__}: {str(last)[:200]}")


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(a=a.lower(), b=b.lower()).ratio()


def _seconds(candidate: dict) -> float | None:
    if candidate.get("duration_seconds"):
        return float(candidate["duration_seconds"])
    text = candidate.get("duration")
    if not text:
        return None
    try:
        total = 0
        for part in text.split(":"):
            total = total * 60 + int(part)
        return float(total)
    except ValueError:
        return None


def best_match(results: list[dict], track: dict) -> str | None:
    best_id, best_score = None, float("-inf")
    for c in results:
        # Search results are not uniform: some have no videoId or no artists.
        if c.get("resultType") not in ("song", "video") or not c.get("videoId") or not c.get("title"):
            continue
        is_song = c["resultType"] == "song"
        title = c["title"]
        if not is_song and title.count("-") == 1:
            title = title.split("-")[1]  # "Artist - Title" video naming
        artists = " ".join(a.get("name") or "" for a in c.get("artists") or [])
        parts = [_ratio(title, track["name"]), _ratio(artists, track["artist"])]
        seconds = _seconds(c)
        if seconds and track["duration"]:
            parts.append((1 - abs(seconds - track["duration"]) * 2 / track["duration"]) * 5)
        if is_song and c.get("album") and c["album"].get("name"):
            parts.append(_ratio(c["album"]["name"], track["album"]))
        score = sum(parts) / len(parts) * (2 if is_song else 1)
        if score > best_score:
            best_id, best_score = c["videoId"], score
    return best_id


def query_for(track: dict) -> str:
    name = re.sub(r" \(feat.*\..+\)", "", track["name"])
    return f"{track['artist']} {name}".replace(" &", "")


def match_tracks(yt, tracks: list[dict]) -> tuple[list[str], list[str]]:
    """Return (video ids in input order, unmatched queries)."""
    cache = json.loads(MATCH_CACHE.read_text()) if MATCH_CACHE.is_file() else {}
    ids, unmatched, dirty = [], [], 0
    for i, track in enumerate(tracks, 1):
        query = query_for(track)
        key = track.get("id") or query
        video_id = cache.get(key)
        if not video_id:
            # Songs-only search first: the mixed default search often returns
            # just a "top result" plus videos, which picks live cuts and
            # wrong-length versions. Fall back to it for video-only tracks.
            results = retry(lambda: yt.search(query, filter="songs"), f"search for {query!r}")
            video_id = best_match(results, track)
            if not video_id:
                results = retry(lambda: yt.search(query), f"search for {query!r}")
                video_id = best_match(results, track)
            if video_id:
                cache[key] = video_id
                dirty += 1
        if video_id:
            ids.append(video_id)
        else:
            unmatched.append(query)
        # Save as we go so an interrupted run resumes instead of restarting.
        if dirty >= 20:
            write_private(MATCH_CACHE, json.dumps(cache, ensure_ascii=False))
            dirty = 0
        if i % 50 == 0:
            log(f"  matched {i}/{len(tracks)}")
    if dirty:
        write_private(MATCH_CACHE, json.dumps(cache, ensure_ascii=False))
    return ids, unmatched


# --------------------------------------------------------------------------
# Copying
# --------------------------------------------------------------------------


def like_oldest_first(yt, playlist_id: str, pace: float) -> dict:
    """Like a playlist's tracks from the bottom up.

    Liked Music shows the most recently liked song first, and Spotify lists
    newest first, so liking from the bottom keeps the Spotify order.
    """
    tracks = [t for t in yt.get_playlist(playlist_id, limit=None)["tracks"] if t.get("videoId")]
    todo = [t for t in reversed(tracks) if t.get("likeStatus") != "LIKE"]
    log(f"  liking {len(todo)} songs ({len(tracks) - len(todo)} already liked)")
    liked, failed, streak = 0, [], 0
    for i, t in enumerate(todo, 1):
        try:
            retry(lambda: yt.rate_song(t["videoId"], "LIKE"), "like", attempts=4)
            liked, streak = liked + 1, 0
        except RuntimeError as e:
            failed.append(f"{t.get('title')} ({t['videoId']}): {e}")
            streak += 1
            if streak >= 5:
                log("  5 likes failed in a row; stopping (probably throttled, re-run later)")
                break
        if i % 50 == 0:
            log(f"  liked {liked}/{len(todo)}")
        time.sleep(pace)
    return {"liked": liked, "already_liked": len(tracks) - len(todo), "failed": failed}


def copy_one(yt, source: dict, args, like: bool = False) -> dict:
    name = getattr(args, "name", None) or source["name"]
    tracks = source["tracks"][: args.limit] if args.limit else source["tracks"]
    log(f"{name!r}: {len(tracks)} tracks ({source['skipped']} unavailable on Spotify)")
    ids, unmatched = match_tracks(yt, tracks)
    unique = list(dict.fromkeys(ids))
    result = {
        "name": name,
        "spotify_total": source["total"],
        "spotify_unavailable": source["skipped"],
        "considered": len(tracks),
        "matched": len(ids),
        "unmatched": unmatched,
        "collapsed_duplicates": len(ids) - len(unique),
    }
    if args.dry_run:
        result["status"] = "dry-run"
        return result
    if len(unique) > 5000:
        die(f"{name!r} has {len(unique)} tracks; YouTube Music playlists hold at most 5,000")

    existing = {p["title"]: p["playlistId"] for p in yt.get_library_playlists(limit=None)}
    if name in existing and not args.allow_same_name:
        result.update(status="skipped: a playlist with this name already exists", playlist_id=existing[name])
        return result

    playlist_id = yt.create_playlist(name, source["description"][:4000], "PRIVATE")
    if not isinstance(playlist_id, str):
        die(f"could not create playlist {name!r}: {str(playlist_id)[:200]}")
    def add(chunk):
        # ytmusicapi reports a rejected add in the response instead of raising.
        response = yt.add_playlist_items(playlist_id, chunk, duplicates=True)
        if isinstance(response, dict) and "SUCCEEDED" not in str(response.get("status")):
            raise RuntimeError(f"add rejected: {str(response)[:200]}")

    for start in range(0, len(unique), 100):  # one huge request gets rejected
        chunk = unique[start : start + 100]
        retry(lambda: add(chunk), "add tracks")
        time.sleep(1)
    # Count what actually landed rather than what was sent; the playlist can
    # take a few seconds to show new tracks.
    added = 0
    for _ in range(6):
        time.sleep(3)
        added = len(yt.get_playlist(playlist_id, limit=None).get("tracks") or [])
        if added >= len(unique):
            break
    result.update(
        status="created",
        playlist_id=playlist_id,
        sent=len(unique),
        added=added,
        url=f"https://music.youtube.com/playlist?list={playlist_id}",
    )
    if like:
        result["likes"] = like_oldest_first(yt, playlist_id, args.pace)
    return result


def report(results: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for r in results:
        log("")
        log(f"{r['name']!r}: {r['status']}")
        log(
            f"  Spotify {r['spotify_total']} | unavailable {r['spotify_unavailable']} | "
            f"matched {r['matched']} | unmatched {len(r['unmatched'])} | "
            f"duplicates collapsed {r['collapsed_duplicates']}"
        )
        if "added" in r:
            log(f"  added {r['added']} -> {r['url']}")
        if "likes" in r:
            likes = r["likes"]
            log(f"  liked {likes['liked']}, already liked {likes['already_liked']}, failed {len(likes['failed'])}")
        if r["unmatched"]:
            safe = re.sub(r"[^\w -]", "_", r["name"]).strip() or "playlist"
            path = out_dir / f"unmatched - {safe}.txt"
            path.write_text("\n".join(r["unmatched"]) + "\n", encoding="utf-8")
            log(f"  unmatched songs listed in {path}")
    (out_dir / "last-run.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_spotify_setup(args) -> None:
    client_id = args.client_id or input("Spotify client id: ").strip()
    client_secret = args.client_secret or input("Spotify client secret: ").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", client_id) or not re.fullmatch(r"[0-9a-f]{32}", client_secret):
        die("client id and secret are each 32 hex characters; copy them from the app's Settings page")
    write_private(SPOTIFY_CONF, json.dumps({"client_id": client_id, "client_secret": client_secret}))
    SPOTIFY_TOKEN.unlink(missing_ok=True)
    log(f"saved to {SPOTIFY_CONF}")


def cmd_spotify_auth_url(args) -> None:
    print(spotify_auth().get_authorize_url() + "&show_dialog=true")


def cmd_spotify_auth_code(args) -> None:
    value = args.redirect_url.strip()
    code = parse_qs(urlparse(value).query).get("code", [None])[0] if "code=" in value else value
    if not code or "accounts.spotify.com" in value:
        die("that is the consent page itself; click Agree, then copy the https://127.0.0.1/?code=... address")
    auth = spotify_auth()
    auth.get_access_token(code, as_dict=False, check_cache=False)
    os.chmod(SPOTIFY_TOKEN, 0o600)
    import spotipy

    total = spotipy.Spotify(auth_manager=auth).current_user_saved_tracks(limit=1)["total"]
    log(f"Spotify connected; liked songs: {total}")


def cmd_ytm_auth(args) -> None:
    pasted = Path(args.file).read_text() if args.file else sys.stdin.read()
    write_private(YTM_AUTH, json.dumps(build_ytm_auth(pasted, args.authuser)))
    yt = ytm_client()
    try:
        liked = yt.get_liked_songs(limit=1).get("trackCount")
        playlists = [p["title"] for p in yt.get_library_playlists(limit=None)]
    except Exception as e:
        YTM_AUTH.unlink(missing_ok=True)
        die(f"YouTube Music rejected the session ({type(e).__name__}: {str(e)[:200]}); copy fresh headers")
    log(f"YouTube Music connected; Liked Music has {liked} songs; {len(playlists)} playlists:")
    for title in playlists:
        log(f"  - {title}")
    log("If this is not the right account, re-run with --authuser 1 (or 2, ...).")


def cmd_list(args) -> None:
    sp = spotify_client()
    me = sp.current_user()["id"]
    log(f"Liked songs: {sp.current_user_saved_tracks(limit=1)['total']}")
    items, _ = paged(lambda offset: sp.current_user_playlists(limit=50, offset=offset))
    own, followed = [], []
    for p in filter(None, items):
        count = (p.get("items") if isinstance(p.get("items"), dict) else p.get("tracks") or {}).get("total")
        row = f"{count:>5}  {p['name']}  {p['external_urls']['spotify']}"
        if p["owner"]["id"] == me:
            own.append(row)
        else:
            followed.append(row + f"  (by {p['owner'].get('display_name')})")
    log(f"\nOwn playlists ({len(own)}):")
    for row in own:
        log("  " + row)
    log(f"\nFollowed playlists ({len(followed)}):")
    for row in followed:
        log("  " + row)


def cmd_copy_liked(args) -> None:
    sp, yt = spotify_client(), ytm_client()
    result = copy_one(yt, fetch_liked(sp), args, like=args.like)
    report([result], Path(args.out))


def cmd_copy_playlist(args) -> None:
    sp, yt = spotify_client(), ytm_client()
    results = []
    for link in args.links:
        results.append(copy_one(yt, fetch_playlist(sp, link), args))
    report(results, Path(args.out))


def cmd_like_playlist(args) -> None:
    likes = like_oldest_first(ytm_client(), args.playlist_id, args.pace)
    log(f"liked {likes['liked']}, already liked {likes['already_liked']}, failed {len(likes['failed'])}")
    for line in likes["failed"]:
        log("  failed: " + line)


def cmd_forget(args) -> None:
    for path in (SPOTIFY_CONF, SPOTIFY_TOKEN, YTM_AUTH):
        path.unlink(missing_ok=True)
    log(f"removed stored credentials from {CONFIG_DIR} (match cache kept)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("spotify-setup", help="store the Spotify app's client id and secret")
    p.add_argument("--client-id")
    p.add_argument("--client-secret")
    p.set_defaults(func=cmd_spotify_setup)

    p = sub.add_parser("spotify-auth-url", help="print the Spotify consent URL")
    p.set_defaults(func=cmd_spotify_auth_url)

    p = sub.add_parser("spotify-auth-code", help="finish Spotify login with the redirect URL")
    p.add_argument("redirect_url", help="the https://127.0.0.1/?code=... address (or just the code)")
    p.set_defaults(func=cmd_spotify_auth_code)

    p = sub.add_parser("ytm-auth", help="store a YouTube Music browser session (headers on stdin)")
    p.add_argument("--file", help="read the pasted headers from this file instead of stdin")
    p.add_argument("--authuser", help="Google account index when several are signed in (default 0)")
    p.set_defaults(func=cmd_ytm_auth)

    p = sub.add_parser("list", help="show liked-song count and Spotify playlists")
    p.set_defaults(func=cmd_list)

    def copy_options(p):
        p.add_argument("--dry-run", action="store_true", help="match only; change nothing on YouTube Music")
        p.add_argument("--limit", type=int, help="only the first N tracks (for a trial run)")
        p.add_argument("--allow-same-name", action="store_true", help="create even if the name exists")
        p.add_argument("--out", default=str(Path.home() / "spotify-transfer"), help="where reports go")
        p.add_argument("--pace", type=float, default=0.7, help="seconds between likes")

    p = sub.add_parser("copy-liked", help="copy Liked Songs to a private playlist, optionally liking them")
    p.add_argument("--name", help='playlist name (default "Spotify Liked Songs")')
    p.add_argument("--like", action="store_true", help="also like every song, oldest first")
    copy_options(p)
    p.set_defaults(func=cmd_copy_liked)

    p = sub.add_parser("copy-playlist", help="copy Spotify playlists to private YouTube Music playlists")
    p.add_argument("links", nargs="+", help="Spotify playlist links or ids")
    copy_options(p)
    p.set_defaults(func=cmd_copy_playlist)

    p = sub.add_parser("like-playlist", help="like every song of a YouTube Music playlist, oldest first")
    p.add_argument("playlist_id")
    p.add_argument("--pace", type=float, default=0.7)
    p.set_defaults(func=cmd_like_playlist)

    p = sub.add_parser("forget", help="delete the stored Spotify and YouTube Music credentials")
    p.set_defaults(func=cmd_forget)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
