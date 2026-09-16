import copy
import tempfile
import unittest
import io
import zipfile
from pathlib import Path
from delivery_compare import *
from pair_storage import PairStore
from storage import Store

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b = demo_pair()
    def test_demo_counts(self):
        m = build_comparison(self.a, self.b)['metrics']
        self.assertEqual((m['최초 납품번호'],m['최종 Delivery'],m['차량 변경'],m['연기'],m['최종미존재']), (10,9,2,2,1))
    def test_every_requested_code_delayed(self):
        for code in ('L','7','3','P'):
            b = copy.deepcopy(self.b);b['rows'][0]['code'] = code
            self.assertEqual(build_comparison(self.a,b)['rows'][0]['처리구분'],'연기')
    def test_other_status_not_completed(self):
        self.b['rows'][0]['code']='1'
        self.assertEqual(build_comparison(self.a,self.b)['rows'][0]['처리구분'],'연기코드 아님')
    def test_delay_and_movement_independent(self):
        r=build_comparison(self.a,self.b)['rows'][1]
        self.assertEqual((r['처리구분'],r['차량 이동']),('연기','배차변경'))
    def test_missing_not_cancelled(self):
        r=build_comparison(self.a,self.b)['rows'][-1]
        self.assertEqual(r['매칭구분'],'최종미존재')
        self.assertNotIn('취소',r.values())
    def test_final_only_included(self):
        r=copy.deepcopy(self.b['rows'][0]);r.update(id='9999999999',row=100)
        self.b['rows'].append(r)
        m=build_comparison(self.a,self.b)['metrics']
        self.assertEqual(m['최종만 존재'],1)
    def test_item_lines_not_deliveries(self):
        r=copy.deepcopy(self.b['rows'][0]);r.update(item='20',row=20)
        self.b['rows'].append(r)
        out=build_comparison(self.a,self.b)
        self.assertEqual(out['metrics']['최종 Delivery'],9)
        self.assertEqual(out['rows'][0]['최종 수량'],2)
    def test_mixed_status_not_fully_delayed(self):
        r=copy.deepcopy(self.b['rows'][0]);r.update(item='20',row=20,code='L')
        self.b['rows'].append(r)
        self.assertEqual(build_comparison(self.a,self.b)['rows'][0]['처리구분'],'일부 연기(혼합)')
    def test_duplicate_item_not_double_counted(self):
        r=copy.deepcopy(self.b['rows'][0]);r['row']=20
        self.b['rows'].append(r)
        self.assertEqual(build_comparison(self.a,self.b)['rows'][0]['최종 수량'],1)
    def test_conflicting_item_quantity_unknown(self):
        r=copy.deepcopy(self.b['rows'][0]);r.update(row=20,qty=8)
        self.b['rows'].append(r)
        out=build_comparison(self.a,self.b)['rows'][0]
        self.assertIsNone(out['최종 수량'])
        self.assertIn('충돌',out['확인사항'])
    def test_multi_vehicle_not_arbitrary(self):
        r=copy.deepcopy(self.b['rows'][0]);r.update(item='20',row=20,vehicle='분할차량')
        self.b['rows'].append(r)
        out=build_comparison(self.a,self.b)['rows'][0]
        self.assertEqual(out['차량 이동'],'차량 확인필요')
        self.assertIn('분할차량',out['최종 차량번호'])
    def test_vehicle_reconcile(self):
        rows=build_comparison(self.a,self.b)['rows']
        self.assertTrue(all(x['등재건수 검산차이']==0 for x in vehicle_summary(rows)))
    def test_config_can_exclude_P(self):
        r=build_comparison(self.a,self.b,['L','7'])['rows'][2]
        self.assertEqual(r['처리구분'],'연기코드 아님')
        self.assertEqual(r['PDAStepStatus'],'P')
    def test_identifiers_preserve_zero(self):
        self.assertEqual(text('001234'),'001234')
        self.assertEqual(text(1234.0),'1234')
        self.assertEqual(text('7.0'),'7')
        self.assertEqual(vehicle(' 경북 80아 8515 '),'경북80아8515')
    def test_csv_formula_escape(self):
        s=csv_bytes([{'x':'=2+2','y':'ordinary'}]).decode('utf-8-sig')
        self.assertIn("'=2+2",s)
    def test_bad_xlsx_rejected(self):
        with self.assertRaises(CompareError):read_source(b'invalid','initial')
    def test_small_xml_duplicate_qty_headers(self):
        # Two Qty columns: first is the item quantity, second must not overwrite it.
        h=['Delivery','Vehicle Number(Full)','PDAStepStatus','Qty','Qty','item']
        vals=['7360000001','차량1','7','2','999','10']
        def row(r,vs):
            return '<row r="%d">'%r+''.join('<c r="%s%d" t="inlineStr"><is><t>%s</t></is></c>'%(chr(65+i),r,v) for i,v in enumerate(vs))+'</row>'
        ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:
            z.writestr('xl/workbook.xml','<workbook xmlns="'+ns+'" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
            z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
            z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="'+ns+'"><sheetData>'+row(1,h)+row(2,vals)+'</sheetData></worksheet>')
        doc=read_source(buf.getvalue(),'final')
        self.assertEqual(doc['rows'][0]['qty'],2.0)
        self.assertEqual(doc['rows'][0]['code'],'7')
        self.assertNotIn('ShipToAddress',doc['rows'][0])
    def test_storage_immutable_and_append_only(self):
        with tempfile.TemporaryDirectory() as d:
            p=PairStore(Store('sqlite:///'+str(Path(d)/'test.db')))
            self.assertEqual(p.save('2026-09-16',self.a,self.b,'test','first.xlsx','last.xlsx'),2)
            self.assertEqual(p.save('2026-09-16',self.a,self.b,'test','first.xlsx','last.xlsx'),0)
            b=copy.deepcopy(self.b);b['rows'][0]['code']='L'
            self.assertEqual(p.save('2026-09-16',self.a,b,'test','first.xlsx','last2.xlsx'),1)
            a=copy.deepcopy(self.a);a['rows'][0]['vehicle']='NEW'
            with self.assertRaises(CompareError):p.save('2026-09-16',a,b,'test','first2.xlsx','last2.xlsx')
            data=p.load('2026-09-16')
            self.assertEqual(len(data['versions']),2)
            self.assertEqual(data['initial'],self.a)
            self.assertEqual(p.load('2026-09-16',data['versions'][0]['hash'])['final'],self.b)

if __name__=='__main__': unittest.main()
