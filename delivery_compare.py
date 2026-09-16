"""Two-file delivery matching. No customer names, addresses or phone numbers are retained."""
from __future__ import annotations
import csv
import io
import json
import re
import zipfile
import posixpath
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

DEFAULT_DELAY_CODES = ('L', '7', '3', 'P')
SCHEMA_VERSION = 2
class CompareError(ValueError):
    pass

def text(v):
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = str(v).strip()
    if re.fullmatch(r'\d+\.0+', s):
        return s.split('.')[0]
    return s

def vehicle(v):
    return re.sub(r'\s+', '', text(v)).upper()

def number(v):
    s = text(v).replace(',', '')
    if not s:
        return None
    try:
        value = float(s)
        if not (-1e15 < value < 1e15):
            raise ValueError()
        return value
    except (ValueError, OverflowError) as exc:
        raise CompareError(f'숫자 형식 오류: {s[:40]}') from exc

def date_text(v, date1904=False):
    s = text(v)
    if not s:
        return ''
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', s):
        return s
    if re.fullmatch(r'\d{8}', s):
        try:
            return datetime.strptime(s, '%Y%m%d').date().isoformat()
        except ValueError:
            return s
    try:
        n = float(s)
        if 20000 < n < 90000:
            return (datetime(1899, 12, 30) + timedelta(days=n + (1462 if date1904 else 0))).date().isoformat()
    except ValueError:
        pass
    return s

