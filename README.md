# navidrome-scripts

Tools for Navidrome ratings and music library review.

| Command | Purpose |
| --- | --- |
| `navidrome-artist-ratings` | Change one artist's tracks from a selected rating to another through the Subsonic API. |
| `navidrome-library-review` | Review Lidarr unmapped files and optional invalid-date exports without changing the library. |
| `navidrome-dates` | Audit embedded dates and review reversible date repairs. |
| `navidrome-playlists` | Match Spotify playlist tracks to your library and update a reviewed Navidrome playlist or export M3U8. |
| `navidrome-unmapped` | Plan and verify scoped, in-place Lidarr matching; separately review missing catalog additions. |
| `navidrome-maintenance` | Schedule read-only reviews and process explicitly approved metadata handoffs. |
| `navidrome-duplicates` | Review duplicate copies, quarantine extras and preserve indexed history. |
| `navidrome-guard` | Snapshot song identities and personal data, then verify preservation after a new full scan. |
| `navidrome-renames` | Review album renames and rollback, with backups and Navidrome history checks. |
| `navidrome-tags` | Review and reversibly fill missing core metadata from an exact Beets/catalog match. |

## Install

From this checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Edit `.env` for the command you want to use. All commands support `--help`.
Filesystem checks also use Navidrome's native API with the same credentials;
the base URL must expose `/auth` and `/api`, as well as `/rest`.
For automation such as arr-scripts, install a released package version and invoke
its console command; this package does not depend on the other music helpers.
Install the published package with `pip install 'navidrome-scripts[tags]==0.14.0'`.

## Artist track ratings

Accepts an artist name or Navidrome artist ID (`--artist-id` forces ID mode).
Preview first, then remove `--dry-run` to write ratings:

```bash
navidrome-artist-ratings --env-file .env --dry-run
navidrome-artist-ratings --env-file .env --batch-size 100
```

Use `--max-albums 2 --batch-size 3` with an artist ID for a small preview.
Updates are batched; rerunning skips tracks that no longer match `FROM_RATING`.
Each run writes a timestamped log under `logs/`.

## Library review

Set `LIDARR_URL` and `LIDARR_API_KEY` in `.env`, then give the local path that
corresponds to Lidarr's `/music`:

```bash
navidrome-library-review --library-root /path/to/Music-Library
```

Reports are saved as HTML, CSV and JSON under `logs/library-review/`.
Use `--check-catalog 10` to search MusicBrainz and Apple Music for the first ten
album groups. Spotify links are manual searches. Existing MusicBrainz IDs and
search results are candidates to review, not proof of the correct edition.

For offline input use `--unmapped-json files.json`. Optional `--date-seeds dates.json`
includes an offline date-audit export; this command never opens a service database.
It does not change tags, move files, import albums or submit catalog edits.
For confirmed MusicBrainz gaps, the separately installed musicbrainz-helper's
`musicbrainz-new-artist APPLE_ALBUM_URL` prepares editor forms for manual review.
Ambiguous catalog/import decisions and indexed-copy history migration still require manual review.

## Duplicate review

Start with one album folder; audit produces JSON plus a readable HTML comparison:

```bash
navidrome-duplicates --library-root /music audit --path "Artist/Album" --output logs/duplicates.json
```

Recording IDs alone do not establish duplicate files. Different encodes, masters,
editions and tag variations remain for review. No keeper is automatically chosen.
After reviewing byte-identical copies, choose paths relative to the music root:

```bash
navidrome-duplicates --library-root /music plan --audit logs/duplicates.json \\
  --keep "Artist/Album/song.flac" --quarantine-file "Artist/Album/copy.flac" \\
  --quarantine-dir /quarantine --navidrome-prefix Music-Library --output logs/quarantine-plan.json
```

Use the prefix shown at the start of Navidrome's API paths; omit it when its paths
are already relative to your music root. Planning needs Lidarr and Navidrome
credentials. A complete administrator index is checked in pages, retaining only
selected paths. Active scans, incomplete coverage, multiple libraries, indexed
copies, mapped duplicates, hardlinks, unique lyrics and local playlist/cue files
require separate review. The keeper must already be mapped and indexed.

