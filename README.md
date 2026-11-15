# efimskills

Skills for Claude Code and other agents that load `SKILL.md` folders.

| Skill | What it does |
|---|---|
| [spotify-to-ytmusic](skills/spotify-to-ytmusic/SKILL.md) | Migrates Spotify liked songs and playlists to YouTube Music: recreates Liked Songs as likes (same order) and copies playlists as private playlists. |

## Install

Clone the repo and link the skill you want into your skills directory:

```bash
git clone https://github.com/efimsky/efimskills.git
ln -s "$PWD/efimskills/skills/spotify-to-ytmusic" ~/.claude/skills/spotify-to-ytmusic
```

Then ask Claude something like "move my Spotify liked songs to YouTube Music".

## spotify-to-ytmusic

The skill bundles `scripts/sp2ytm.py`, which can also be used on its own:

```bash
python3 -m venv ~/.config/sp2ytm/venv
~/.config/sp2ytm/venv/bin/pip install spotipy ytmusicapi
~/.config/sp2ytm/venv/bin/python skills/spotify-to-ytmusic/scripts/sp2ytm.py -h
```

It needs Python 3.10+, a Spotify Premium account (Spotify restricts its API otherwise) and a
browser signed in to YouTube Music. Credentials are stored only in `~/.config/sp2ytm/`.
`SKILL.md` documents the full workflow.
