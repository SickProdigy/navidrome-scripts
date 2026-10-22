import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from mutagen.flac import FLAC

from navidrome_scripts import dates, date_evidence as evidence
import test_unmapped as fixtures


def catalog(date='2003', original='1985-05'):
    credit=[dict(name='Artist',artist=dict(id=fixtures.ARTIST,name='Artist'))]
    return dict(id=fixtures.RELEASE,title='Album',date=date,**{'artist-credit':credit,
        'release-group':dict(id=fixtures.GROUP,**{'first-release-date':original,'artist-credit':credit}),
        'media':[dict(position=1,tracks=[dict(id='release-track',number='1',title='Song',length=1000,
            recording=dict(id=fixtures.RECORDING,title='Song',length=1000))])]})


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        fixtures.UnmappedTests.setUp(self)
        self.audit=Path(self.temp.name)/'dates.jsonl'
        self.output=Path(self.temp.name)/'date-plan.json'
        self.client=Mock();self.client.lookup_release.return_value=catalog()

    def plan(self,fill_missing=False):
        dates.audit(self.root,self.audit)
        return evidence.make_plan(self.root,self.audit,self.output,self.client,fill_missing)

    def test_exact_edition_plan_keeps_valid_release_date_and_original_precision(self):
        before=self.path.read_bytes();payload=self.plan()
        self.assertEqual(payload['entries'][0]['targets'],{'original_date':'1985-05'})
        self.assertEqual(self.path.read_bytes(),before)
        self.client.lookup_release.assert_called_once_with(fixtures.RELEASE)

    def test_apply_and_rollback_preserve_unrelated_tags_mtime_and_exact_original(self):
        before=self.path.read_bytes();mtime=self.path.stat().st_mtime_ns
        self.plan();dates.apply_plan(self.root,self.output,self.journal,self.client)
        self.assertEqual(evidence.semantic_dates(self.path),dict(release_date='2003',original_date='1985-05'))
        self.assertEqual(self.path.stat().st_mtime_ns,mtime)
        self.assertEqual(FLAC(self.path)['lyrics'],['keep these lyrics'])
        dates.apply_plan(self.root,self.output,self.journal,self.client)
        dates.rollback(self.root,self.journal)
        self.assertEqual(self.path.read_bytes(),before)

    def test_source_year_only_is_never_expanded_to_january_first(self):
        self.client.lookup_release.return_value=catalog(original='1985')
        self.plan();dates.apply_plan(self.root,self.output,self.journal,self.client)
        self.assertEqual(evidence.semantic_dates(self.path)['original_date'],'1985')

    def test_blank_catalog_dates_only_allow_placeholder_cleanup(self):
        self.client.lookup_release.return_value=catalog(date='',original='')
        payload=self.plan()
        self.assertEqual(payload['entries'][0]['targets'],{})
        self.assertTrue(payload['unresolved'])
        dates.apply_plan(self.root,self.output,self.journal,self.client)
        self.assertEqual(FLAC(self.path)['date'],['2003'])
        self.assertNotIn('originaldate',FLAC(self.path))

    def test_conflicting_artist_or_track_identity_stays_unresolved(self):
        response=catalog();response['release-group']['artist-credit']=[dict(artist=dict(id='other'))]
        self.client.lookup_release.return_value=response
        payload=self.plan()
        self.assertEqual(payload['entries'],[])
        self.assertIn('credits conflict',payload['unresolved'][0]['reason'])

    def test_catalog_changes_after_review_block_writes(self):
        before=self.path.read_bytes();self.plan()
        self.client.lookup_release.return_value=catalog(original='1986')
        with self.assertRaisesRegex(ValueError,'evidence changed'):
            dates.apply_plan(self.root,self.output,self.journal,self.client)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse(self.journal.exists())

    def test_edited_plan_cannot_invent_more_precision_than_source(self):
        self.client.lookup_release.return_value=catalog(original='1985')
        payload=self.plan();payload['entries'][0]['targets']['original_date']='1985-01-01'
        self.output.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError,'precision/value'):
            dates.apply_plan(self.root,self.output,self.journal,self.client)

    def test_valid_conflicting_date_is_not_overwritten(self):
        audio=FLAC(self.path);audio['originaldate']='1990';audio.save()
        payload=self.plan()
        self.assertEqual(payload['entries'][0]['targets'],{})
        self.assertTrue(any('conflicts' in r['reason'] for r in payload['unresolved']))
        dates.apply_plan(self.root,self.output,self.journal,self.client)
        self.assertEqual(FLAC(self.path)['originaldate'],['1990'])

    def test_missing_dates_are_only_filled_with_explicit_option(self):
        audio=FLAC(self.path);del audio['originaldate'];del audio['originalyear'];audio.save()
        # No invalid dates remain, so only a year-1 folder supplies an audit row.
        renamed=self.path.parent.with_name('Album (1)');self.path.parent.rename(renamed);self.path=renamed/self.path.name
        payload=self.plan(fill_missing=True)
        self.assertEqual(payload['entries'][0]['targets'],{'original_date':'1985-05'})

    def test_stage_checks_reject_unrelated_tag_changes_before_any_original_write(self):
        self.plan();before=self.path.read_bytes()
        original=evidence.write_dates
        def bad(path,entry):
            original(path,entry)
            audio=FLAC(path);audio['lyrics']='lost lyrics';audio.save()
        with patch.object(evidence,'write_dates',side_effect=bad):
            with self.assertRaisesRegex(ValueError,'unrelated metadata'):
                dates.apply_plan(self.root,self.output,self.journal,self.client)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse(self.journal.exists())

    def test_optional_backend_missing_leaves_original_untouched(self):
        self.plan();before=self.path.read_bytes()
        with patch.dict(sys.modules,{'mediafile':None}):
            with self.assertRaisesRegex(ValueError,'tags extra'):
                dates.apply_plan(self.root,self.output,self.journal,self.client)
        self.assertEqual(self.path.read_bytes(),before)


