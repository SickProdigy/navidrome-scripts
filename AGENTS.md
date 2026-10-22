# Shared checkout

- Preserve the local branch. Use a temporary clone for PR work; leave uncertain changes uncommitted for review.
- Keep implementation in navidrome_scripts/, with console commands and executable modules; no duplicate root wrappers.
- Keep this package independent of SonicRatings and musicbrainz-helper. arr-scripts may invoke installed commands.
- Library review is read-only. Unmapped does not mean missing from MusicBrainz. Verify IDs, edition and track lists before proposing repairs.
- Never open live service SQLite databases over network shares. Use APIs or offline exports.
- Keep tag changes and file moves separate, preview-first, and preserve originals and file modification times.
- Use semantic versioning: minor bumps for substantial features and patch bumps for small fixes. Keep unreleased work clearly marked until published.
