"""Streamlit two-original-file workflow, with per-session previews and optional append-only save."""
from __future__ import annotations
import csv
import io
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo
import streamlit as st
from delivery_compare import (CompareError, aggregate, csv_bytes, demo_pair, text, vehicle, number, date_text)
from pair_storage import PairStore

# 이 파일 하나만 교체하면 CSV 업로드 + 상태 판정(L·7=연기, 3·P=취소)이 적용됩니다.
DELAY_CODES = ('L', '7')
CANCEL_CODES = ('3', 'P')

def _decode_candidates(data: bytes):
    """Excel/사내 시스템에서 자주 나오는 한국어 CSV 문자셋을 안전하게 순서대로 시도합니다."""
    if len(data) > 30_000_000:
        raise CompareError('파일 크기는 30MB 이하여야 합니다.')
    if data.startswith(b'PK\\x03\\x04'):
        raise CompareError('이 파일은 CSV가 아니라 XLSX 형식입니다. Excel에서 CSV 또는 CSV UTF-8로 다시 저장해 주세요.')
    if data.startswith(b'\\xD0\\xCF\\x11\\xE0'):
        raise CompareError('이 파일은 CSV가 아니라 구형 Excel 형식입니다. Excel에서 CSV 또는 CSV UTF-8로 다시 저장해 주세요.')
    encodings = ('utf-8-sig', 'cp949', 'euc-kr', 'utf-16', 'utf-16-le', 'utf-16-be')
    seen = set()
    for enc in encodings:
        try:
            content = data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
        # 잘못된 UTF-16 추측으로 NUL이 과다하게 생기는 경우 제외
        nul_ratio = content.count('\\x00') / max(1, len(content))
        if nul_ratio > 0.05:
            continue
        key = content[:5000]
        if key in seen:
            continue
        seen.add(key)
        yield enc, content.replace('\\x00', '')


def _rows_with_delimiter(content: str, delimiter: str):
    try:
        return list(csv.reader(io.StringIO(content, newline=''), delimiter=delimiter))
    except csv.Error:
        return []


def _find_header(rows, required):
    for i, row in enumerate(rows[:40]):
        cleaned = [text(v).replace('\\ufeff', '').strip() for v in row]
        if required.issubset(set(cleaned)):
            headers = {}
            for col, name in enumerate(cleaned):
                if name and name not in headers:
                    headers[name] = col
            return i, headers
    return None, None


def read_csv_source(data: bytes, kind: str, filename: str = ''):
    """CSV/Excel Unicode 텍스트를 읽고 비교에 필요한 열만 메모리에 남깁니다."""
    if kind not in ('initial', 'final'):
        raise CompareError('파일 구분 오류')
    required = {'납품번호', '차량번호'} if kind == 'initial' else {'Delivery', 'Vehicle Number(Full)', 'PDAStepStatus'}

    decoded_any = False
    best_columns = []
    chosen = None
    # 문자셋 + 구분자(쉼표/탭/세미콜론)를 모두 자동 시도합니다.
    for enc, content in _decode_candidates(data):
        decoded_any = True
        delimiters = []
        try:
            sniffed = csv.Sniffer().sniff(content[:50000], delimiters=',\\t;').delimiter
            delimiters.append(sniffed)
        except csv.Error:
            pass
        for d in (',', '\\t', ';'):
            if d not in delimiters:
                delimiters.append(d)
        for delimiter in delimiters:
            rows = _rows_with_delimiter(content, delimiter)
            if not rows:
                continue
            header_i, headers = _find_header(rows, required)
            if headers is not None:
                chosen = (enc, delimiter, rows, header_i, headers)
                break
            # 오류 안내용으로 첫 행 열 이름 일부만 보관(민감 데이터 행은 사용하지 않음)
            if rows:
                first = [text(v).replace('\\ufeff', '').strip() for v in rows[0]][:20]
                if len(first) > len(best_columns):
                    best_columns = first
        if chosen:
            break

    if not decoded_any:
        raise CompareError('CSV 문자 인코딩을 읽지 못했습니다. UTF-8/CP949/EUC-KR/UTF-16을 모두 확인했습니다. 파일이 암호화되었거나 CSV가 아닌 경우가 있으니 Excel에서 새 CSV로 저장해 주세요.')
    if chosen is None:
        raise CompareError('파일은 읽었지만 필수 열을 찾지 못했습니다. 필요한 열: ' + ', '.join(sorted(required)) + '. CSV 첫 행에 열 제목이 있는지 확인해 주세요.')

    enc, delimiter, rows, header_i, headers = chosen
    output = []
    blank_rows = []
    def cell(row, name):
        idx = headers.get(name)
        return row[idx] if idx is not None and idx < len(row) else ''

    for physical_row, row in enumerate(rows[header_i + 1:], start=header_i + 2):
        if not any(text(v) for v in row):
            continue
        delivery = text(cell(row, '납품번호' if kind == 'initial' else 'Delivery'))
        if not delivery:
            blank_rows.append(physical_row)
            continue
        # Excel이 큰 숫자를 1.234E+09처럼 바꾸는 것을 막기 위해 납품번호는 문자열 숫자로 유지합니다.
        if delivery.endswith('.0') and delivery[:-2].isdigit():
            delivery = delivery[:-2]
        if not re.fullmatch(r'\\d+', delivery):
            raise CompareError(f'{physical_row}행 납품번호가 숫자 식별자가 아닙니다: {delivery[:30]}')
        rec = {'id': delivery, 'row': physical_row}
        if kind == 'initial':
            rec.update(
                vehicle=vehicle(cell(row, '차량번호')),
                zone=text(cell(row, 'ZONE')),
                qty=number(cell(row, '수량')),
                volume=number(cell(row, '용적 (㎥)')),
                model=text(cell(row, 'Model')),
                route=text(cell(row, 'ROUTE ID')),
            )
        else:
            rec.update(
                vehicle=vehicle(cell(row, 'Vehicle Number(Full)')),
                item=text(cell(row, 'item')),
                qty=number(cell(row, 'Qty')),
                volume=number(cell(row, 'Volume')),
                volume_unit=text(cell(row, 'VolumeUnit')),
                model=text(cell(row, 'Material')),
                code=text(cell(row, 'PDAStepStatus')).upper(),
                description=text(cell(row, 'PDAStatusTxt')),
                route=text(cell(row, 'RouteID')),
                op_date=date_text(cell(row, 'PlannedGIDate(CBO)')),
                book_date=date_text(cell(row, 'BookDLDate')),
                assign_date=date_text(cell(row, 'TruckAssignDate')),
            )
        output.append(rec)
    if blank_rows:
        raise CompareError(f'납품번호가 비어 있는 {len(blank_rows)}행이 있습니다. 예: {blank_rows[:5]}. 원본을 확인하세요.')
    if not output:
        raise CompareError('자료 행이 없습니다.')
    return {'kind': kind, 'sheet': filename or 'CSV', 'rows': output, 'encoding': enc}