def read_source(data: bytes, kind: str):
    """Read recognized XLSX sheet, project approved columns, preserve actual source row numbers."""
    if kind not in ('initial', 'final'):
        raise CompareError('파일 구분 오류')
    if len(data) > 30_000_000:
        raise CompareError('파일 크기는 30MB 이하여야 합니다.')
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    required = {'납품번호', '차량번호'} if kind == 'initial' else {'Delivery', 'Vehicle Number(Full)', 'PDAStepStatus'}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if len(z.infolist()) > 10000 or sum(x.file_size for x in z.infolist()) > 150_000_000:
                raise CompareError('압축 해제 크기 제한을 초과했습니다.')
            def xml(path):
                raw = z.read(path)
                if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                    raise CompareError('외부 엔티티 XML은 허용되지 않습니다.')
                return ET.fromstring(raw)
            wb = xml('xl/workbook.xml')
            prop = wb.find('m:workbookPr', ns)
            date1904 = prop is not None and prop.get('date1904') in ('1', 'true')
            strings = []
            if 'xl/sharedStrings.xml' in z.namelist():
                strings = [''.join(t.text or '' for t in si.findall('.//m:t', ns)) for si in xml('xl/sharedStrings.xml').findall('m:si', ns)]
            rels = {r.get('Id'): r.get('Target') for r in xml('xl/_rels/workbook.xml.rels') if r.get('TargetMode') != 'External'}
            candidates = []
            for sheet in wb.findall('m:sheets/m:sheet', ns):
                rid = sheet.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
                target = rels.get(rid, '')
                path = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
                if not path.startswith('xl/'):
                    raise CompareError('허용되지 않은 시트 경로입니다.')
                headers = None
                output = []
                blanks = []
                for row in xml(path).findall('m:sheetData/m:row', ns):
                    rn = int(row.get('r', 0))
                    vals = {}
                    for c in row.findall('m:c', ns):
                        letters = re.match(r'[A-Z]+', c.get('r', 'A1')).group()
                        col = 0
                        for ch in letters:
                            col = col * 26 + ord(ch) - 64
                        if col > 250:
                            continue
                        t = c.get('t', '')
                        v = c.find('m:v', ns)
                        if t == 'inlineStr':
                            value = ''.join(x.text or '' for x in c.findall('.//m:t', ns))
                        elif t == 's':
                            value = strings[int(v.text)] if v is not None else ''
                        elif t == 'e':
                            value = '#EXCEL_ERROR'
                        else:
                            value = v.text if v is not None and v.text else ''
                        vals[col] = value
                    if headers is None:
                        if rn > 25:
                            break
                        if required.issubset({text(v) for v in vals.values()}):
                            # First occurrence wins: final export has two separate Qty headers.
                            headers = {}
                            for col, name in sorted(vals.items()):
                                headers.setdefault(text(name), col)
                        continue
                    if not any(text(x) for x in vals.values()):
                        continue
                    def get(name):
                        val = vals.get(headers.get(name), '')
                        if val == '#EXCEL_ERROR':
                            raise CompareError(f'{sheet.get("name")} {rn}행 {name}: Excel 오류값')
                        return val
                    delivery = text(get('납품번호' if kind == 'initial' else 'Delivery'))
                    if not delivery:
                        blanks.append(rn)
                        continue
                    if not re.fullmatch(r'\d+', delivery):
                        raise CompareError(f'{rn}행 납품번호가 숫자 식별자가 아닙니다: {delivery[:30]}')
                    r = {'id': delivery, 'row': rn}
                    if kind == 'initial':
                        r.update(vehicle=vehicle(get('차량번호')), zone=text(get('ZONE')),
                                 qty=number(get('수량')), volume=number(get('용적 (㎥)')),
                                 model=text(get('Model')), route=text(get('ROUTE ID')))
                    else:
                        r.update(vehicle=vehicle(get('Vehicle Number(Full)')), item=text(get('item')),
                                 qty=number(get('Qty')), volume=number(get('Volume')),
                                 volume_unit=text(get('VolumeUnit')), model=text(get('Material')),
                                 code=text(get('PDAStepStatus')).upper(), description=text(get('PDAStatusTxt')),
                                 route=text(get('RouteID')), op_date=date_text(get('PlannedGIDate(CBO)'), date1904),
                                 book_date=date_text(get('BookDLDate'), date1904),
                                 assign_date=date_text(get('TruckAssignDate'), date1904))
                    output.append(r)
                if headers is not None:
                    if blanks:
                        raise CompareError(f'{sheet.get("name")}: 납품번호가 비어 있는 {len(blanks)}행이 있습니다. 예: {blanks[:5]}. 원본을 확인하세요.')
                    candidates.append({'kind': kind, 'sheet': sheet.get('name'), 'rows': output})
            if len(candidates) != 1:
                raise CompareError(f'필수 열이 있는 시트가 {len(candidates)}개입니다. 해당 파일에는 비교할 원본 시트 한 개가 필요합니다. 필수: {", ".join(sorted(required))}')
            if not candidates[0]['rows']:
                raise CompareError('자료 행이 없습니다.')
            return candidates[0]
    except CompareError:
        raise
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, IndexError, TypeError) as exc:
        raise CompareError('XLSX를 읽을 수 없습니다. 암호 없는 원본 .xlsx 파일인지 확인하세요.') from exc

def aggregate(source):
    groups = defaultdict(list)
    for r in source['rows']:
        groups[r['id']].append(r)
    result = {}
    for ident, rows in groups.items():
        issues = []
        distinct = []
        seen = {}
        for r in rows:
            signature = json.dumps({k: v for k, v in r.items() if k != 'row'}, sort_keys=True, ensure_ascii=False)
            if source['kind'] == 'final':
                key = r.get('item') or signature
            else:
                key = signature
            if key in seen:
                if seen[key] != signature:
                    issues.append('동일 품목번호 내용 충돌')
                continue
            seen[key] = signature
            distinct.append(r)
        if source['kind'] == 'initial' and len(distinct) > 1:
            issues.append('최초 납품번호 중복·분할 확인')
        vehicles = sorted({r['vehicle'] for r in rows if r['vehicle']})
        if any(not r['vehicle'] for r in rows):
            issues.append('차량번호 누락')
        if len(vehicles) > 1:
            issues.append('복수 차량·분할 확인')
        codes = sorted({r.get('code', '') for r in rows}) if source['kind'] == 'final' else []
        if source['kind'] == 'final' and (not codes or '' in codes):
            issues.append('상태코드 누락')
        if len(codes) > 1:
            issues.append('품목별 상태 혼합')
        units = {r.get('volume_unit', '') for r in distinct if r.get('volume') is not None}
        if source['kind'] == 'final' and any(u not in ('M3', 'm3', '㎥', 'm³') for u in units):
            issues.append('용적 단위 확인')
        def summed(key):
            if any(r.get(key) is None for r in distinct) or '동일 품목번호 내용 충돌' in issues:
                return None
            return round(sum(r[key] for r in distinct), 6)
        result[ident] = {'id': ident, 'vehicles': vehicles, 'codes': codes,
            'description': ' | '.join(sorted({r.get('description', '') for r in rows if r.get('description')})),
            'qty': summed('qty'), 'volume': summed('volume'), 'raw_rows': len(rows),
            'unique_rows': len(distinct), 'source_rows': [r['row'] for r in rows], 'issues': sorted(set(issues)),
            'zone': ' / '.join(sorted({r.get('zone', '') for r in rows if r.get('zone')})),
            'book_dates': sorted({r.get('book_date', '') for r in rows if r.get('book_date')}),
            'op_dates': sorted({r.get('op_date', '') for r in rows if r.get('op_date')})}
    return result

