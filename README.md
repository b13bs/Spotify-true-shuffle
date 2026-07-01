# Spotify True Shuffle

## Human Words 🖐️

Spotify shuffle sucks. It's full of biases, it tries to predict your habits and what you may want to hear, it prioritizes data saving and caching.

For a few months, I experimented with a local music library, and I was surprisingly pleased with the true random that it offered. I rediscovered my liked songs that I had forgotten.

That's why this simple Python script [full of good nutrients](https://i.imgur.com/z2EYpEC.png) was created.

After the OAuth authorization flow, it reads your liked songs and creates a new playlist with your liked songs in random order. It's far from perfect, since it's     a static playlist in a defined order. But having multiple of those random playlists is good enough for me.

Note to the pedants out there: of course it's not true random like the name suggests. No, I didn't hook it up to a Geiger counter to measure radioactive decay. It relies on the OS cryptographically secure random source, which is good enough. 

*Gloire aux PRNGs qui font la job*

## Slop 🤖

### Description

Create private Spotify playlists from all liked songs using the operating
system cryptographic random source.

### Spotify setup

1. Create an app at <https://developer.spotify.com/dashboard>.
2. Add this redirect URI to the app:

   ```text
   http://127.0.0.1:8888/callback
   ```

3. Export credentials before running:

   ```bash
   export SPOTIFY_CLIENT_ID="your-client-id"
   export SPOTIFY_CLIENT_SECRET="your-client-secret"
   ```

The script stores only a local OAuth token cache beside the script, at
`.spotify-true-shuffle-token.json`, with owner-only permissions.

### Usage

First interactive run:

```bash
./spotify-true-shuffle.py --action create
```

List playlists whose names contain `True random`:

```bash
./spotify-true-shuffle.py --action list
```

Cron-friendly run with no stdout:

```bash
./spotify-true-shuffle.py --action create --quiet --log-level INFO
```

Logs are written to `spotify-true-shuffle.log` by default. Playlists are private
and named with an incrementing prefix like `🎲 True random #1`.

Stdout stays concise for interactive use. Use `--log-level DEBUG` when you want
detailed request, page, and batch progress in `spotify-true-shuffle.log`.

### Troubleshooting

If Spotify returns `HTTP 403` while creating the playlist, check the app in the
Spotify Developer Dashboard. Development-mode apps require the app owner to have
Spotify Premium, and every authenticated user must be added in the app's Users
Management allowlist.

If a run failed after creating an empty playlist, delete that empty playlist by
hand in Spotify before running again.

The script verifies that newly created playlists report as private before adding
tracks. If Spotify still reports the playlist as public after retries, the run
fails and leaves the empty playlist for manual deletion.