def _paste_lines(raw: str, header_names=()):
    """Excel 한 열을 복사해 붙인 텍스트를 행 단위로 유지합니다."""
    raw = (raw or '').replace('\r\n', '\n').replace('\r', '\n')
    lines = raw.split('\n')
    # 브라우저 textarea가 마지막 줄바꿈을 포함하는 경우 끝의 빈 줄만 제거
    while lines and lines[-1] == '':
        lines.pop()
    if lines and text(lines[0]).lower() in {text(x).lower() for x in header_names}:
        lines = lines[1:]
    return [text(x) for x in lines]

def read_pasted_columns(initial_delivery: str, initial_vehicle: str,
                        final_delivery: str, final_vehicle: str, final_status: str,
                        use_saved_initial=None, comparison_date: str = ''):
    """파일을 올리지 않고 Excel 열 복사/붙여넣기만으로 비교용 자료를 만듭니다."""
    if use_saved_initial is None:
        a_id = _paste_lines(initial_delivery, ('납품번호', 'Delivery'))
        a_vehicle = _paste_lines(initial_vehicle, ('배차차량', '차량번호', '배차 차량', '차량 번호'))
        if not a_id or not a_vehicle:
            raise CompareError('상세정보의 납품번호와 배차차량을 각각 붙여넣어 주세요.')
        if len(a_id) != len(a_vehicle):
            raise CompareError(f'상세정보 행 수가 다릅니다. 납품번호 {len(a_id):,}행 / 배차차량 {len(a_vehicle):,}행. 같은 범위를 복사해 주세요.')
        initial_rows = []
        for i, (ident, truck) in enumerate(zip(a_id, a_vehicle), start=1):
            if not ident and not truck:
                continue
            if not ident:
                raise CompareError(f'상세정보 붙여넣기 {i}행의 납품번호가 비어 있습니다.')
            if ident.endswith('.0') and ident[:-2].isdigit():
                ident = ident[:-2]
            if not re.fullmatch(r'\d+', ident):
                raise CompareError(f'상세정보 붙여넣기 {i}행 납품번호가 숫자가 아닙니다: {ident[:30]}')
            initial_rows.append({
                'id': ident, 'row': i, 'vehicle': vehicle(truck), 'zone': '',
                'qty': None, 'volume': None, 'model': '', 'route': ''
            })
        if not initial_rows:
            raise CompareError('상세정보 붙여넣기 자료가 없습니다.')
        initial = {'kind': 'initial', 'sheet': '상세정보 · 복사붙여넣기', 'rows': initial_rows, 'encoding': 'clipboard'}
    else:
        initial = use_saved_initial

    b_id = _paste_lines(final_delivery, ('Delivery', '납품번호'))
    b_vehicle = _paste_lines(final_vehicle, ('Vehicle Number(Full)', 'VehicleNumber(Full)', 'Vehicle Number'))
    b_status = _paste_lines(final_status, ('PDAStepStatus', 'PDA Step Status'))
    if not b_id or not b_vehicle:
        raise CompareError('최종리스트의 Delivery와 Vehicle Number(Full)를 각각 붙여넣어 주세요.')
    if len(b_id) != len(b_vehicle):
        raise CompareError(
            f'최종리스트 행 수가 다릅니다. Delivery {len(b_id):,}행 / '
            f'차량 {len(b_vehicle):,}행. 같은 범위를 복사해 주세요.'
        )
    # PDAStepStatus는 빈값도 정상값으로 사용합니다.
    # 열 전체가 비었거나 끝부분이 빈 셀인 경우 Delivery 행수만큼 빈값으로 채웁니다.
    if len(b_status) < len(b_id):
        b_status = b_status + [''] * (len(b_id) - len(b_status))
    elif len(b_status) > len(b_id):
        raise CompareError(
            f'PDAStepStatus 행수가 Delivery보다 많습니다. Delivery {len(b_id):,}행 / '
            f'PDAStepStatus {len(b_status):,}행. 같은 시작행·끝행을 복사해 주세요.'
        )
    final_rows = []
    for i, (ident, truck, status) in enumerate(zip(b_id, b_vehicle, b_status), start=1):
        if not ident and not truck and not status:
            continue
        if not ident:
            raise CompareError(f'최종리스트 붙여넣기 {i}행의 Delivery가 비어 있습니다.')
        if ident.endswith('.0') and ident[:-2].isdigit():
            ident = ident[:-2]
        if not re.fullmatch(r'\d+', ident):
            raise CompareError(f'최종리스트 붙여넣기 {i}행 Delivery가 숫자가 아닙니다: {ident[:30]}')
        final_rows.append({
            'id': ident, 'row': i, 'vehicle': vehicle(truck),
            'item': '', 'qty': None, 'volume': None, 'volume_unit': '',
            'model': '', 'code': text(status).upper(), 'description': '',
            'route': '', 'op_date': comparison_date, 'book_date': '', 'assign_date': ''
        })
    if not final_rows:
        raise CompareError('최종리스트 붙여넣기 자료가 없습니다.')
    final = {'kind': 'final', 'sheet': '최종리스트 · 복사붙여넣기', 'rows': final_rows, 'encoding': 'clipboard'}
    return initial, final