def build_comparison(initial, final, delay_codes=DEFAULT_DELAY_CODES):
    delays = {text(x).upper() for x in delay_codes}
    a, b = aggregate(initial), aggregate(final)
    rows = []
    for ident in sorted(set(a) | set(b)):
        first, last = a.get(ident), b.get(ident)
        av = first['vehicles'] if first else []
        bv = last['vehicles'] if last else []
        issues = list((first or {}).get('issues', [])) + list((last or {}).get('issues', []))
        paired = first is not None and last is not None
        if not first:
            matching = '최종만 존재'
            movement = '최초없음'
        elif not last:
            matching = '최종미존재'
            movement = '최종없음'
        else:
            matching = '양쪽 일치'
            if len(av) != 1 or len(bv) != 1 or '차량번호 누락' in issues:
                movement = '차량 확인필요'
            else:
                movement = '배차변경' if av != bv else '차량유지'
        codes = (last or {}).get('codes', [])
        if not last:
            status = '최종미존재'
        elif not codes or '' in codes:
            status = '상태 확인필요'
        elif all(x in delays for x in codes):
            status = '연기'
        elif any(x in delays for x in codes):
            status = '일부 연기(혼합)'
        else:
            status = '연기코드 아님'
        rows.append({'납품번호 / Delivery': ident, '매칭구분': matching,
            '최초 차량번호': ' / '.join(av), '최종 차량번호': ' / '.join(bv),
            '차량 이동': movement, '처리구분': status,
            'PDAStepStatus': ' / '.join(codes), 'PDAStatusTxt(원문)': (last or {}).get('description', ''),
            '최초 ZONE': (first or {}).get('zone', ''),
            '최초 수량': (first or {}).get('qty'), '최종 수량': (last or {}).get('qty'),
            '최초 용적(㎥)': (first or {}).get('volume'), '최종 용적(원본)': (last or {}).get('volume'),
            '최초 원본행수': (first or {}).get('raw_rows', 0), '최종 원본행수': (last or {}).get('raw_rows', 0),
            '최종 예약일': ' / '.join((last or {}).get('book_dates', [])),
            '최초 근거행': ', '.join(map(str, (first or {}).get('source_rows', []))),
            '최종 근거행': ', '.join(map(str, (last or {}).get('source_rows', []))),
            '확인사항': ' / '.join(sorted(set(issues)))})
    matched = sum(r['매칭구분'] == '양쪽 일치' for r in rows)
    metrics = {'최초 납품번호': len(a), '최종 Delivery': len(b), '양쪽 일치': matched,
        '차량 변경': sum(r['차량 이동'] == '배차변경' for r in rows),
        '연기': sum(r['처리구분'] == '연기' for r in rows),
        '일부 연기': sum(r['처리구분'] == '일부 연기(혼합)' for r in rows),
        '최종미존재': len(a) - matched, '최종만 존재': len(b) - matched,
        '확인필요': sum(bool(r['확인사항']) for r in rows),
        '최초 원본행': len(initial['rows']), '최종 원본행': len(final['rows'])}
    return {'rows': rows, 'metrics': metrics, 'delay_codes': sorted(delays),
            'initial_sheet': initial['sheet'], 'final_sheet': final['sheet']}

