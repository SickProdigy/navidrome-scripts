import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from navidrome_scripts import library_review as review


class LibraryReviewTests(unittest.TestCase):
    def row(self, path='/music/Artist/Album/CD01/song.flac', **overrides):
        row = dict(kind='unmapped', artist='Artist', album='Album', path=path,
                   release_group_mbid='', release_mbid='', reason='unmapped', tag_read_error='')
        row.update(overrides)
        return row

    def test_groups_discs_but_preserves_separate_editions(self):
        rows = [self.row(), self.row('/music/Artist/Album/CD02/second.flac'),
                self.row('/music/Artist/Album deluxe/song.flac', release_mbid='different')]
        groups = review.group_rows(rows)
        self.assertEqual(sorted(len(g['files']) for g in groups), [1, 2])

    def test_date_seeds_do_not_flag_unknown_year_zero(self):
        good = dict(path='Music-Library/Artist/Album/song.flac', year=0, original_year=0)
        bad = dict(good, path='Music-Library/Artist/Album (1)/song.flac')
        rows = review.add_date_seeds([good, bad])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['path'], '/music/Artist/Album (1)/song.flac')

    def test_unreadable_file_is_visible_not_treated_as_missing_from_mb(self):
        with patch.object(review, 'read_tags', side_effect=ValueError('unreadable')):
            row = review.make_unmapped(dict(path='/music/Artist/album/track.flac', id=7), '/music')
        self.assertIn('unreadable', row['tag_read_error'])
        self.assertEqual(row['album'], '')
        self.assertEqual(row['track_file_id'], 7)

    def test_music_root_escape_rejected(self):
        with self.assertRaises(ValueError):
            review.read_tags('/downloads/track.flac', Path('.'))

    def test_network_failure_is_not_a_missing_catalog_entry(self):
        group = review.group_rows([self.row()])[0]
        mb = Mock()
        mb.search.side_effect = requests.Timeout()
        review.check_catalog(group, mb, Mock(), 'us')
        self.assertIn('failed', group['catalog_status'])
        self.assertNotIn('Possible missing', group['catalog_status'])

    def test_apple_hit_is_only_a_possible_missing_entry(self):
        group = review.group_rows([self.row()])[0]
        mb, apple = Mock(), Mock()
        mb.search.return_value = {'release-groups': []}
        apple.get.return_value.json.return_value = {'results': [dict(
            collectionId=1, collectionName='Album', artistName='Artist',
            collectionViewUrl='https://music.apple.com/us/album/album/1', releaseDate='2020-01-01')]}
        review.check_catalog(group, mb, apple, 'us')
        self.assertIn('Possible missing', group['catalog_status'])
        self.assertTrue(group['apple_candidates'][0]['exact'])

    def test_report_escapes_names_and_retains_per_file_records(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report'
            groups = review.group_rows([self.row(artist='<script>bad</script>')])
            review.write_report(output, groups, 'test')
            self.assertNotIn('<script>bad</script>', (output / 'review.html').read_text(encoding='utf8'))
            payload = json.loads((output / 'review.json').read_text(encoding='utf8'))
            self.assertEqual(len(payload['groups'][0]['files']), 1)
            with self.assertRaises(FileExistsError):
                review.write_report(output, groups, 'test')

    def test_tagged_reference_prevents_missing_entry_classification(self):
        group = review.group_rows([self.row(release_mbid='existing-reference')])[0]
        mb, apple = Mock(), Mock()
        mb.search.return_value = {'release-groups': []}
        apple.get.return_value.json.return_value = {'results': [dict(
            collectionId=1, collectionName='Album', artistName='Artist')]}
        review.check_catalog(group, mb, apple, 'us')
        self.assertIn('Tagged MusicBrainz reference', group['catalog_status'])
        self.assertNotIn('Possible missing', group['catalog_status'])


if __name__ == '__main__':
    unittest.main()
