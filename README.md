# navidrome-scripts

Tooling for interacting with a Navidrome instance. Includes a script to
change track ratings for a particular artist through Navidrome's
Subsonic-compatible API.

## update-artist-track-ratings.py

- Purpose: find tracks for an artist that have a given rating (default 5)
  and set them to a new rating (default 2).
- Artist input may be either a human-readable artist name or a Navidrome
  artist id. Id-like values are detected automatically; use `--artist-id`
  to force id mode.
- Updates are flushed in small batches while scanning, so interrupted runs
  can be rerun; already-changed tracks no longer match `FROM_RATING`.
- Every run writes a timestamped log under `logs/` by default.
- Use `--dry-run` to preview changes before writing ratings.
- Use `--max-albums N` with an artist id for a quick smoke test against
  only the first N albums.

Example:

```bash
python update-artist-track-ratings.py --env-file .env --dry-run
```

Quick smoke test with tiny batches:

```bash
python update-artist-track-ratings.py --env-file .env --dry-run --max-albums 2 --batch-size 3
```

Real update after reviewing the dry-run:

```bash
python update-artist-track-ratings.py --env-file .env --batch-size 100
```

## .env support and auth methods

The script can load defaults from a `.env` file. Either pass `--env-file` or
place a `.env` in the working directory. CLI args override environment values.

Supported variables:

- `BASE_URL` - Navidrome base URL
- `USERNAME` - Navidrome/Subsonic username
- `PASSWORD` - Navidrome/Subsonic password
- `TOKEN` or `API_TOKEN` - optional Subsonic JWT token if your setup exposes one
- `ARTIST` - artist name or internal artist id
- `FROM_RATING` - default source rating (default 5)
- `TO_RATING` - default destination rating (default 2)

Example `.env`:

```env
BASE_URL=http://localhost:4533
USERNAME=your_username
PASSWORD=your_password
ARTIST=3Lu5a2hhygk22VnEcfKkwY
FROM_RATING=5
TO_RATING=2
```

The script prefers `PASSWORD` when both password and token are provided.