def build_comparison_local(initial, final):
    """기존 delivery_compare.py 버전과 무관하게 현재 업무 기준으로 판정합니다."""
    delays = set(DELAY_CODES)
    cancels = set(CANCEL_CODES)
    a, b = aggregate(initial), aggregate(final)
    op_dates = sorted({r.get('op_date', '') for r in final.get('rows', []) if r.get('op_date')})
    comparison_day = op_dates[0] if len(op_dates) == 1 else ' / '.join(op_dates)
    rows = []
    for ident in sorted(set(a) | set(b)):
        first, last = a.get(ident), b.get(ident)
        av = first['vehicles'] if first else []
        bv = last['vehicles'] if last else []
        issues = list((first or {}).get('issues', [])) + list((last or {}).get('issues', []))
        if not first:
            matching, movement = '최종만 존재', '최초없음'
        elif not last:
            matching, movement = '최종미존재', '최종없음'
        else:
            matching = '양쪽 일치'
            if len(av) != 1 or len(bv) != 1 or '차량번호 누락' in issues:
                movement = '차량 확인필요'
            else:
                movement = '배차변경' if av != bv else '차량유지'
        codes = (last or {}).get('codes', [])
        if not last:
            status = '최종미존재'
        else:
            # 빈 PDAStepStatus도 정상 일반상태로 취급합니다.
            # 한 Delivery에 여러 품목행이 있으면 각 상태를 분류한 뒤 서로 다를 때만 혼합으로 표시합니다.
            code_categories = set()
            for x in codes or ['']:
                if x in delays:
                    code_categories.add('연기')
                elif x in cancels:
                    code_categories.add('취소')
                else:
                    code_categories.add('일반상태')
            status = next(iter(code_categories)) if len(code_categories) == 1 else '상태 혼합 확인'
        rows.append({
            '배차일': comparison_day,
            '납품번호 / Delivery': ident, '매칭구분': matching,
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
            '확인사항': ' / '.join(sorted(set(issues))),
        })
    matched = sum(r['매칭구분'] == '양쪽 일치' for r in rows)
    delay_count = sum(r['처리구분'] == '연기' for r in rows)
    cancel_count = sum(r['처리구분'] == '취소' for r in rows)
    final_listed_count = len(b)
    final_dispatch_count = max(0, final_listed_count - delay_count - cancel_count)
    metrics = {
        '최초 배차건수': len(a),
        '최종 배차건수': final_dispatch_count,
        '최종 원본등재건수(참고)': final_listed_count,
        '양쪽 일치': matched,
        '차량 변경': sum(r['차량 이동'] == '배차변경' for r in rows),
        '연기': delay_count,
        '취소': cancel_count,
        '상태 혼합 확인': sum(r['처리구분'] == '상태 혼합 확인' for r in rows),
        '최종미존재': len(a) - matched, '최종만 존재': len(b) - matched,
        '확인필요': sum(bool(r['확인사항']) for r in rows),
        '최초 원본행': len(initial['rows']), '최종 원본행': len(final['rows']),
    }
    return {'rows': rows, 'metrics': metrics, 'delay_codes': list(DELAY_CODES), 'cancel_codes': list(CANCEL_CODES),
            'initial_sheet': initial['sheet'], 'final_sheet': final['sheet']}

def transfer_summary_local(rows):
    groups = defaultdict(Counter)
    for r in rows:
        if r['차량 이동'] == '배차변경':
            c = groups[(r['최초 차량번호'], r['최종 차량번호'])]
            c['납품건수'] += 1
            c['연기'] += r['처리구분'] == '연기'
            c['취소'] += r['처리구분'] == '취소'
            c['상태 혼합 확인'] += r['처리구분'] == '상태 혼합 확인'
    return [{'최초 차량번호': a, '최종 차량번호': b, **dict(c)}
            for (a, b), c in sorted(groups.items(), key=lambda x: (-x[1]['납품건수'], x[0]))]

