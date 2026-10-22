import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from mutagen.flac import FLAC

from navidrome_scripts import dates,tag_repair as tags
import test_unmapped as fixtures
from test_date_evidence import catalog


def exported(path):
    return dict(path=str(path),title='Song',artist='Artist',artists=['Artist'],album='Album',albumartist='Artist',albumartists=['Artist'],
        track=1,disc=1,tracktotal=1,disctotal=1,mb_albumid=fixtures.RELEASE,mb_releasegroupid=fixtures.GROUP,
        mb_trackid=fixtures.RECORDING,mb_releasetrackid='release-track',mb_artistids=[fixtures.ARTIST],mb_albumartistids=[fixtures.ARTIST])


class TagRepairTests(unittest.TestCase):
    def setUp(self):
        fixtures.UnmappedTests.setUp(self)
        self.client.mapped[6]=dict(id=6,path='/music/Artist/Album/song.flac',albumId=2)
        self.output=Path(self.temp.name)/'tags.json'
        self.catalog=Mock();self.catalog.lookup_release.return_value=catalog()
        self.export_changes={};self.commands=[]

    def runner(self,command,log,env,**kwargs):
        self.commands.append(command)
        if 'list' in command:
            return fixtures.RELEASE+'\n'
        if 'export' in command:
            Path(command[command.index('-o')+1]).write_text(json.dumps([dict(exported(self.path),**self.export_changes)]))
        return ''

    def plan(self):
        return tags.make_plan(self.client,self.root,2,self.output,catalog=self.catalog,run=self.runner)

    def apply(self,payload):
        return tags.apply(self.client,self.root,payload,self.output,self.journal,self.catalog)

    def test_plan_reads_exact_beets_export_and_preserves_original(self):
        before=self.path.read_bytes();payload=self.plan()
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(len(payload['entries']),1)
        self.assertEqual(payload['entries'][0]['targets']['albumartist'],'Artist')
        self.assertNotIn('artist',payload['entries'][0]['targets'])
        self.assertEqual([c[c.index('export')+1] for c in self.commands if 'export' in c],['-l'])
        self.assertTrue(self.output.with_suffix('.html').exists())

    def test_missing_tags_apply_resume_and_exact_rollback(self):
        before=self.path.read_bytes();mtime=self.path.stat().st_mtime_ns
        payload=self.plan();self.assertEqual(self.apply(payload),0)
        saved=tags.semantic(self.path)
        self.assertEqual(saved['albumartist'],'Artist')
        self.assertEqual(saved['mb_releasetrackid'],'release-track')
        self.assertEqual(saved['mb_artistids'],[fixtures.ARTIST])
        self.assertEqual(self.path.stat().st_mtime_ns,mtime)
        self.assertEqual(FLAC(self.path)['lyrics'],['keep these lyrics'])
        self.assertEqual(FLAC(self.path)['originaldate'],['0001-01-01'])
        repaired=self.path.read_bytes();self.apply(payload)
        self.assertEqual(self.path.read_bytes(),repaired)
        dates.rollback(self.root,self.journal)
        self.assertEqual(self.path.read_bytes(),before)

    def test_existing_conflicting_values_are_retained_and_reported(self):
        audio=FLAC(self.path);audio['album']='Custom display album';audio.save()
        payload=self.plan()
        self.assertIn('album',payload['unresolved'][0]['fields'])
        self.apply(payload)
        self.assertEqual(FLAC(self.path)['album'],['Custom display album'])

    def test_wrong_beets_recording_or_edition_cannot_produce_repairs(self):
        self.export_changes['mb_trackid']='wrong-recording'
        payload=self.plan()
        self.assertEqual(payload['entries'],[])
        self.assertIn('mapping',payload['unresolved'][0]['reason'])

    def test_changed_catalog_and_edited_plan_stop_before_writes(self):
        payload=self.plan();before=self.path.read_bytes()
        payload['entries'][0]['targets']['artist']='Different artist'
        self.output.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError,'missing fields'):
            self.apply(payload)
        self.assertEqual(self.path.read_bytes(),before)
        payload['entries'][0]['targets'].pop('artist');self.output.write_text(json.dumps(payload))
        self.catalog.lookup_release.return_value['title']='Different album'
        with self.assertRaisesRegex(ValueError,'exact catalog'):
            self.apply(payload)
        self.assertEqual(self.path.read_bytes(),before)

    def test_shared_name_conflicting_artist_catalog_stays_unresolved(self):
        self.catalog.lookup_release.return_value['release-group']['artist-credit']=[dict(artist=dict(id='other-person'))]
        payload=self.plan()
        self.assertEqual(payload['entries'],[])
        self.assertIn('artists conflict',payload['unresolved'][0]['reason'])

    def test_extra_unmapped_file_and_new_lidarr_tag_writes_are_blocked(self):
        self.client.write_tags='newFiles'
        with self.assertRaisesRegex(ValueError,'Never'):
            self.plan()
        self.client.write_tags='no'
        self.path.with_name('other.flac').write_bytes(self.path.read_bytes())
        with self.assertRaisesRegex(ValueError,'extra/unmapped'):
            self.plan()

    def test_stage_rejects_backend_that_changes_lyrics_before_touching_original(self):
        payload=self.plan();before=self.path.read_bytes();write=tags.write_fields
        def destructive(path,entry):
            write(path,entry)
            audio=FLAC(path);audio['lyrics']='unapproved';audio.save()
        with patch.object(tags,'write_fields',side_effect=destructive):
            with self.assertRaisesRegex(ValueError,'unapproved'):
                self.apply(payload)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse(self.journal.exists())

    def test_original_write_failure_restores_exact_backup(self):
        payload=self.plan();before=self.path.read_bytes();write=tags.write_fields
        def interrupted(path,entry):
            write(path,entry)
            if path==self.path:
                raise OSError('interrupted original write')
        with patch.object(tags,'write_fields',side_effect=interrupted):
            with self.assertRaises(OSError):
                self.apply(payload)
        self.assertEqual(self.path.read_bytes(),before)
        self.apply(payload)
        self.assertEqual(tags.semantic(self.path)['albumartist'],'Artist')

    def test_moved_or_changed_files_and_changed_edition_require_new_preview(self):
        payload=self.plan();before=self.path.read_bytes()
        self.client.album['releases'][0]['foreignReleaseId']='another-edition'
        with self.assertRaisesRegex(ValueError,'edition changed'):
            self.apply(payload)
        self.client.album['releases'][0]['foreignReleaseId']=fixtures.RELEASE
        self.path.write_bytes(before+b'changed')
        with self.assertRaisesRegex(ValueError,'file changed'):
            self.apply(payload)

    def test_unknown_or_repeated_recordings_are_not_guessed(self):
        audio=FLAC(self.path);del audio['title'];del audio['tracknumber'];audio.save()
        response=self.catalog.lookup_release.return_value
        response['media'][0]['tracks'].append(dict(response['media'][0]['tracks'][0],number='2',id='other-release-track'))
        payload=self.plan();self.assertEqual(payload['entries'],[])
        self.assertIn('no unique',payload['unresolved'][0]['reason'])

    def test_title_and_position_can_be_filled_only_with_independent_recording_id(self):
        audio=FLAC(self.path);del audio['title'];del audio['tracknumber'];audio.save()
        payload=self.plan()
        self.assertEqual(payload['entries'][0]['targets']['title'],'Song')
        self.assertEqual(payload['entries'][0]['targets']['track'],1)
        self.apply(payload)
        self.assertEqual(tags.semantic(self.path)['title'],'Song')

    def test_actual_beets_export_format_uses_padded_numbers_and_joined_lists(self):
        self.export_changes.update(track='01',disc='01',tracktotal='01',disctotal='01',artists='Artist',albumartists='Artist',
            mb_artistids=fixtures.ARTIST,mb_albumartistids=fixtures.ARTIST)
        payload=self.plan()
        self.assertEqual(payload['entries'][0]['targets']['tracktotal'],1)
        self.assertEqual(payload['entries'][0]['targets']['artists'],['Artist'])
        self.apply(payload)

    def test_artist_names_containing_delimiters_are_compared_without_splitting(self):
        expected=exported(self.path)
        expected['artists']=['Name; With Semicolon','Other Artist']
        row=dict(expected,artists='Name; With Semicolon; Other Artist')
        self.assertEqual(tags.beets_values(row,expected)['artists'],expected['artists'])

    def test_multiple_conflicting_identifying_tag_values_stay_for_review(self):
        audio=FLAC(self.path);audio['musicbrainz_albumid']=[fixtures.RELEASE,'other-edition'];audio.save()
        payload=self.plan();self.assertEqual(payload['entries'],[])
        self.assertIn('conflicting identifying',payload['unresolved'][0]['reason'])

    def test_edited_duration_is_not_accepted_as_audio_evidence(self):
        payload=self.plan();payload['entries'][0]['duration_ms']+=2000
        self.output.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError,'duration differs'):
            self.apply(payload)

    def test_all_complete_fields_produce_a_noop_plan(self):
        audio=tags.media(self.path);audio.update({k:v for k,v in exported(self.path).items() if k in tags.FIELDS});audio.save()
        payload=self.plan();self.assertEqual(payload['entries'],[])
        self.assertEqual(self.apply(payload),0);self.assertFalse(self.journal.exists())