Run changes on the server with native library paths while scans/tag writers are
idle. Review the plan, then quarantine or restore:

```bash
navidrome-duplicates --library-root /music apply --plan logs/quarantine-plan.json --journal logs/quarantine-run.json
navidrome-duplicates --library-root /music restore --plan logs/quarantine-plan.json --journal logs/quarantine-run.json
```

Quarantine stays outside the library. Copies are hash-verified before their
original paths are removed; no service records, directories, artwork or sidecars
are deleted. A changed Navidrome state triggers restoration of original paths when
possible, with unresolved cases retained for review. Restore never overwrites new
content and retains quarantine backups. Lidarr may retain missing unmapped rows
until its next scoped artist rescan. This does not merge indexed song histories.

For **already indexed copies**, use the separate reviewed workflow below. It needs
Navidrome's native `missing fix` command (tested on 0.64.2) and a local/native backup
directory mapping. `roots.json` maps Navidrome library IDs to local filesystem roots,
for example `{"1":"/music"}`. `native-command.json` contains an argument array for
the official CLI, such as `["docker","exec","navidrome","/app/navidrome","--datafolder","/data"]`.

```bash
navidrome-duplicates --library-root /music indexed-plan --roots roots.json --keep-id KEEP_AUDIO_ID --quarantine-id OTHER_ID --native-command native-command.json --backup-root /path/to/navidrome/backups --native-backup-root /data/backups --quarantine-dir /quarantine/reviewed-pair-1 --output logs/indexed-plan.json
navidrome-duplicates --library-root /music indexed-apply --plan logs/indexed-plan.json --journal logs/indexed-run.json
```

Review `logs/indexed-plan.html` first. The selected audio is kept; the other ID is
retained by Navidrome's remap and both copies' playlist references follow it.
Overlapping user annotations/bookmarks and file-backed/smart playlists stay in
review. Different bytes require `--allow-alternate-encode`; different editions or
unverified track identities are refused. Backups include all users' private data.
Keep backups and quarantine outside every music library and source control.

After quarantine, complete a full Navidrome scan and repeat `indexed-apply`.
After remapping, complete another full scan and run `indexed-verify` with the same
plan/journal. `indexed-restore-files` restores quarantined audio without overwriting
new files and prints the separate **offline** database-recovery command. It never
restores the database while Navidrome is running.

## Verify Navidrome preservation

Capture a baseline before a reviewed repair, using your Navidrome song IDs:

```bash
navidrome-guard capture --song-id SONG_ID --output logs/navidrome-before.json
# After the repair and a completed full Navidrome scan:
navidrome-guard verify --snapshot logs/navidrome-before.json
```

This checks the same IDs/paths, ratings/stars, nondecreasing play history and visible
playlist IDs/ordered membership. It checks only the authenticated account; repeat
for other users when validating their data. A stale or still-running scan cannot
prove preservation. For a separately reviewed rename, `--path-map FILE.json` supplies
an explicit old-path to new-path mapping; the original song IDs must still survive.

## Match unmapped files

Start with one album folder. These commands use `LIDARR_URL` and `LIDARR_API_KEY`:

```bash
navidrome-unmapped --library-root /path/to/Music-Library plan --path "/music/Artist/Album" --output logs/import-plan.json
```

Review the plan. `ready` files match the exact tagged release, position, title,
duration and available recording IDs. Conflicting artists/editions, existing track
files and duplicate copies remain for review. Files must already be in the correct
Lidarr artist folder. This command will not move files into place.

Apply reviewed matches and verify their assignment, original bytes and timestamps:

```bash
navidrome-unmapped --library-root /path/to/Music-Library apply --plan logs/import-plan.json --journal logs/import-run.json
```

Queued commands can be checked by repeating the same command; imports are not
blindly reissued. Keep the plan and journal. Lidarr's File Date must be `None` and
tag writing must be `Never` or `For new downloads only`; existing files stay untouched.