def vehicle_summary_local(rows):
    """차량별 최초 배차건수와 연기·취소를 제외한 최종 배차건수를 비교합니다."""
    names = sorted({v for r in rows for key in ('최초 차량번호', '최종 차량번호')
                    for v in r[key].split(' / ') if v})
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

        delay_last = sum(r['처리구분'] == '연기' for r in last)
        cancel_last = sum(r['처리구분'] == '취소' for r in last)
        final_listed = len(last)
        final_dispatch = max(0, final_listed - delay_last - cancel_last)

        # 이 검산은 원본 최종 등재건수 기준입니다. 연기·취소 제외 최종 배차건수와는 별도입니다.
        expected_listed = len(first) - missing - out + inc + extra

        dispatch_change = out + inc
        issue_total = dispatch_change + delay_last + cancel_last
        result.append({
            '차량번호': v,
            '최초 배차건수': len(first),
            '최종 배차건수': final_dispatch,
            '배차 증감': final_dispatch - len(first),
            '배차변경': dispatch_change,
            '변동합계': issue_total,
            '최종 원본등재(참고)': final_listed,
            '연기 제외': delay_last,
            '취소 제외': cancel_last,
            '동일대상 최초건수': len(paired_first),
            '동일대상 최종등재건수': len(paired_last),
            '반출': out,
            '반입': inc,
            '최초차량 기준 연기': sum(r['처리구분'] == '연기' for r in first),
            '최종차량 기준 연기': delay_last,
            '최초차량 기준 취소': sum(r['처리구분'] == '취소' for r in first),
            '최종차량 기준 취소': cancel_last,
            '최종차량 상태혼합': sum(r['처리구분'] == '상태 혼합 확인' for r in last),
            '최종미존재': missing,
            '최종만 존재': extra,
            '원본등재 검산차이': final_listed - expected_listed,
            '일반상태 최종건수': sum(r['처리구분'] == '일반상태' for r in last),
        })
    return result


def show_table(rows, key, height=420):
    if not rows:
        st.info('조회 조건에 해당하는 내역이 없습니다.')
        return
    st.dataframe(rows, hide_index=True, use_container_width=True, height=height)
    st.download_button('이 표 CSV 내려받기', csv_bytes(rows), file_name=key + '.csv', mime='text/csv', key='download_' + key)