def transfer_summary(rows):
    groups = defaultdict(Counter)
    for r in rows:
        if r['차량 이동'] == '배차변경':
            c = groups[(r['최초 차량번호'], r['최종 차량번호'])]
            c['납품건수'] += 1
            c['연기'] += r['처리구분'] == '연기'
            c['일부 연기'] += r['처리구분'] == '일부 연기(혼합)'
    return [{'최초 차량번호': a, '최종 차량번호': b, **dict(c)} for (a, b), c in sorted(groups.items(), key=lambda x: (-x[1]['납품건수'], x[0]))]

def vehicle_summary(rows):
    """Set-based vehicle counts; transfer is not subtracted a second time for delayed deliveries."""
    names = sorted({v for r in rows for key in ('최초 차량번호', '최종 차량번호') for v in r[key].split(' / ') if v})
    result = []
    for v in names:
        first = [r for r in rows if v in r['최초 차량번호'].split(' / ')]
        last = [r for r in rows if v in r['최종 차량번호'].split(' / ')]
        paired_first = [r for r in first if r['매칭구분'] == '양쪽 일치']
        paired_last = [r for r in last if r['매칭구분'] == '양쪽 일치']
        out = sum(r['차량 이동'] == '배차변경' for r in first)
        inc = sum(r['차량 이동'] == '배차변경' for r in last)
        missing = sum(r['매칭구분'] == '최종미존재' for r in first)
        extra = sum(r['매칭구분'] == '최종만 존재' for r in last)
        expected = len(first) - missing - out + inc + extra
        result.append({'차량번호': v, '최초 납품건수': len(first), '최종 등재건수': len(last),
            '등재 증감': len(last) - len(first), '동일대상 최초건수': len(paired_first), '동일대상 최종건수': len(paired_last),
            '반출': out, '반입': inc, '최초차량 기준 연기': sum(r['처리구분'] == '연기' for r in first),
            '최종차량 기준 연기': sum(r['처리구분'] == '연기' for r in last),
            '최종차량 일부 연기': sum(r['처리구분'] == '일부 연기(혼합)' for r in last),
            '최종미존재': missing, '최종만 존재': extra, '등재건수 검산차이': len(last) - expected,
            '연기코드 아닌 최종건수': sum(r['처리구분'] == '연기코드 아님' for r in last)})
    return result

def csv_bytes(rows):
    if not rows:
        return '\ufeff'.encode('utf8')
    def safe(v):
        if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
            return "'" + v
        return v
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), extrasaction='ignore')
    writer.writeheader()
    writer.writerows({k: safe(v) for k, v in r.items()} for r in rows)
    return out.getvalue().encode('utf-8-sig')

def demo_pair():
    initial, final = [], []
    for i in range(1, 11):
        initial.append({'id': str(9000000000+i), 'row': i+1, 'vehicle': f'예시차량{(i-1)//3+1}', 'zone': '예시', 'qty': 1.0, 'volume': .5, 'model': 'DEMO'})
        if i == 10:
            continue
        final.append({'id': str(9000000000+i), 'row': i+1, 'vehicle': '예시차량2' if i in (1, 2) else initial[-1]['vehicle'],
            'item': '10', 'code': ('L' if i == 2 else 'P' if i == 3 else '1'), 'description': '가상 예시 상태',
            'qty': 1.0, 'volume': .5, 'volume_unit': 'M3', 'model': 'DEMO', 'op_date': '2026-09-16', 'book_date': '2026-09-16'})
    return {'kind': 'initial', 'sheet': '가상 예시', 'rows': initial}, {'kind': 'final', 'sheet': '가상 예시', 'rows': final}
