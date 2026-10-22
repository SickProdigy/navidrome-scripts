# Changes

## 0.14.0 — 2026-10-22

- Add reviewed indexed-duplicate cleanup through official Navidrome backups/remap, with explicit library roots and alternate-encode approval, all-user reference checks and retained quarantine/recovery files.
- Refuse overlapping annotation/bookmark histories and file-backed/smart playlist cases rather than silently discard personal data.
- Add reviewed rename rollback plans, original-path restoration from verified backups and a scoped Lidarr rescan without artist creation.
- Reconcile interrupted copies and queued/unknown rescan results without repeated submissions; verify track assignments even when Lidarr assigns new file IDs.
- Require a post-rollback full Navidrome scan and check current personal data and playlists; prevent forward rename replay after rollback starts.

## 0.13.1 — unreleased

- Include missing-file identities in administrator absence checks so retained ratings/history cannot be mistaken for an unindexed copy.
- Validate native pagination totals independently for active and missing records.
- Carry configured music-root mappings through arr-scripts planning and scheduled review instead of assuming every API path starts with /music.

## 0.13.0 — unreleased

- Add reviewed one-album Lidarr metadata refresh plans, file/sidecar backups and post-refresh date reconciliation.
- Refuse conflicting artist/edition proposals, preserve files, distinguish stale/unknown upstream dates and reconcile queued or uncertain requests without blind retries.
- Add explicit arr-scripts refresh plan/application actions; no scheduled metadata mutation.
- Normalize native Python's CRLF output at the arr-scripts adapter boundary so Git Bash does not forward invalid HTTP headers.

## 0.12.0 — unreleased

- Add optional Apple storefront date context to exact-release date plans, with complete track-count/position/credit/duration comparisons.
- Link to review the existing MusicBrainz release and release group. External dates never become automatic write authority.
- Cache storefront results per release, pace requests and distinguish lookup failures from limited searches with no candidates.

## 0.11.1 — unreleased

- Record tag-repair completion in the existing library lock file, independent of preserved audio mtimes.
- Require a newer completed full Navidrome scan before renaming; unfinished repairs must be resumed or rolled back first.
- Keep resumed empty journals usable and include rollback in the scan handoff.

## 0.11.0 — unreleased

- Export the matched Beets proposal and add reviewed missing-core-tag plans with independent exact-release/track evidence checks.
- Add staged tag application, full-file backups, resumable journals and exact rollback while preserving dates, unrelated tags, artwork, lyrics, ReplayGain and mtimes.
- Handle formatted Beets exports without splitting artist names; retain conflicting existing values for review.
- Add explicit arr-scripts tag plan/apply/rollback actions; scheduled jobs still do not apply metadata plans automatically.
- Resolve physical paths through Navidrome's native API for preservation checks, duplicate review and M3U export; Subsonic virtual paths are not treated as disk filenames.
- Add an opt-in isolated Navidrome scan integration test covering personal data, playlist membership, real M3U paths and missing-file detection.

## 0.10.0 — unreleased

- Add per-album Lidarr rename previews, explicit scoped application and persistent command reconciliation.
- Back up audio and sidecars; move artwork/lyrics to reviewed destinations without overwriting conflicting files.
- Require separate pre/post full Navidrome scans and verify persistent IDs, personal data, playlists, file bytes and timestamps.
- Add optional arr-scripts rename plan/apply/verify commands; no scheduled renaming.

## 0.9.0 — unreleased

- Add exact-release date evidence and HTML repair previews, preserving edition/original distinctions and source precision.
- Add date-only writes through Beets' MediaFile backend, staged validation, source rechecks and exact rollback.
- Add separate arr-scripts date plan/apply/rollback commands; application requires Lidarr tag writing disabled and File Date=None.
- Verify decoded audio, artwork, lyrics, ReplayGain and timestamps across MP3, FLAC, M4A, Opus and Ogg fixtures.

## 0.8.0 — unreleased

- Add read-only duplicate HTML/JSON reports distinguishing exact bytes from shared recording IDs.
- Add explicit quarantine/restore plans for unmapped, unindexed byte-identical copies while preserving the mapped/indexed keeper.
- Check complete administrator index coverage, aliases, sidecars, assignments and personal references; refuse ambiguous or indexed-copy cleanup.
- Add per-account API preservation snapshots requiring a newly completed full scan for verification.

## 0.7.0 — unreleased

- Add explicit metadata-handoff approval, one-album workers, read-only audit scheduling and persistent per-step results.
- Isolate Beets previews; verify ReplayGain repair and report lyrics enqueue separately from embedding completion.
- Detect extra audio, mapping changes, altered files and incompatible settings; resume completed steps and bound failed gain retries.
- Add disabled-by-default arr-scripts adapter/service with a user-selected published package pin.

## 0.6.0 — unreleased

- Add scoped unmapped-file import plans with exact release/track identity checks, duplicate and artist-folder guards, and separate reviewed catalog additions.
- Reconcile queued/unknown import outcomes without repeating commands; verify original file bytes and timestamps and produce a separate metadata-processing handoff.
- Block same-name artists with conflicting catalog identities and respect import-list exclusions.

## 0.5.0 — unreleased

- Add Spotify playlist preview, reviewed Navidrome synchronization, and local M3U8 export.
- Match exact title/artist/duration with album or ISRC evidence; report ambiguous copies and unresolved items.
- Preserve playlist order and repeated tracks, retain existing destination IDs, save prior contents, and reject stale previews.
- Recover uncertain creation responses without blindly creating another playlist; restore existing playlist contents after review.

## 0.4.0 — unreleased

- Add embedded-date audit with progress, errors and elapsed time.
- Prepare reviewed plans to remove invalid year-0/1 placeholders without inventing dates.
- Verify file hashes, preserve unrelated tags and mtime, keep full-file backups, and support rollback and resumed application.
- Refuse overlapping repairs, changed files, path escapes and hardlinked files.

## 0.3.0 — packaging foundation (unreleased)

- Move artist rating updates into an installable package with console and module commands.
- Move read-only Lidarr library review from musicbrainz-helper into this repository.
- Add Docker usage, tests and a release workflow.

Minor versions introduce substantial features. Patch versions fix smaller defects.
