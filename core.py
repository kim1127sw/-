"""배차 비교 계산과 입력 검증. 웹 프레임워크와 무관한 순수 Python 모듈."""
from __future__ import annotations
import csv
import io
import json
import math
import posixpath
import re
import zipfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from xml.etree import ElementTree as ET

SH = ['운영일','스냅샷일시','상태','차량번호','셀명','전체건수','기준건수','시간맞춤건수','용적','설치비(원)','거리(km)','시점근거','작성자','근거자료']
SK = ['date','at','status','vehicle','cell','total','standard','timefit','volume','fee','distance','basis','author','source']
EH = ['운영일','변경일시','변경ID','운송건ID','변경유형','변경전차량','변경후차량','증감건수','변경후배송일','변경사유','처리자','근거자료','반영여부']
EK = ['date','at','id','job','type','from','to','qty','newDate','reason','author','source','enabled']
METRICS = {'total':'전체건수','standard':'기준건수','timefit':'시간맞춤건수','volume':'용적','fee':'설치비(원)','distance':'거리(km)'}
CATS = ['연기','취소','반출','반입','추가','복원','배차해제']
TYPES = ['연기','취소','배차변경','추가','복원','배차해제']
KST = ZoneInfo('Asia/Seoul')

class InputError(ValueError):
    pass

def text(x) -> str:
    if x is None:
        return ''
    s = str(x).strip()
    if len(s) > 4000:
        raise InputError('한 셀의 글자 수는 4,000자를 넘을 수 없습니다.')
    return s

def date_value(x, with_time=False) -> str:
    try:
        if isinstance(x, (int, float)) or re.fullmatch(r'\d+(?:\.\d+)?', text(x)):
            d = datetime(1899, 12, 30) + timedelta(seconds=round(float(x)*86400))
        elif isinstance(x, datetime):
            d = x
        elif isinstance(x, date):
            if with_time:
                raise ValueError('시각 필요')
            d = datetime.combine(x, datetime.min.time())
        else:
            s = text(x).replace('/', '-').replace('Z', '+00:00')
            # Support ISO dates/times; explicitly reject date-only timestamps.
            if with_time and not re.search(r'[T ]\d{2}:\d{2}', s):
                raise ValueError('시각 필요')
            d = datetime.fromisoformat(s)
        if d.tzinfo:
            d = d.astimezone(KST).replace(tzinfo=None)
        if d.microsecond:
            raise ValueError('초 미만 값은 지원하지 않음')
        if not 2000 <= d.year <= 2100:
            raise ValueError('허용 연도 범위')
        return d.isoformat(timespec='seconds') if with_time else d.date().isoformat()
    except (ValueError, TypeError, OverflowError) as exc:
        raise InputError(f'날짜/시각 형식 오류: {text(x)!r}. YYYY-MM-DD 또는 YYYY-MM-DD HH:mm:ss 사용.') from exc

def number(x, label, integer=False):
    try:
        if not text(x):
            raise ValueError('필수값')
        n = float(text(x).replace(',', ''))
        if not math.isfinite(n) or n < 0 or (integer and not n.is_integer()):
            raise ValueError('범위')
        return int(n) if integer else n
    except (TypeError, ValueError) as exc:
        raise InputError(f'{label}: 0 이상 숫자가 필요합니다. 건수는 정수로 입력하세요.') from exc