class RealFormatTagTests(unittest.TestCase):
    def test_five_formats_preserve_audio_personal_metadata_and_exact_rollback(self):
        import base64
        import subprocess
        import mediafile
        import imageio_ffmpeg
        from test_renames import RenameLidarr
        picture=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a8ukAAAAASUVORK5CYII=')
        ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
        for suffix,codec in {'.mp3':'libmp3lame','.flac':'flac','.m4a':'aac','.opus':'libopus','.ogg':'libvorbis'}.items():
            with self.subTest(format=suffix),tempfile.TemporaryDirectory() as directory:
                base=Path(directory);root=base/'music';folder=root/'Artist/Album';folder.mkdir(parents=True)
                path=folder/('song'+suffix)
                subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t','1','-c:a',codec,str(path)],check=True)
                audio=mediafile.MediaFile(str(path),id3v23=suffix=='.mp3')
                audio.title='Song';audio.artist='Artist';audio.album='Album';audio.track=1;audio.disc=1
                audio.mb_albumid=fixtures.RELEASE;audio.mb_releasegroupid=fixtures.GROUP;audio.mb_trackid=fixtures.RECORDING
                audio.year=2003;audio.lyrics='Keep these lyrics';audio.rg_track_gain=-7.5;audio.rg_album_gain=-7.5
                audio.rg_track_peak=0.8;audio.rg_album_peak=0.8;audio.comments='Keep personal notes'
                audio.images=[mediafile.Image(data=picture,type=mediafile.ImageType.front)];audio.save()
                client=RenameLidarr(root);client.mapped[6]['path']='/music/Artist/Album/'+path.name
                source=Mock();source.lookup_release.return_value=catalog()
                output=base/'plan.json';journal=base/'repair.jsonl'
                def runner(command,log,env,**kwargs):
                    if 'list' in command:
                        return fixtures.RELEASE+'\n'
                    if 'export' in command:
                        Path(command[command.index('-o')+1]).write_text(json.dumps([exported(path)]))
                    return ''
                before=path.read_bytes();mtime=path.stat().st_mtime_ns
                decoded=lambda:subprocess.check_output([ffmpeg,'-v','error','-i',str(path),'-map','0:a:0','-f','hash','-hash','sha256','-'])
                samples=decoded()
                payload=tags.make_plan(client,root,2,output,catalog=source,run=runner)
                self.assertEqual(len(payload['entries']),1)
                tags.apply(client,root,payload,output,journal,source)
                self.assertEqual(decoded(),samples)
                saved=mediafile.MediaFile(str(path))
                self.assertEqual(saved.lyrics,'Keep these lyrics');self.assertEqual(saved.comments,'Keep personal notes')
                self.assertEqual(saved.images[0].data,picture);self.assertAlmostEqual(saved.rg_track_gain,-7.5)
                self.assertEqual(saved.albumartist,'Artist');self.assertEqual(path.stat().st_mtime_ns,mtime)
                self.assertEqual(saved.year,2003)
                dates.rollback(root,journal);self.assertEqual(path.read_bytes(),before)


if __name__=='__main__':
    unittest.main()