For `catalog_missing` rows that include a `catalog_proposal`, explicitly run
`add-catalog --plan logs/import-plan.json --journal logs/catalog-run.json` with the
same `--library-root`. This only adds an unmonitored album to an existing artist,
without searching for downloads. After metadata refresh, generate a fresh import
plan. Excluded catalog entries and mismatched artist IDs are refused.

After verified mapping, a `.metadata-queue.json` handoff is saved beside the import
journal. Existing-file imports do not trigger ordinary download notifications;
the optional arr-scripts worker processes this handoff only after approval.

## Optional maintenance with arr-scripts

After publishing/installing the matching package release, set
`enableLibraryMaintenance="true"` and `navidromeScriptsVersion="X.Y.Z"` in
arr-scripts' `extended.conf`, then rerun container setup. The default is disabled;
no unreleased version is automatically installed or enabled.

The service generates a read-only unmapped-file report every seven days and
processes approved jobs one album at a time. Optional full date audits are off by
default. Review and import one album, then approve its metadata handoff:

```bash
bash /config/extended/LibraryMaintenance.bash --plan "/music/Artist/Album" /config/logs/import-plan.json
bash /config/extended/LibraryMaintenance.bash --import /config/logs/import-plan.json /config/logs/import-run.json
bash /config/extended/LibraryMaintenance.bash --approve /config/logs/import-run.metadata-queue.json
```

The worker checks the whole album folder for extra/unmapped audio before work.
Beets runs as a read-only exact-release preview with a separate scratch library;
use the separate reviewed `navidrome-tags` workflow below to apply tag changes. Missing
ReplayGain is repaired and verified, then missing lyrics are enqueued. Lyrics
completion remains in the lyrics worker; queueing does not mean embedding finished.
Retries preserve completed steps; settings changes pause incompatible approvals.

Logs: `/config/logs/library-maintenance.log`. Reports, approvals, step logs and
status files: `/config/extended/library-maintenance/`. This does not enable
MusicBrainz submissions, date repair, file moves or duplicate deletion. Stop other
tag writers while approving/processing a repair album.

## Missing metadata

After correcting an existing catalog entry, refresh that album's Lidarr metadata separately:

```bash
navidrome-unmapped --library-root /path/to/Music-Library refresh-plan --album-id 123 --output logs/refresh.json
# Review refresh.html, then:
navidrome-unmapped --library-root /path/to/Music-Library refresh --plan logs/refresh.json --journal logs/refresh-journal.json
```

This checks artist/edition identity and backs up mapped audio and sidecars before
submitting one metadata refresh. Repeat the same command to check a queued result.
Unknown or stale upstream dates remain for review; nothing invents a date or
automatically renames files. Lidarr tag writing must be `Never` and File Date `None`.

From this checkout, install `pip install -e '.[tags,matching]'`, then preview one mapped
album. Lidarr audio-tag writing must be `Never` and File Date must be `None`:

```bash
navidrome-tags --library-root /path/to/Music-Library plan --album-id 123 --output logs/tags.json
```

Open `logs/tags.html`. This checks Beets' exact-edition match against MusicBrainz
track identities and durations, then proposes missing titles, artist credits,
album names, track/disc numbers and MusicBrainz IDs. Existing values are retained;
conflicts remain for review. Dates use the separate date workflow below.

After review, with other tag writers stopped:

```bash
navidrome-tags --library-root /path/to/Music-Library apply --plan logs/tags.json --journal logs/tags-repair.jsonl
```

Every selected format is tested on a temporary copy before originals are touched.
Full-file backups and staging need roughly twice the selected files' size in free
space. Lyrics, artwork, ReplayGain, unrelated tags, paths and modification times
are checked. Repeat the same command to resume; altered files require review.
Supported formats are MP3, FLAC, M4A, Opus and Ogg Vorbis.

To restore unchanged repaired files exactly:

```bash
navidrome-tags --library-root /path/to/Music-Library rollback --journal logs/tags-repair.jsonl
```

Complete a full Navidrome scan after tags change and before planning any rename.
Use `navidrome-guard` snapshots to check personal data across the scan.

## Invalid dates

Audit without changing files, then generate and review a repair plan:

```bash
mkdir -p logs
navidrome-dates --library-root /path/to/Music-Library audit --output logs/dates.jsonl
navidrome-dates --library-root /path/to/Music-Library plan --lookup-dates --audit logs/dates.jsonl --output logs/date-plan.json
```

The audit records progress, errors and elapsed time. Review `date-plan.html` beside
the JSON plan. Exact release and track identities must match before MusicBrainz
dates can replace invalid values. Edition and original dates stay separate, and
year-only dates stay year-only. Missing or conflicting evidence remains for review;
invalid placeholders may be removed without inventing replacements. Omit
`--lookup-dates` for a removal-only plan. Folder renaming remains separate.

Add `--apple-review` to the lookup plan for storefront dates, track-list comparisons
and links to review the existing MusicBrainz entry (`--country us` selects the
storefront). These results are context for manual catalog corrections; they never
authorize a tag replacement themselves. Lookup failures and differing/incomplete
track lists remain visible. Storefront requests are paced and cached per release.

Install `pip install 'navidrome-scripts[tags]'` for replacement dates, then apply the
reviewed plan while other tag writers are stopped:

```bash
navidrome-dates --library-root /path/to/Music-Library apply --plan logs/date-plan.json --journal logs/date-repair.jsonl
```

This rechecks source evidence, verifies file hashes, tests replacement writes on
temporary copies, saves full-file backups alongside the journal, checks unrelated
tags, and preserves modification times. Allow roughly twice the selected files'
size for backups and staging. Hardlinked files require separate review. Repairs
share a library lock; repeating the same command skips completed files. An
interruption during an unconfirmed write requires backup review before continuing.

To restore the exact originals, provided files have not changed since repair:

```bash
navidrome-dates --library-root /path/to/Music-Library rollback --journal logs/date-repair.jsonl
```

Keep the plan, journal and backups together. Reconcile incorrect source metadata
before running another tagger, or it could write the placeholders again. File moves
and date corrections remain separate operations.

## Album folder and filename repairs

Correct the album's current Lidarr metadata first. Finish tag repairs and a full
Navidrome scan before changing paths. Then review one album's proposed names:

```bash
navidrome-renames --library-root /path/to/Music-Library plan --album-id 123 --navidrome-prefix Music-Library --output logs/rename-plan.json
```

Use the prefix shown in Navidrome's native filesystem paths; omit it if paths start directly with
the artist. Open `logs/rename-plan.html` to review audio, artwork and lyric paths.
Existing destination folders, extra unmapped audio and local playlist/cue files
require separate review. Both services' credentials belong in `.env`.
Use the same library root for repairs and renaming. The helper records completed
tag writes separately from file mtimes and refuses an older full scan; resume or
roll back an interrupted repair before renaming.

Apply only the reviewed plan, with other writers stopped and enough space for
full-file backups:

```bash
navidrome-renames --library-root /path/to/Music-Library apply --plan logs/rename-plan.json --journal logs/rename.json
```

This submits one scoped Lidarr command. Repeat that exact command to check a queued
or interrupted result; it does not submit another rename. Artwork and lyrics follow
the reviewed paths. Changed contents or timestamps stop the workflow for review.
Original bytes remain in `rename.json.backups`; the plan records their old paths.
After a full Navidrome scan, verify the forward rename:

```bash
navidrome-renames --library-root /path/to/Music-Library verify --plan logs/rename-plan.json --journal logs/rename.json
```

To undo a rename, review a rollback plan before restoring paths:

```bash
navidrome-renames --library-root /path/to/Music-Library rollback-plan --plan logs/rename-plan.json --journal logs/rename.json --output logs/rollback-plan.json
navidrome-renames --library-root /path/to/Music-Library rollback --plan logs/rollback-plan.json --journal logs/rollback.json
```

Open `logs/rollback-plan.html` first. Stop other writers and wait for Lidarr jobs
to finish. Rollback retains the backups, restores original paths and verifies a
scoped Lidarr rescan. Changed files stop for review. Repeat the same command to
resume a queued or interrupted rollback. After a new full Navidrome scan, run:

```bash
navidrome-renames --library-root /path/to/Music-Library rollback-verify --plan logs/rollback-plan.json --journal logs/rollback.json
```

Preservation checks cover the configured account. Other users need their own
`navidrome-guard` snapshots. A failed verification remains unresolved in the journal.

## Spotify playlists

Set `NAVIDROME_URL`, `NAVIDROME_USERNAME`, and `NAVIDROME_PASSWORD` in `.env`.
For Spotify, use a user OAuth access token (`SPOTIFY_ACCESS_TOKEN`) or
`SPOTIFY_CLIENT_ID` and `SPOTIFY_REFRESH_TOKEN` (plus `SPOTIFY_CLIENT_SECRET`
for a confidential client). Include `playlist-read-private` for private lists.
Client credentials alone do not provide user playlist access. Spotify currently
restricts playlist-item access to lists you own or collaborate on; see the
[Spotify API documentation](https://developer.spotify.com/documentation/web-api/reference/get-playlists-items).

```bash
navidrome-playlists plan --spotify-playlist PLAYLIST_URL --output logs/playlist-plan.json
# Review the JSON matches and unresolved items before applying:
navidrome-playlists apply --plan logs/playlist-plan.json
```

Repeat `plan` with a new output filename and `apply` to sync changes. The saved
state keeps the same Navidrome playlist ID. Order and repeated tracks are preserved;
ambiguous copies, unavailable items and missing songs require review. `--allow-partial`
explicitly permits omitting unresolved items; lookup errors always block updates.
To update an existing playlist, pass its `--playlist-id` when planning.

State, prior playlist contents and refreshed Spotify tokens are stored under
`logs/spotify-playlists/`; retain this directory between runs and keep it private.
`--state FILE` and `SPOTIFY_TOKEN_FILE` select separate locations for other accounts.
Unknown write outcomes remain pending until verified; rerun the same apply command
to recover rather than deleting state or creating another playlist.

For a local playlist, use `navidrome-playlists export --plan logs/playlist-plan.json
--output playlist.m3u8` (one command). Paths are relative to your music root; place
the resulting M3U8 there. Export reads Navidrome's native API using the same
credentials to resolve real filenames; virtual Subsonic paths are not exported.
`--source-json FILE` can replace `--spotify-playlist` when
planning from a complete API export. `restore --plan FILE` restores a previously
existing playlist's contents if nobody has edited it since synchronization.

## Validation

Install test dependencies with `pip install -e '.[tags,test]'`, then run
`python -m unittest discover -s tests`. The opt-in `tests/integration_navidrome.py`
also exercises real scans against an isolated local server: set
`NAVIDROME_TEST_EXE` to a trusted Navidrome binary and run the script with this
checkout on `PYTHONPATH`. It generates audio and a temporary database, checks
ratings/playlists across separate tag and move scans, and stops its own server.
Production music, accounts and databases are never used by this test.

With Docker running, `python tests/run_lidarr_sandbox.py` exercises the full
Lidarr/Navidrome workflow with generated audio in disposable containers. Results
go under `logs/nav-fixture-*`; the script removes its test services afterward.

## Run as Python modules

From the repository root, the same implementations run without console commands
once their dependencies are installed:

```bash
python -m navidrome_scripts.artist_ratings --help
python -m navidrome_scripts.library_review --help
python -m navidrome_scripts.dates --help
python -m navidrome_scripts.playlists --help
python -m navidrome_scripts.unmapped --help
python -m navidrome_scripts.maintenance --help
python -m navidrome_scripts.duplicates --help
python -m navidrome_scripts.navidrome_guard --help
python -m navidrome_scripts.renames --help
python -m navidrome_scripts.tag_repair --help
```

## Docker

```bash
docker build -t navidrome-scripts .
mkdir -p logs
docker run --rm --env-file .env \
  -v /path/to/Music-Library:/music:ro -v "$PWD/logs:/work/logs" \
  navidrome-scripts --library-root /music
```

Use a Lidarr URL reachable from the container; `localhost` refers to the container.
To run the ratings command instead, use `--entrypoint navidrome-artist-ratings`
before the image name, followed by `--dry-run` or other ratings options.

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
