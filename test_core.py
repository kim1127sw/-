import copy
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from core import *
from storage import Store

class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.data=demo_data()
    def test_demo_reconciliation(self):
        rows=compare(self.data,'2026-09-17')
        paired=[r for r in rows if r['paired']]
        self.assertEqual(len(rows),8)
        self.assertEqual(len(paired),7)
        self.assertEqual(sum(r['first']['total'] for r in paired),77)
        self.assertEqual(sum(r['last']['total'] for r in paired),76)
        self.assertTrue(all(r['gap']==0 for r in paired))
    def test_no_later_snapshot_not_zero(self):
        r=next(r for r in compare(self.data,'2026-09-17') if r['vehicle']=='예시차량-08')
        self.assertFalse(r['paired']);self.assertIsNone(r['delta']);self.assertEqual(r['status'],'후속자료 없음')
    def test_cutoff(self):
        rows=compare(self.data,'2026-09-17','2026-09-16T16:59:00')
        self.assertFalse(any(r['paired'] for r in rows))
    def test_cancel_restore_both_retained(self):
        r=next(r for r in compare(self.data,'2026-09-17') if r['vehicle']=='예시차량-05')
        self.assertEqual((r['counts']['취소'],r['counts']['복원'],r['delta']),(1,1,0))
        self.assertTrue(r['changed'])
    def test_disabled_events(self):
        self.data['events'][0]['enabled']='N'
        r=next(r for r in compare(self.data,'2026-09-17') if r['vehicle']=='예시차량-01')
        self.assertEqual(r['counts']['연기'],0);self.assertEqual(r['gap'],-1)
    def test_first_boundary_excluded(self):
        self.data['events'][0]['at']='2026-09-16T16:30:00'
        r=next(r for r in compare(self.data,'2026-09-17') if r['vehicle']=='예시차량-01')
        self.assertEqual(r['counts']['연기'],0)
    def test_different_vehicle_baselines(self):
        # Recipient completion occurs after transfer; inbound must be excluded there.
        for s in self.data['snapshots']:
            if s['vehicle']=='예시차량-02' and s['status']=='완료':
                s['at']='2026-09-16T17:03:00';s['total']=11
        rows={r['vehicle']:r for r in compare(self.data,'2026-09-17')}
        self.assertEqual(rows['예시차량-01']['counts']['반출'],1)
        self.assertEqual(rows['예시차량-02']['counts']['반입'],0)
    def test_repeated_transfer(self):
        data={'snapshots':[], 'events':[]}
        for v,n in [('A',10),('B',5)]:
            for at,status in [('2026-09-16T16:00:00','완료'),('2026-09-16T18:00:00','최종확정')]:
                s=copy.deepcopy(self.data['snapshots'][0]);s.update(vehicle=v,total=n,at=at,status=status);data['snapshots'].append(s)
        for i,(a,b) in enumerate([('A','B'),('B','A')]):
            e=copy.deepcopy(self.data['events'][2]);e.update({'id':f'RT-{i}','from':a,'to':b,'at':f'2026-09-16T17:0{i}:00'});data['events'].append(e)
        rows=compare(data,'2026-09-17')
        self.assertTrue(all(r['delta']==0 and r['gap']==0 and r['changed'] for r in rows))
        self.assertTrue(all(r['counts']['반출']==1 for r in rows))
    def test_duplicate_skip(self):
        _,added,skipped=merge_data(self.data,self.data)
        self.assertEqual(added,0);self.assertEqual(skipped,22)
    def test_conflict_and_immutable_input(self):
        before=canonical(self.data);extra=copy.deepcopy(self.data);extra['snapshots'][0]['total']+=1
        with self.assertRaises(InputError): merge_data(self.data,extra)
        self.assertEqual(canonical(self.data),before)
    def test_missing_metric_rejected(self):
        s=copy.deepcopy(self.data['snapshots'][0]);s['volume']=''
        with self.assertRaises(InputError): normalize(s,'snapshots')
    def test_invalid_deferral_rejected(self):
        e=copy.deepcopy(self.data['events'][0]);e['newDate']=e['date']
        with self.assertRaises(InputError): normalize(e,'events')
    def test_csv_roundtrip(self):
        for kind,hs in [('snapshots',SH),('events',EH)]:
            content=csv_bytes(raw_rows(kind,self.data[kind]),hs)
            self.assertEqual(parse_file('input.csv',content,kind)[kind],self.data[kind])
    def test_legacy_json(self):
        obj={'format':'dispatch-compare-v1','mode':'real',**self.data}
        result=parse_file('backup.json',json.dumps(obj).encode())
        self.assertEqual(result,self.data)
    def test_demo_json_blocked(self):
        with self.assertRaises(InputError):
            parse_file('backup.json',json.dumps({'mode':'demo',**self.data}).encode())
    def test_timezones(self):
        self.assertEqual(date_value('2026-09-16T07:00:00+00:00',True),'2026-09-16T16:00:00')
        with self.assertRaises(InputError):date_value('2026-09-16',True)
        with self.assertRaises(InputError):date_value('2026-09-16T16:00:00.500',True)
    def test_csv_injection(self):
        self.assertIn("'=cmd",csv_bytes([{'memo':'=cmd'}]).decode('utf-8-sig'))
    def test_sqlite_reload_and_atomicity(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store('sqlite:///'+str(Path(d)/'test.db'))
            self.assertEqual(store.append(self.data,'tester','hash'),(22,0))
            self.assertEqual(store.append(self.data,'tester','hash'),(0,22))
            self.assertEqual({canonical(r) for r in store.load()['snapshots']},{canonical(r) for r in self.data['snapshots']})
            fresh=copy.deepcopy(self.data['snapshots'][0]);fresh['vehicle']='NEW'
            bad=copy.deepcopy(self.data['snapshots'][0]);bad['total']=99
            with self.assertRaises(InputError):store.append({'snapshots':[fresh,bad]},'tester','hash')
            self.assertEqual(len(store.load()['snapshots']),15)
            self.assertEqual(len(store.audit()),22)

if __name__=='__main__':
    unittest.main(verbosity=2)
