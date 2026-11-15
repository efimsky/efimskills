---
name: spotify-to-ytmusic
description: Migrate a Spotify library to YouTube Music — like the same songs so Liked Songs is recreated in Liked Music (in the same order), and copy playlists across as private playlists. Use this whenever the user wants to move, switch, transfer, sync, export or copy liked songs, saved tracks or playlists from Spotify to YouTube Music, is thinking about leaving or cancelling Spotify, or asks how to "like the same songs" on YouTube Music, even if they don't name a tool. Bundles a tested script, so prefer it over third-party transfer sites or installing other tools.
---

# Spotify to YouTube Music

Copy a person's Spotify liked songs and playlists into their YouTube Music account with the
bundled `scripts/sp2ytm.py`. The script does the mechanical work; your job is to walk the
person through two logins, agree what to copy, run it, and report honest numbers.

Neither service has an import feature, so this works by reading the Spotify library through
its API, searching YouTube Music for each track, and then creating playlists and likes there.

## Before starting

- **Spotify Premium must still be active.** Spotify blocks its API for developer apps owned
  by a non-Premium account (403 errors). Do the migration before the person cancels.
- Work outside any code repository. Reports go to `~/spotify-transfer/` by default.
- The script needs Python 3.10+ with `spotipy` and `ytmusicapi`. If `uv` is installed, run it
  as `uv run <skill-dir>/scripts/sp2ytm.py ...`. Otherwise make a private environment once:

  ```bash
  python3 --version            # needs 3.10+; on macOS `brew install python` if older
  python3 -m venv ~/.config/sp2ytm/venv
  ~/.config/sp2ytm/venv/bin/pip install spotipy ytmusicapi
  ```

  and run `~/.config/sp2ytm/venv/bin/python <skill-dir>/scripts/sp2ytm.py ...`.
  The examples below write this as `sp2ytm`.

All credentials and the match cache live in `~/.config/sp2ytm/` with owner-only permissions.

## Handling the person's secrets

Three secrets are involved: the Spotify client secret, the Spotify redirect code, and the
YouTube Music session cookie. The cookie is the sensitive one — it is a full Google session.

Steer the person to hand secrets to the script directly rather than pasting them into the
conversation, because anything pasted stays in the transcript. The commands below are built
for that (clipboard piping, `!`-prefixed commands in Claude Code). If they paste one into the
chat anyway, don't lecture; use it, never echo it back, and at the end tell them how to
invalidate it (see "Wrap up").

## Step 1 — Connect Spotify

1. The person creates an app at https://developer.spotify.com/dashboard:
   - name and description: anything
   - **Redirect URI: `https://127.0.0.1`** — exactly, `https`, no port, no trailing slash
   - API: tick **Web API** only
   - then Settings shows the Client ID and "View client secret"
2. Store them: `sp2ytm spotify-setup` (prompts), or `--client-id ... --client-secret ...`.
3. `sp2ytm spotify-auth-url` prints a consent URL. Open it for them (`open <url>` on macOS)
   and also show the link.
4. They click **Agree**. The browser lands on a page that fails to load ("127.0.0.1 refused
   to connect"). Tell them in advance that this is expected: nothing listens there, and the
   only thing needed is the address in the address bar, `https://127.0.0.1/?code=...`.
5. `sp2ytm spotify-auth-code '<that url>'` finishes the login and prints the liked-song count.

Things that go wrong here:
- They paste the `accounts.spotify.com/authorize...` address. That is the consent page, so
  they have not clicked Agree yet, or copied the wrong tab. The script says so.
- The code is single-use and expires in minutes. If the exchange fails, print a fresh URL.
- The consent asks for liked songs and playlists (read-only) in one go, so there is no
  second consent later.

## Step 2 — Connect YouTube Music

YouTube Music has no usable public API; the script reuses the person's browser session.

1. In a browser signed in to the right Google account, open https://music.youtube.com.
2. Open DevTools → **Network**, type `browse` in the filter, click around (e.g. Library)
   until a `browse?...` POST request appears.
3. Copy that request's **request headers** (Firefox: right-click → Copy Value → Copy Request
   Headers. Chrome: Headers tab → Request Headers section, select it all and copy).
   Copying only the value of the `cookie` header works too.