try:
    import mediafile
    import imageio_ffmpeg
    REAL_AUDIO=True
except ImportError:
    REAL_AUDIO=False


@unittest.skipUnless(REAL_AUDIO,'install tags and test extras for real audio date round-trips')
class RealDateAudioTests(unittest.TestCase):
    def test_date_only_writes_preserve_decoded_audio_artwork_lyrics_and_gain(self):
        codecs={'.mp3':'libmp3lame','.flac':'flac','.m4a':'aac','.opus':'libopus','.ogg':'libvorbis'}
        ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'music';root.mkdir()
            for suffix,codec in codecs.items():
                with self.subTest(format=suffix):
                    folder=root/codec;folder.mkdir();path=folder/('Song'+suffix)
                    subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t','1','-c:a',codec,str(path)],check=True)
                    audio=mediafile.MediaFile(str(path))
                    audio.artist='Artist';audio.albumartist='Artist';audio.title='Song';audio.album='Album';audio.track=1;audio.disc=1
                    audio.mb_albumid=fixtures.RELEASE;audio.mb_releasegroupid=fixtures.GROUP;audio.mb_trackid=fixtures.RECORDING
                    audio.mb_albumartistid=fixtures.ARTIST;audio.year=1;audio.month=1;audio.day=1
                    audio.original_year=1;audio.original_month=1;audio.original_day=1
                    audio.lyrics='Keep these lyrics';audio.rg_track_gain=-7.5;audio.rg_album_gain=-7.5
                    audio.rg_track_peak=0.8;audio.rg_album_peak=0.8
                    import base64
                    picture=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a8ukAAAAASUVORK5CYII=')
                    audio.images=[mediafile.Image(data=picture,type=mediafile.ImageType.front)];audio.save()
                    before=path.read_bytes();mtime=path.stat().st_mtime_ns
                    decoded=lambda: subprocess.check_output([ffmpeg,'-v','error','-i',str(path),'-map','0:a:0','-f','hash','-hash','sha256','-'])
                    samples=decoded()
                    report=Path(directory)/(codec+'.jsonl');plan=Path(directory)/(codec+'.json');journal=Path(directory)/(codec+'-journal.jsonl')
                    client=Mock();client.lookup_release.return_value=catalog(date='2003-06',original='1985')
                    dates.audit(folder,report);payload=evidence.make_plan(folder,report,plan,client)
                    self.assertEqual(len(payload['entries']),1)
                    dates.apply_plan(folder,plan,journal,client)
                    self.assertEqual(evidence.semantic_dates(path),dict(release_date='2003-06',original_date='1985'))
                    self.assertEqual(decoded(),samples)
                    self.assertEqual(path.stat().st_mtime_ns,mtime)
                    saved=mediafile.MediaFile(str(path))
                    self.assertEqual(saved.lyrics,'Keep these lyrics')
                    self.assertAlmostEqual(saved.rg_track_gain,-7.5)
                    self.assertEqual(saved.images[0].data,picture)
                    dates.rollback(folder,journal)
                    self.assertEqual(path.read_bytes(),before)