def normalize(row: dict, kind: str) -> dict:
    hs, ks = (SH, SK) if kind == 'snapshots' else (EH, EK)
    x = {k:row.get(h, row.get(k, '')) for h,k in zip(hs,ks)}
    x['date'], x['at'] = date_value(x['date']), date_value(x['at'], True)
    if kind == 'snapshots':
        for k in ('status','vehicle','cell','basis','author','source'):
            x[k] = text(x[k])
        if not x['vehicle'] or not x['cell']:
            raise InputError('차량번호와 셀명은 필수입니다.')
        if x['status'] not in ['작성중','완료','최종확정']:
            raise InputError('상태: 작성중 / 완료 / 최종확정 중 선택하세요.')
        if x['basis'] not in ['시스템이력','완료즉시저장','사후관측']:
            raise InputError('시점근거: 시스템이력 / 완료즉시저장 / 사후관측 중 선택하세요.')
        for k in METRICS:
            x[k] = number(x[k], METRICS[k], k in ['total','standard','timefit'])
    else:
        for k in ('id','job','type','from','to','newDate','reason','author','source','enabled'):
            x[k] = text(x[k])
        x['enabled'] = x['enabled'].upper()
        x['qty'] = number(x['qty'], '증감건수', True)
        if not all(x[k] for k in ('id','job','reason')):
            raise InputError('변경ID, 운송건ID, 변경사유는 필수입니다.')
        if x['enabled'] not in ['Y','N'] or x['type'] not in TYPES or x['qty'] <= 0:
            raise InputError('변경유형 / 반영여부(Y,N) / 증감건수(양의 정수)를 확인하세요.')
        out = x['type'] in ['연기','취소','배차변경','배차해제']
        inc = x['type'] in ['배차변경','추가','복원']
        if bool(x['from']) != out or bool(x['to']) != inc:
            raise InputError('변경전·후 차량을 확인하세요. 추가·복원은 후차량만, 차감은 전차량만 입력합니다.')
        if x['type'] == '배차변경' and x['from'] == x['to']:
            raise InputError('배차변경의 전·후 차량이 같습니다.')
        if x['newDate']:
            x['newDate'] = date_value(x['newDate'])
        if x['type'] == '연기' and (not x['newDate'] or x['newDate'] <= x['date']):
            raise InputError('연기는 운영일 이후의 변경후배송일이 필요합니다.')
    return x

def record_key(kind, row):
    fields = [row['date'], row['vehicle'], row['at']] if kind == 'snapshots' else [row['date'],row['id']]
    return json.dumps(fields, ensure_ascii=False, separators=(',',':'))

def canonical(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',',':'), allow_nan=False)

def merge_data(old: dict, incoming: dict):
    """Atomic in-memory merge: conflicts never mutate the existing dataset."""
    result, count, skipped = {}, 0, 0
    for kind in ('snapshots','events'):
        m = {record_key(kind,r):r for r in old.get(kind,[])}
        for raw in incoming.get(kind,[]):
            r = normalize(raw, kind)
            k = record_key(kind,r)
            if k in m:
                if canonical(m[k]) != canonical(r):
                    raise InputError(f'같은 키에 다른 내용이 있습니다. 기존 기록을 변경하지 않았습니다: {k}')
                skipped += 1
            else:
                m[k] = r
                count += 1
        result[kind] = list(m.values())
    return result, count, skipped

def rows_from_grid(grid, kind):
    hs = SH if kind == 'snapshots' else EH
    for i, row in enumerate(grid[:25]):
        headers = [text(v) for v in row]
        if set(hs[:5]).issubset(headers):
            missing = set(hs) - set(headers)
            if missing:
                raise InputError('누락된 열: '+', '.join(sorted(missing)))
            positions = [headers.index(h) for h in hs]
            result=[]
            for line, values in enumerate(grid[i+1:], i+2):
                cells = [values[j] if j<len(values) else '' for j in positions]
                if not any(text(c) for c in cells):
                    continue
                try:
                    result.append(normalize(dict(zip(hs,cells)),kind))
                except InputError as e:
                    raise InputError(f'{kind} / {line}행: {e}') from e
            return result
    raise InputError('25행 이내에 입력양식의 제목 행이 없습니다.')