4. With the headers on the clipboard, run (macOS; use `xclip -o`/`wl-paste` on Linux):

   ```bash
   pbpaste | sp2ytm ytm-auth
   ```

   In Claude Code the person can run this themselves by typing `! pbpaste | ... ytm-auth`,
   which keeps the cookie out of the conversation.

The command verifies the session and prints the Liked Music count and the account's playlist
names. **Read those back to the person and confirm it is the right account** — with several
Google accounts signed in, the default picks the first; re-run with `--authuser 1` (or 2…)
for another.

If they paste the request URL instead of the headers, explain the difference: the URL is
fine as a sign they found the right request, but the login lives in its headers.

## Step 3 — Agree what to copy

Run `sp2ytm list`. It shows the liked-song count, the person's **own** playlists and the
playlists they merely **follow**.

Ask what they want before copying anything beyond what they named. Do not assume "copy my
playlists" means all of them:
- Followed playlists belong to other people. A copy is a frozen snapshot that stops
  updating, and there are often dozens of them, thousands of tracks.
- People keep stale and auto-generated playlists they don't want carried over.
- YouTube Music playlists hold at most 5,000 tracks.

Offer the list and let them pick. Copy only the ones they name.

## Step 4 — Copy

Do a quick trial first; it changes nothing on YouTube Music and catches login problems:

```bash
sp2ytm copy-liked --dry-run --limit 20
```

Then the real runs:

```bash
sp2ytm copy-liked --like                 # playlist "Spotify Liked Songs" + likes
sp2ytm copy-playlist <spotify link> ...  # one or more playlists
```

What these do, so you can explain it:
- **`copy-liked`** always creates a private playlist with the liked songs in Spotify order.
  With `--like` it also likes every song. Keep the playlist even when liking: likes are shared
  with the person's YouTube account, and a plain playlist is the easy thing to re-export.
- **Likes are applied oldest-first**, because Liked Music shows the most recently liked song
  on top. That reproduces the Spotify order (newest on top). Likes are paced (0.7 s apart)
  and stop after five consecutive failures, which means throttling — re-run later; songs
  already liked are skipped, so re-running is safe.
- **Matching** searches YouTube Music songs for "artist title" and scores candidates on
  title, artist, album and — most heavily — duration, so it prefers the same recording over
  live or extended versions. Matches are cached, so an interrupted run resumes.
- A playlist whose name already exists on YouTube Music is skipped rather than duplicated
  (`--allow-same-name` overrides).

Rough speed: about 2–3 songs per second for matching, plus under a second per like. Run
anything over a few hundred tracks in the background and tell the person how long to expect.

## Step 5 — Report honestly

The script prints, per playlist: Spotify total, unavailable on Spotify, matched, unmatched,
duplicates collapsed, and the number of tracks that actually landed. Relay those numbers and
explain any gap instead of saying "all copied":

- **Duplicates collapsed** — the same song saved twice from different releases becomes one
  track on YouTube Music. Name a few if the person asks.
- **Unavailable** — podcast episodes and tracks Spotify no longer serves.
- **Unmatched** — listed in `~/spotify-transfer/unmatched - <name>.txt`; the person can
  search and add those by hand.
- **A match is not a guarantee of the right version.** Suggest skimming for live cuts or
  remasters, especially for obscure tracks.

Give the playlist links from the output.

## Wrap up

- Suggest keeping Spotify until they have looked the result over. Downgrading to free keeps
  the Spotify library intact if a re-run is needed.
- `sp2ytm forget` deletes the stored credentials.
- If secrets were pasted into the conversation: the Spotify client secret can be rotated
  (or the app deleted) in the developer dashboard, and signing out of YouTube Music in that
  browser invalidates the cookie.

## If the script breaks

Both services change their private or public response formats without notice; the script
already tolerates the ones seen so far (Spotify's `tracks`→`items` / `track`→`item` rename,
YouTube Music results without `artists` or `videoId`). When something new breaks:

- Read the traceback, print the keys of the offending response, and patch the small helper
  involved (`_inner_track`, `fetch_playlist`, `best_match`) rather than working around it.
- A 401/403 from YouTube Music means the cookie expired: redo Step 2.
- "Insufficient client scope" from Spotify means the token predates a scope: redo the
  consent in Step 1.
- Progress is never lost: matches are cached in `~/.config/sp2ytm/match-cache.json`.
