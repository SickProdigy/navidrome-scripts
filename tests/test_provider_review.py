import json
from pathlib import Path
import unittest
from unittest.mock import Mock

import requests

from navidrome_scripts import provider_review as p,date_evidence,dates
from test_date_evidence import catalog
import test_unmapped as fixtures


def collection():
    return dict(wrapperType='collection',collectionId=123,artistName='Artist',collectionName='Album',
        releaseDate='2003-06-12T00:00:00Z',collectionViewUrl='https://music.apple.com/us/album/album/123',trackCount=1)


def track():
    return dict(wrapperType='track',kind='song',collectionId=123,discNumber=1,trackNumber=1,
        trackName='Song',artistName='Artist',trackTimeMillis=1000)


class ProviderReviewTests(unittest.TestCase):
    def client(self,details=None):
        client=Mock();client.country='us'
        client.get.side_effect=[[collection()],details if details is not None else [collection(),track()]]
        return client

    def test_compatible_full_track_list_remains_manual_context(self):
        report=p.review(catalog(),self.client())
        self.assertEqual(report['status'],'manual_review')
        self.assertTrue(report['candidates'][0]['track_comparison']['compatible'])
        self.assertEqual(report['candidates'][0]['date'],'2003-06-12')
        self.assertTrue(report['edit_release'].endswith(fixtures.RELEASE+'/edit'))
        self.assertIn('no edits are submitted',report['note'])

    def test_missing_positions_counts_and_duration_conflicts_are_reported(self):
        for changed in (dict(track(),trackNumber=2),dict(track(),trackTimeMillis=10000)):
            report=p.review(catalog(),self.client([collection(),changed]))
            self.assertFalse(report['candidates'][0]['track_comparison']['compatible'])
        report=p.review(catalog(),self.client([collection()]))
        self.assertFalse(report['candidates'][0]['track_comparison']['complete'])

    def test_lookup_failures_are_distinct_from_no_candidates(self):
        client=self.client();client.get.side_effect=requests.ConnectionError('network')
        self.assertEqual(p.review(catalog(),client)['status'],'lookup_failed')
        client.get.side_effect=[[]]
        self.assertEqual(p.review(catalog(),client)['status'],'no_exact_storefront_candidates_in_limited_search')

    def test_different_same_named_artist_and_changed_store_metadata_stay_for_review(self):
        wrong=dict(track(),artistName='Someone Else')
        result=p.review(catalog(),self.client([collection(),wrong]))
        self.assertFalse(result['candidates'][0]['track_comparison']['compatible'])
        changed=dict(collection(),releaseDate='2004-01-01T00:00:00Z')
        result=p.review(catalog(),self.client([changed,track()]))
        self.assertIn('changed between',result['candidates'][0]['track_comparison']['reason'])

    def test_unsafe_links_and_other_artist_search_results_are_not_offered(self):
        client=self.client();client.get.side_effect=[[dict(collection(),collectionViewUrl='javascript:bad')]]
        self.assertEqual(p.review(catalog(),client)['candidates'],[])
        client.get.side_effect=[[dict(collection(),artistName='Other Artist')]]
        self.assertEqual(p.review(catalog(),client)['candidates'],[])

    def test_rate_limit_wait_is_shared_between_search_and_lookup(self):
        session=Mock();session.headers={};session.get.return_value.json.return_value=dict(results=[])
        clock=Mock(side_effect=[10.0,10.1,10.2]);sleep=Mock()
        client=p.AppleReviewClient(session=session,clock=clock,sleep=sleep)
        client.get('search',term='test');client.get('lookup',id=1)
        self.assertAlmostEqual(sleep.call_args.args[0],3.0)


class DateProviderPlanTests(unittest.TestCase):
    def setUp(self):
        fixtures.UnmappedTests.setUp(self)

    def test_storefront_date_is_never_promoted_to_a_tag_replacement(self):
        before=self.path.read_bytes();report=Path(self.temp.name)/'audit.jsonl';output=Path(self.temp.name)/'plan.json'
        dates.audit(self.root,report)
        mb=Mock();mb.lookup_release.return_value=catalog(date='',original='')
        apple=ProviderReviewTests().client()
        payload=date_evidence.make_plan(self.root,report,output,mb,apple_review=True,apple_client=apple)
        self.assertEqual(payload['entries'][0]['targets'],{})
        self.assertEqual(payload['provider_reviews'][0]['candidates'][0]['date'],'2003-06-12')
        self.assertEqual(self.path.read_bytes(),before)
        rendered=output.with_suffix('.html').read_text()
        self.assertIn('Review existing release',rendered)
        self.assertIn('music.apple.com',rendered)
        payload['entries'][0]['targets']['original_date']='2003-06-12'
        output.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError,'reviewed source'):
            dates.apply_plan(self.root,output,self.journal,mb)
        self.assertEqual(self.path.read_bytes(),before)


if __name__=='__main__':
    unittest.main()