def xlsx_grids(data: bytes):
    """Read only the two input sheets. No formula evaluation or external links."""
    ns = {'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        if len(infos)>10000 or sum(x.file_size for x in infos)>100_000_000:
            raise InputError('압축 해제 크기 또는 파일 수 제한을 초과했습니다.')
        def xml(name):
            raw = z.read(name)
            if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                raise InputError('외부 엔티티 XML은 허용하지 않습니다.')
            return ET.fromstring(raw)
        wb = xml('xl/workbook.xml')
        props=wb.find('m:workbookPr',ns)
        date1904 = props is not None and props.attrib.get('date1904') in ('1','true')
        strings=[]
        if 'xl/sharedStrings.xml' in z.namelist():
            strings=[''.join(el.itertext()) for el in xml('xl/sharedStrings.xml').findall('m:si',ns)]
        rels={r.attrib['Id']:r.attrib['Target'] for r in xml('xl/_rels/workbook.xml.rels')
              if r.attrib.get('TargetMode') != 'External'}
        result={}
        for s in wb.findall('m:sheets/m:sheet',ns):
            kind={'스냅샷입력':'snapshots','변경이력':'events'}.get(s.attrib['name'])
            if not kind:
                continue
            rid=s.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
            target=rels[rid]
            path=target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/'+target)
            if not path.startswith('xl/'):
                raise InputError('허용되지 않은 시트 경로입니다.')
            grid=[]
            for r in xml(path).findall('m:sheetData/m:row',ns):
                row=[]
                for c in r.findall('m:c',ns):
                    letters=re.match(r'[A-Z]+',c.attrib['r']).group()
                    col=0
                    for ch in letters:
                        col=col*26+ord(ch)-64
                    if col>200:
                        continue
                    row += [''] * max(0,col-len(row))
                    v=c.find('m:v',ns)
                    t=c.attrib.get('t','')
                    if c.find('m:f',ns) is not None:
                        if v is not None and text(v.text):
                            raise InputError('입력 시트의 수식은 값 붙여넣기로 바꿔 주세요.')
                        val=''
                    elif t=='inlineStr':
                        val=''.join(c.find('m:is',ns).itertext())
                    elif t=='s':
                        val=strings[int(v.text)] if v is not None else ''
                    elif v is None:
                        val=''
                    elif t in ('str','d'):
                        val=v.text or ''
                    elif t=='e':
                        raise InputError('입력 시트에 Excel 오류값이 있습니다.')
                    else:
                        val=float(v.text) if v.text else ''
                        # Only dates in known template columns need 1904 adjustment.
                        if date1904 and isinstance(val,float) and col in ([1,2] if kind=='snapshots' else [1,2,9]):
                            val += 1462
                    row[col-1]=val
                grid.append(row)
            result[kind]=rows_from_grid(grid,kind)
        if not result:
            raise InputError('스냅샷입력 또는 변경이력 시트가 없습니다. 제공된 입력양식을 사용하세요.')
        return result

def parse_file(name: str, data: bytes, csv_kind='snapshots'):
    if len(data)>20_000_000:
        raise InputError('파일은 20MB 이하로 나누어 올려 주세요.')
    ext=name.lower().rsplit('.',1)[-1]
    try:
        if ext=='xlsx':
            return xlsx_grids(data)
        if ext=='csv':
            try:
                s=data.decode('utf-8-sig')
            except UnicodeDecodeError:
                s=data.decode('cp949')
            return {csv_kind:rows_from_grid(list(csv.reader(io.StringIO(s))),csv_kind)}
        if ext=='json':
            obj=json.loads(data.decode('utf-8-sig'))
            if not isinstance(obj,dict) or obj.get('mode')=='demo':
                raise InputError('예시 백업은 실자료에 합칠 수 없습니다.')
            if not any(k in obj for k in ('snapshots','events')):
                raise InputError('배차 백업 JSON이 아닙니다.')
            for kind in ('snapshots','events'):
                if not isinstance(obj.get(kind,[]),list):
                    raise InputError('JSON 데이터 형식을 확인하세요.')
            return {k:[normalize(r,k) for r in obj.get(k,[])] for k in ('snapshots','events')}
        raise InputError('XLSX, CSV 또는 JSON을 사용하세요. XLSB 원본 자동변환은 포함하지 않습니다.')
    except InputError:
        raise
    except (ValueError,KeyError,TypeError,ET.ParseError,zipfile.BadZipFile,UnicodeError,IndexError) as exc:
        raise InputError('파일이 손상되었거나 지원하는 입력형식이 아닙니다.') from exc

def compare(data, day: str, cutoff=''):
    snaps=[s for s in data.get('snapshots',[]) if s['date']==day and (not cutoff or s['at']<=cutoff)]
    events=[e for e in data.get('events',[]) if e['date']==day and e['enabled']=='Y' and (not cutoff or e['at']<=cutoff)]
    ids={s['vehicle'] for s in snaps} | {e[k] for e in events for k in ('from','to') if e[k]}
    result=[]
    for vehicle in sorted(ids):
        ss=sorted([s for s in snaps if s['vehicle']==vehicle],key=lambda x:x['at'])
        first=next((s for s in ss if s['status'] in ('완료','최종확정')),None)
        last=ss[-1] if ss else None
        paired=bool(first and last and last['at']>first['at'])
        r={'vehicle':vehicle,'date':day,'cell':(last or first or {}).get('cell','미지정'),
           'first':first,'last':last,'paired':paired,'snapshots':ss,'events':[],
           'counts':dict.fromkeys(CATS,0),'expected':None,'gap':None,'delta':None,
           'changed':False,'quality':[],'status':'최초 미확보'}
        if paired:
            r['events']=sorted([e for e in events if first['at']<e['at']<=last['at'] and vehicle in (e['from'],e['to'])],key=lambda e:(e['at'],e['id']))
            for e in r['events']:
                if e['type']=='배차변경':
                    r['counts']['반출' if e['from']==vehicle else '반입']+=e['qty']
                else:
                    r['counts'][e['type']]+=e['qty']
            c=r['counts']
            r['expected']=first['total']-c['연기']-c['취소']-c['반출']+c['반입']+c['추가']+c['복원']-c['배차해제']
            r['gap']=last['total']-r['expected']
            r['delta']=last['total']-first['total']
            r['changed']=bool(r['events']) or any(abs(last[k]-first[k])>1e-8 for k in METRICS) or first['cell']!=last['cell']
            if r['gap']:
                r['quality'].append('미설명 차이')
            if last['status']!='최종확정':
                r['quality'].append('최종 미확정')
            if '사후관측' in (first['basis'],last['basis']):
                r['quality'].append('시점 확인')
            r['status']=' / '.join(r['quality']) or '건수 일치'
        elif first:
            r['status']='후속자료 없음'
        result.append(r)
    return result

def raw_rows(kind, records):
    hs,ks=(SH,SK) if kind=='snapshots' else (EH,EK)
    return [{h:r.get(k,'') for h,k in zip(hs,ks)} for r in records]

def csv_bytes(rows, headers=None):
    """Prevent formula injection in report CSVs opened with Excel."""
    headers=headers or (list(rows[0]) if rows else [])
    out=io.StringIO(newline='')
    writer=csv.writer(out)
    writer.writerow(headers)
    for row in rows:
        values=[]
        for h in headers:
            v=row.get(h,'')
            if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@','\t','\r')):
                v="'"+v
            values.append(v)
        writer.writerow(values)
    return out.getvalue().encode('utf-8-sig')

def demo_data():
    day='2026-09-17'
    result={'snapshots':[],'events':[]}
    initial=[12,10,11,9,13,8,14,10]
    final=[9,11,10,10,13,8,15,10]
    def snap(i,n,at,status):
        return dict(zip(SK,[day,at,status,f'예시차량-{i+1:02}',f'예시셀-{i//2+1}',n,max(0,n-1),0,round(n*.62,3),n*35000,round(20+n*2.5,2),'완료즉시저장','예시담당자','허구 데이터']))
    for i,n in enumerate(initial):
        result['snapshots'].append(snap(i,n,f'2026-09-16T16:{30+i:02}:00','완료'))
        if i!=7:  # No later snapshot: must not turn into a zero-vehicle final.
            result['snapshots'].append(snap(i,final[i],'2026-09-16T18:00:00','완료' if i==5 else '최종확정'))
    items=[('연기',0,None),('취소',0,None),('배차변경',0,1),('배차변경',2,3),('취소',4,None),('복원',None,4),('추가',None,6)]
    for i,(ty,a,b) in enumerate(items):
        job='DEMO-J05' if i in (4,5) else f'DEMO-J{i+1:02}'
        row=dict(zip(EK,[day,f'2026-09-16T17:{i:02}:00',f'DEMO-E{i+1:02}',job,ty,'' if a is None else f'예시차량-{a+1:02}','' if b is None else f'예시차량-{b+1:02}',1,'2026-09-18' if ty=='연기' else '','기능 확인용 예시','예시담당자','허구 데이터','Y']))
        result['events'].append(row)
    return merge_data({'snapshots':[],'events':[]},result)[0]