def render(mode, can_edit, actor, store):
    # ─────────────────────────────────────────────────────────────
    # 간편 UI: 조회 화면과 자료등록 화면을 분리해 한눈에 보이도록 구성
    # ─────────────────────────────────────────────────────────────
    st.markdown("""
    <style>
    .block-container {max-width: 1500px; padding-top: 1.4rem; padding-bottom: 3rem;}
    h1, h2, h3 {letter-spacing: -0.035em;}
    [data-testid="stMetric"] {
        background: #ffffff;
        border: 1px solid #e9edf2;
        border-radius: 16px;
        padding: 18px 18px 14px 18px;
        box-shadow: 0 2px 10px rgba(20,35,55,.04);
    }
    [data-testid="stMetricLabel"] {font-weight: 700;}
    [data-testid="stMetricValue"] {font-size: 1.9rem;}
    div[data-testid="stTabs"] button {font-weight: 700;}
    .dispatch-head {
        border: 1px solid #e9edf2;
        background: #ffffff;
        border-radius: 18px;
        padding: 20px 22px;
        margin-bottom: 16px;
        box-shadow: 0 2px 10px rgba(20,35,55,.04);
    }
    .dispatch-head .title {font-size: 1.45rem; font-weight: 800; letter-spacing: -.04em;}
    .dispatch-head .sub {font-size: .88rem; color: #6b7280; margin-top: 5px;}
    .soft-note {
        background: #f8fafc;
        border: 1px solid #edf1f5;
        border-radius: 12px;
        padding: 12px 14px;
        color: #667085;
        font-size: .86rem;
    }
    </style>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="dispatch-head">
      <div class="title">🚚 가전운송 배차 현황</div>
      <div class="sub">최초 배차와 변경 후 최종 배차를 납품번호 기준으로 비교합니다.</div>
    </div>
    """, unsafe_allow_html=True)

    repository = None
    base = None
    NEW_LABEL = '➕ 새 배차 등록'
    saved_days = []

    if store:
        try:
            repository = PairStore(store)
            saved_days = repository.list_ids()
        except Exception:
            st.error('저장된 배차자료를 읽을 수 없습니다. 데이터베이스 연결을 확인하세요.')
            return

    # 상단의 날짜 선택만 남기고 사이드바의 복잡한 필터는 제거
    top1, top2 = st.columns([1.15, 1.85])
    options = [NEW_LABEL] + saved_days
    selected = top1.selectbox(
        '배차일 선택',
        options,
        key='pair_day_simple',
        help='과거 자료를 보려면 날짜를 선택하고, 새 자료를 넣으려면 새 배차 등록을 선택하세요.'
    )

    if selected != NEW_LABEL and repository:
        try:
            base = repository.load(selected)
            versions = base.get('versions', [])
            if versions:
                hashes = [v['hash'] for v in versions]
                labels = {
                    v['hash']: (
                        v['등록시각(UTC)'][:19] + ' UTC'
                        + (' · ' + v['파일명'] if v.get('파일명') else '')
                    )
                    for v in versions
                }
                chosen = top2.selectbox(
                    '최종자료 버전',
                    hashes,
                    index=len(hashes) - 1,
                    format_func=lambda x: labels[x],
                    key='pair_version_simple_' + selected
                )
                base = repository.load(selected, chosen)
            if base is not None:
                base['comparison_date'] = selected
        except Exception:
            st.error('선택한 배차일의 자료를 불러오지 못했습니다.')
            return
    else:
        top2.markdown(
            '<div class="soft-note" style="margin-top:29px">새 배차자료는 아래 <b>자료등록</b> 탭에서 붙여넣으면 됩니다.</div>',
            unsafe_allow_html=True
        )

    if mode == 'demo':
        initial, final = demo_pair()
        work = {
            'selected': NEW_LABEL,
            'comparison_date': '',
            'initial': initial,
            'final': final,
            'initial_name': '가상 예시',
            'final_name': '가상 예시'
        }
    else:
        work = base
        preview = st.session_state.get('delivery_preview')
        if preview and preview.get('selected') == selected:
            work = preview

    # 분석결과는 한 번만 계산해서 모든 탭에서 공유
    result = None
    all_rows = []
    visible_day = ''
    if work:
        result = build_comparison_local(work['initial'], work['final'])
        all_rows = result['rows']
        visible_day = work.get('comparison_date', '') if isinstance(work, dict) else ''
        if not visible_day:
            days = sorted({r.get('배차일', '') for r in all_rows if r.get('배차일')})
            visible_day = days[0] if len(days) == 1 else ''

    tab_dash, tab_delivery, tab_vehicle, tab_input = st.tabs([
        '📊 한눈에 보기',
        '🔎 납품번호 조회',
        '🚛 차량별 현황',
        '➕ 자료등록'
    ])

    # ─────────────────────────────────────────────────────────────
    # 1. 한눈에 보기
    # ─────────────────────────────────────────────────────────────
    with tab_dash:
        if not work:
            st.info('저장된 배차일을 선택하거나, 「자료등록」 탭에서 새 배차자료를 등록하세요.')
        else:
            if visible_day:
                st.caption(f'조회 배차일 · {visible_day}')

            initial_count = sum(r['매칭구분'] != '최종만 존재' for r in all_rows)
            final_listed_count = sum(r['매칭구분'] != '최종미존재' for r in all_rows)
            delay_count = sum(r['처리구분'] == '연기' for r in all_rows)
            cancel_count = sum(r['처리구분'] == '취소' for r in all_rows)
            final_dispatch_count = max(0, final_listed_count - delay_count - cancel_count)
            moved_count = sum(r['차량 이동'] == '배차변경' for r in all_rows)

            # 가장 중요한 5개만 크게 표시
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric('최초 배차건수', f'{initial_count:,} 건')
            c2.metric('최종 배차건수', f'{final_dispatch_count:,} 건',
                      f'{final_dispatch_count - initial_count:+,} 건', delta_color='off')
            c3.metric('차량 변경', f'{moved_count:,} 건')
            c4.metric('연기', f'{delay_count:,} 건')
            c5.metric('취소', f'{cancel_count:,} 건')

            st.caption(
                f'최종 배차건수 = 최종 원본등재 {final_listed_count:,}건 '
                f'- 연기 {delay_count:,}건 - 취소 {cancel_count:,}건'
            )

            missing_count = sum(r['매칭구분'] == '최종미존재' for r in all_rows)
            extra_count = sum(r['매칭구분'] == '최종만 존재' for r in all_rows)
            if missing_count or extra_count:
                st.info(
                    f'참고 · 최초에만 존재 {missing_count:,}건 / 최종에만 존재 {extra_count:,}건 '
                    '→ 자동으로 취소 처리하지 않고 별도 확인합니다.'
                )

            st.divider()

            # 검색/필터도 한 줄로만
            f1, f2 = st.columns([1.5, 1])
            search = f1.text_input(
                '검색',
                placeholder='납품번호 또는 차량번호 검색',
                key='simple_search'
            ).strip()
            quick = f2.radio(
                '빠른 보기',
                ['전체', '차량변경', '연기', '취소'],
                horizontal=True,
                key='simple_quick'
            )

            filtered = []
            for r in all_rows:
                if search:
                    target = ' '.join([
                        r['납품번호 / Delivery'],
                        r['최초 차량번호'],
                        r['최종 차량번호']
                    ])
                    if search.lower() not in target.lower():
                        continue
                if quick == '차량변경' and r['차량 이동'] != '배차변경':
                    continue
                if quick == '연기' and r['처리구분'] != '연기':
                    continue
                if quick == '취소' and r['처리구분'] != '취소':
                    continue
                filtered.append(r)

            compact = [{
                '배차일': r.get('배차일', ''),
                '납품번호': r['납품번호 / Delivery'],
                '최초 차량': r['최초 차량번호'],
                '최종 차량': r['최종 차량번호'],
                '차량 이동': r['차량 이동'],
                '상태': r['처리구분'],
                'PDA': r['PDAStepStatus'],
            } for r in filtered]

            st.markdown(f'**배차내역 · {len(compact):,}건**')
            if compact:
                st.dataframe(compact, hide_index=True, use_container_width=True, height=520)
                st.download_button(
                    '현재 목록 CSV 내려받기',
                    csv_bytes(compact),
                    file_name=f'배차현황_{visible_day or "조회"}.csv',
                    mime='text/csv',
                    key='simple_download'
                )
            else:
                st.info('조건에 맞는 내역이 없습니다.')

    # ─────────────────────────────────────────────────────────────
    # 2. 납품번호 조회
    # ─────────────────────────────────────────────────────────────
    with tab_delivery:
        if not work:
            st.info('먼저 저장된 날짜를 선택하거나 새 자료를 등록하세요.')
        else:
            st.subheader('납품번호 하나만 빠르게 확인')
            q = st.text_input(
                '납품번호 / Delivery',
                placeholder='납품번호 입력',
                key='delivery_exact_search'
            ).strip()

            candidates = all_rows
            if q:
                candidates = [r for r in all_rows if q in r['납품번호 / Delivery']]

            if not q:
                st.caption('납품번호를 입력하면 최초 차량 → 최종 차량과 연기·취소 여부를 바로 보여줍니다.')
            elif not candidates:
                st.warning('해당 납품번호를 찾지 못했습니다.')
            else:
                choices = [r['납품번호 / Delivery'] for r in candidates]
                delivery = st.selectbox('조회 결과', choices, key='delivery_exact_select')
                detail = next(r for r in candidates if r['납품번호 / Delivery'] == delivery)

                d1, d2, d3, d4 = st.columns(4)
                d1.metric('최초 차량', detail['최초 차량번호'] or '없음')
                d2.metric('최종 차량', detail['최종 차량번호'] or '없음')
                d3.metric('차량 이동', detail['차량 이동'])
                d4.metric('처리상태', detail['처리구분'])

                st.markdown('**상세정보**')
                detail_view = {
                    '배차일': detail.get('배차일', ''),
                    '납품번호 / Delivery': detail['납품번호 / Delivery'],
                    '최초 차량번호': detail['최초 차량번호'],
                    '최종 차량번호': detail['최종 차량번호'],
                    'PDAStepStatus': detail['PDAStepStatus'] or '(빈값 · 일반상태)',
                    '처리구분': detail['처리구분'],
                    '매칭구분': detail['매칭구분'],
                    '확인사항': detail['확인사항'],
                }
                st.dataframe([detail_view], hide_index=True, use_container_width=True)

                if detail['차량 이동'] == '배차변경':
                    st.success(
                        f"{detail['최초 차량번호']} → {detail['최종 차량번호']} 로 배차가 변경되었습니다."
                    )

    # ─────────────────────────────────────────────────────────────
    # 3. 차량별 현황
    # ─────────────────────────────────────────────────────────────
    with tab_vehicle:
        if not work:
            st.info('먼저 저장된 날짜를 선택하거나 새 자료를 등록하세요.')
        else:
            vehicle_rows_all = vehicle_summary_local(all_rows)
            st.subheader('차량별 배차 변동 한눈에 보기')
            st.caption('배차변경 = 해당 차량의 반출 + 반입 건수입니다. 연기·취소는 최종 차량 기준으로 집계합니다.')

            # ── 차량 하나를 선택하면 핵심 수치를 큰 카드로 표시
            vehicle_names = [r['차량번호'] for r in vehicle_rows_all]
            selected_vehicle = st.selectbox(
                '차량 선택',
                vehicle_names,
                key='vehicle_focus_select'
            )
            focus = next((r for r in vehicle_rows_all if r['차량번호'] == selected_vehicle), None)

            if focus:
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric('최초 배차', f"{focus['최초 배차건수']:,} 건")
                c2.metric(
                    '최종 배차',
                    f"{focus['최종 배차건수']:,} 건",
                    f"{focus['배차 증감']:+,} 건",
                    delta_color='off'
                )
                c3.metric(
                    '배차변경',
                    f"{focus['배차변경']:,} 건",
                    f"반출 {focus['반출']:,} · 반입 {focus['반입']:,}",
                    delta_color='off'
                )
                c4.metric('연기', f"{focus['연기 제외']:,} 건")
                c5.metric('취소', f"{focus['취소 제외']:,} 건")

                # 선택 차량의 실제 변동 납품번호를 바로 확인
                focus_events = []
                for r in all_rows:
                    initial_hit = selected_vehicle in r['최초 차량번호'].split(' / ')
                    final_hit = selected_vehicle in r['최종 차량번호'].split(' / ')
                    is_change = r['차량 이동'] == '배차변경' and (initial_hit or final_hit)
                    is_delay = r['처리구분'] == '연기' and final_hit
                    is_cancel = r['처리구분'] == '취소' and final_hit
                    if not (is_change or is_delay or is_cancel):
                        continue

                    kinds = []
                    if is_change:
                        if initial_hit:
                            kinds.append('배차변경 반출')
                        if final_hit:
                            kinds.append('배차변경 반입')
                    if is_delay:
                        kinds.append('연기')
                    if is_cancel:
                        kinds.append('취소')

                    focus_events.append({
                        '납품번호': r['납품번호 / Delivery'],
                        '구분': ' / '.join(kinds),
                        '최초 차량': r['최초 차량번호'],
                        '최종 차량': r['최종 차량번호'],
                        'PDA': r['PDAStepStatus'] or '(빈값)',
                    })

                with st.expander(f'{selected_vehicle} 변동 상세 · {len(focus_events):,}건'):
                    if focus_events:
                        st.dataframe(
                            focus_events,
                            hide_index=True,
                            use_container_width=True,
                            height=min(420, 42 + len(focus_events) * 35)
                        )
                    else:
                        st.info('이 차량에는 배차변경·연기·취소가 없습니다.')

            st.divider()

            # ── 전체 차량 비교: 변동이 있는 차량을 한 차트에 표시
            h1, h2 = st.columns([1.2, 1])
            vsearch = h1.text_input(
                '차량번호 검색',
                placeholder='예: 경북80아9992',
                key='vehicle_issue_search'
            ).strip()
            issue_only = h2.toggle(
                '변동 있는 차량만',
                value=True,
                key='vehicle_issue_only'
            )

            vehicle_rows = vehicle_rows_all
            if vsearch:
                vehicle_rows = [
                    r for r in vehicle_rows
                    if vsearch.lower() in r['차량번호'].lower()
                ]
            if issue_only:
                vehicle_rows = [r for r in vehicle_rows if r['변동합계'] > 0]

            # 변동합계가 큰 차량부터 보여줌
            vehicle_rows = sorted(
                vehicle_rows,
                key=lambda r: (-r['변동합계'], -r['배차변경'], r['차량번호'])
            )

            chart_rows = vehicle_rows[:25]
            if chart_rows:
                points = []
                for r in chart_rows:
                    points.extend([
                        {'차량번호': r['차량번호'], '구분': '배차변경', '건수': r['배차변경']},
                        {'차량번호': r['차량번호'], '구분': '연기', '건수': r['연기 제외']},
                        {'차량번호': r['차량번호'], '구분': '취소', '건수': r['취소 제외']},
                    ])

                st.markdown('**차량별 배차변경 · 연기 · 취소**')
                st.vega_lite_chart(
                    spec={
                        'data': {'values': points},
                        'mark': {'type': 'bar', 'cornerRadiusEnd': 3},
                        'encoding': {
                            'x': {
                                'field': '차량번호',
                                'type': 'nominal',
                                'sort': [r['차량번호'] for r in chart_rows],
                                'axis': {'labelAngle': -40, 'title': None}
                            },
                            'xOffset': {'field': '구분'},
                            'y': {
                                'field': '건수',
                                'type': 'quantitative',
                                'axis': {'title': '건수'},
                                'scale': {'domainMin': 0}
                            },
                            'color': {
                                'field': '구분',
                                'type': 'nominal',
                                'legend': {'orient': 'top', 'title': None}
                            },
                            'tooltip': [
                                {'field': '차량번호', 'type': 'nominal'},
                                {'field': '구분', 'type': 'nominal'},
                                {'field': '건수', 'type': 'quantitative'}
                            ]
                        }
                    },
                    use_container_width=True
                )
                st.caption('변동합계가 많은 차량 순으로 최대 25대를 표시합니다.')
            else:
                st.info('조건에 맞는 차량 변동내역이 없습니다.')

            # ── 표도 핵심 정보만
            compact_vehicle = [{
                '차량번호': r['차량번호'],
                '배차변경': r['배차변경'],
                '반출': r['반출'],
                '반입': r['반입'],
                '연기': r['연기 제외'],
                '취소': r['취소 제외'],
                '변동합계': r['변동합계'],
                '최초 배차': r['최초 배차건수'],
                '최종 배차': r['최종 배차건수'],
                '배차 증감': r['배차 증감'],
            } for r in vehicle_rows]

            st.markdown(f'**차량별 요약 · {len(compact_vehicle):,}대**')
            if compact_vehicle:
                st.dataframe(
                    compact_vehicle,
                    hide_index=True,
                    use_container_width=True,
                    height=520,
                    column_config={
                        '배차변경': st.column_config.NumberColumn(help='반출 + 반입'),
                        '연기': st.column_config.NumberColumn(help='최종 차량 기준 L·7'),
                        '취소': st.column_config.NumberColumn(help='최종 차량 기준 3·P'),
                        '변동합계': st.column_config.NumberColumn(help='배차변경 + 연기 + 취소'),
                    }
                )
                st.download_button(
                    '차량별 요약 CSV 내려받기',
                    csv_bytes(compact_vehicle),
                    file_name=f'차량별_배차변동_{visible_day or "조회"}.csv',
                    mime='text/csv',
                    key='vehicle_issue_download'
                )
            else:
                st.info('표시할 차량이 없습니다.')

    # ─────────────────────────────────────────────────────────────
    # 4. 자료등록
    # ─────────────────────────────────────────────────────────────
    with tab_input:
        st.subheader('새 배차자료 등록')
        st.caption('Excel 파일 자체는 올리지 않고 필요한 열만 복사해서 붙여넣습니다.')

        if mode == 'demo':
            st.info('현재는 예시 모드입니다.')
            return
        if not can_edit:
            st.warning('현재 계정은 조회 전용입니다. 자료 등록 권한이 없습니다.')
            return

        default_day = (
            date.fromisoformat(selected)
            if selected != NEW_LABEL
            else datetime.now(ZoneInfo("Asia/Seoul")).date()
        )
        paste_day = st.date_input(
            '배차일',
            value=default_day,
            key='simple_paste_day',
            help='이 날짜 기준으로 저장되고 나중에 다시 조회할 수 있습니다.'
        )

        st.markdown('### 1. 최초 배차 · 상세정보')
        a1, a2 = st.columns(2)
        paste_initial_id = a1.text_area(
            '납품번호',
            height=180,
            placeholder='Excel의 납품번호 열을 복사해서 붙여넣기',
            key='simple_initial_delivery'
        )
        paste_initial_vehicle = a2.text_area(
            '배차차량',
            height=180,
            placeholder='Excel의 배차차량 열을 복사해서 붙여넣기',
            key='simple_initial_vehicle'
        )
        if base:
            st.caption('과거 배차일을 선택한 상태라면 위 두 칸을 비워두고 최종자료만 새로 입력해도 기존 최초배차를 사용합니다.')

        st.markdown('### 2. 최종 배차 · 최종리스트')
        b1, b2, b3 = st.columns(3)
        paste_final_id = b1.text_area(
            'Delivery',
            height=190,
            placeholder='Delivery 열 붙여넣기',
            key='simple_final_delivery'
        )
        paste_final_vehicle = b2.text_area(
            'Vehicle Number(Full)',
            height=190,
            placeholder='최종 차량번호 열 붙여넣기',
            key='simple_final_vehicle'
        )
        paste_final_status = b3.text_area(
            'PDAStepStatus',
            height=190,
            placeholder='상태코드 열 붙여넣기\n빈값도 정상입니다.',
            key='simple_final_status'
        )
        st.caption('판정기준 · L·7 = 연기 / 3·P = 취소 / 빈값과 그 외 코드는 일반상태')

        final_ready = bool(paste_final_id.strip() and paste_final_vehicle.strip())
        initial_ready = bool(paste_initial_id.strip() and paste_initial_vehicle.strip()) or base is not None

        if st.button(
            '분석하기',
            type='primary',
            use_container_width=True,
            disabled=not (initial_ready and final_ready),
            key='simple_analyze'
        ):
            try:
                first_doc, last_doc = read_pasted_columns(
                    paste_initial_id,
                    paste_initial_vehicle,
                    paste_final_id,
                    paste_final_vehicle,
                    paste_final_status,
                    use_saved_initial=(
                        base['initial']
                        if base is not None and not (paste_initial_id.strip() or paste_initial_vehicle.strip())
                        else None
                    ),
                    comparison_date=paste_day.isoformat()
                )
                st.session_state['delivery_preview'] = {
                    'selected': selected,
                    'comparison_date': paste_day.isoformat(),
                    'initial': first_doc,
                    'final': last_doc,
                    'initial_name': (
                        base['initial_name']
                        if base is not None and not (paste_initial_id.strip() or paste_initial_vehicle.strip())
                        else '상세정보 · 복사붙여넣기'
                    ),
                    'final_name': '최종리스트 · 복사붙여넣기'
                }
                st.session_state['simple_analyzed_notice'] = True
                st.rerun()
            except CompareError as exc:
                st.error(str(exc))

        if st.session_state.pop('simple_analyzed_notice', False):
            st.success('분석 완료 · 「한눈에 보기」 탭에서 결과를 확인하세요.')

        # 현재 미리보기가 있으면 저장 버튼만 단순하게 표시
        preview = st.session_state.get('delivery_preview')
        current = preview if preview and preview.get('selected') == selected else work
        if current and repository:
            st.divider()
            st.markdown('### 3. 저장')
            save_day_text = current.get('comparison_date', paste_day.isoformat())
            try:
                save_day_default = date.fromisoformat(save_day_text)
            except Exception:
                save_day_default = paste_day

            save_day = st.date_input(
                '저장할 배차일',
                value=save_day_default,
                key='simple_save_day'
            )
            confirmed = st.checkbox(
                '분석 결과를 확인했고 이 배차일로 저장합니다.',
                key='simple_save_confirm'
            )
            if st.button(
                'Supabase에 저장',
                type='primary',
                use_container_width=True,
                disabled=not confirmed,
                key='simple_save'
            ):
                try:
                    n = repository.save(
                        save_day.isoformat(),
                        current['initial'],
                        current['final'],
                        actor,
                        current['initial_name'],
                        current['final_name']
                    )
                    st.success(f'저장 완료 · 신규 데이터셋 {n}개')
                except CompareError as exc:
                    st.error(str(exc))
                except Exception:
                    st.error('저장에 실패했습니다. 데이터베이스 연결을 확인하세요.')

        # CSV는 숨겨진 보조 기능으로만 유지
        with st.expander('기타 · CSV 업로드 / 백업'):
            st.caption('회사 환경에서 CSV가 정상적으로 읽히는 경우에만 사용하세요.')
            c1, c2 = st.columns(2)
            first_upload = c1.file_uploader(
                '상세정보.csv',
                type=['csv'],
                key='simple_csv_initial'
            )
            last_upload = c2.file_uploader(
                '최종리스트.csv',
                type=['csv'],
                key='simple_csv_final'
            )
            if st.button(
                'CSV 분석',
                disabled=last_upload is None or (first_upload is None and base is None),
                key='simple_csv_analyze'
            ):
                try:
                    first_doc = (
                        read_csv_source(first_upload.getvalue(), 'initial', first_upload.name)
                        if first_upload else base['initial']
                    )
                    last_doc = read_csv_source(last_upload.getvalue(), 'final', last_upload.name)
                    st.session_state['delivery_preview'] = {
                        'selected': selected,
                        'comparison_date': selected if selected != NEW_LABEL else '',
                        'initial': first_doc,
                        'final': last_doc,
                        'initial_name': first_upload.name if first_upload else base['initial_name'],
                        'final_name': last_upload.name
                    }
                    st.rerun()
                except CompareError as exc:
                    st.error(str(exc))

            if current:
                backup_day = current.get('comparison_date', '')
                backup = json.dumps({
                    'schema_version': 3,
                    'comparison_date': backup_day,
                    'initial': current['initial'],
                    'final': current['final'],
                    'delay_codes': list(DELAY_CODES),
                    'cancel_codes': list(CANCEL_CODES)
                }, ensure_ascii=False, indent=2).encode('utf8')
                st.download_button(
                    '현재 비교자료 JSON 백업',
                    backup,
                    file_name='납품번호비교_백업.json',
                    mime='application/json',
                    key='simple_backup'
                )

