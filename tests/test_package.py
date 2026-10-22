import subprocess
import sys
import unittest
from navidrome_scripts.catalog import lucene_quote

class PackageTests(unittest.TestCase):
    def test_modules_show_help(self):
        for module in ('artist_ratings', 'library_review', 'dates', 'playlists', 'unmapped', 'maintenance', 'duplicates', 'navidrome_guard', 'renames', 'tag_repair'):
            result = subprocess.run([sys.executable, '-m', 'navidrome_scripts.' + module, '--help'], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
    def test_query_escaping(self):
        self.assertEqual(lucene_quote('a"b'), '\"a\\"b\"')
