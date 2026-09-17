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

def build_comparison_local(initial, final):
    """기존 delivery_compare.py 버전과 무관하게 현재 업무 기준으로 판정합니다."""
    delays = set(DELAY_CODES)
    cancels = set(CANCEL_CODES)
    a, b = aggregate(initial), aggregate(final)
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
        elif not codes or '' in codes:
            status = '상태 확인필요'
        elif all(x in delays for x in codes):
            status = '연기'
        elif all(x in cancels for x in codes):
            status = '취소'
        elif any(x in delays or x in cancels for x in codes):
            status = '상태 혼합 확인'
        else:
            status = '일반상태'
        rows.append({
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
    metrics = {
        '최초 납품번호': len(a), '최종 Delivery': len(b), '양쪽 일치': matched,
        '차량 변경': sum(r['차량 이동'] == '배차변경' for r in rows),
        '연기': sum(r['처리구분'] == '연기' for r in rows),
        '취소': sum(r['처리구분'] == '취소' for r in rows),
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
        result.append({
            '차량번호': v, '최초 납품건수': len(first), '최종 등재건수': len(last),
            '등재 증감': len(last) - len(first), '동일대상 최초건수': len(paired_first), '동일대상 최종건수': len(paired_last),
            '반출': out, '반입': inc,
            '최초차량 기준 연기': sum(r['처리구분'] == '연기' for r in first),
            '최종차량 기준 연기': sum(r['처리구분'] == '연기' for r in last),
            '최초차량 기준 취소': sum(r['처리구분'] == '취소' for r in first),
            '최종차량 기준 취소': sum(r['처리구분'] == '취소' for r in last),
            '최종차량 상태혼합': sum(r['처리구분'] == '상태 혼합 확인' for r in last),
            '최종미존재': missing, '최종만 존재': extra, '등재건수 검산차이': len(last) - expected,
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
    st.subheader('상세정보 → 최종리스트 · 납품번호별 차량 이동')
    st.caption('최초: 상세정보.csv의 납품번호·차량번호 / 최종: 최종리스트.csv의 Delivery·Vehicle Number(Full)')
    repository = None
    base = None
    selected = '새 파일 비교'
    if store:
        try:
            repository = PairStore(store)
            selected = st.sidebar.selectbox('저장한 비교일', ['새 파일 비교'] + repository.list_ids(), key='pair_id')
            if selected != '새 파일 비교':
                base = repository.load(selected)
                versions = base['versions']
                if versions:
                    hashes = [v['hash'] for v in versions]
                    version_label = {v['hash']: v['등록시각(UTC)'][:19] + ' UTC · ' + v['파일명'] for v in versions}
                    chosen = st.sidebar.selectbox('최종 파일 등록 버전', hashes, index=len(hashes)-1, format_func=lambda x: version_label[x], key='pair_version_' + selected)
                    base = repository.load(selected, chosen)
        except Exception:
            st.error('납품번호 비교 저장소를 읽을 수 없습니다. 연결정보와 데이터베이스 권한을 확인하세요.')
            return
    if mode == 'demo':
        initial, final = demo_pair()
        work = {'initial': initial, 'final': final, 'initial_name': '가상 예시', 'final_name': '가상 예시'}
    else:
        work = base
        preview = st.session_state.get('delivery_preview')
        if preview and preview.get('selected') == selected:
            work = preview
        with st.expander('① CSV 파일 등록 · 상세정보.csv + 최종리스트.csv', expanded=work is None):
            st.info('Excel에서 CSV 또는 CSV UTF-8로 저장한 두 파일을 올리세요. UTF-8·CP949·EUC-KR·UTF-16을 자동 인식합니다. 필요한 납품번호·차량·품목·상태 열만 읽고 고객명·주소·전화번호는 저장하지 않습니다.')
            if not can_edit:
                st.caption('조회 전용 계정입니다. 편집 권한이 있는 담당자가 파일을 등록해야 합니다.')
            else:
                st.caption('업로드한 파일은 앱 운영 서버로 전송됩니다. 회사에서 승인한 환경에서만 사용하세요.')
                c1, c2 = st.columns(2)
                first_upload = c1.file_uploader('최초배차 · 상세정보.csv', type=['csv'], key='pair_first_upload')
                last_upload = c2.file_uploader('최종배차 · 최종리스트.csv', type=['csv'], key='pair_final_upload')
                if base:
                    st.caption('최초 파일을 생략하면 선택한 비교일에 고정된 상세정보를 사용합니다.')
                if st.button('두 파일 분석하기', type='primary', disabled=last_upload is None or (first_upload is None and base is None), key='pair_analyze'):
                    try:
                        first_doc = read_csv_source(first_upload.getvalue(), 'initial', first_upload.name) if first_upload else base['initial']
                        last_doc = read_csv_source(last_upload.getvalue(), 'final', last_upload.name)
                        work = {'selected': selected, 'initial': first_doc, 'final': last_doc,
                            'initial_name': first_upload.name if first_upload else base['initial_name'], 'final_name': last_upload.name}
                        st.session_state['delivery_preview'] = work
                        st.success('분석했습니다. 아직 저장소에 등록하지 않은 미리보기입니다.')
                    except CompareError as exc:
                        st.error(str(exc))
        if work is None:
            st.info('상세정보.csv와 최종리스트.csv를 올린 뒤 「두 파일 분석하기」를 누르세요. 저장된 자료는 왼쪽에서 선택할 수 있습니다.')
            return
    st.sidebar.caption('상태 기준: L·7 = 연기 / 3·P = 취소')
    result = build_comparison_local(work['initial'], work['final'])
    all_rows = result['rows']
    st.caption(f'원본: {work["initial_name"]} / {work["final_name"]} · 최초 완료시각은 파일에서 확인되지 않아 임의 생성하지 않습니다.')
    date_counts = Counter(r.get('op_date') for r in work['final']['rows'] if r.get('op_date'))
    if date_counts:
        st.caption('최종 PlannedGIDate(CBO) 분포: ' + ', '.join(f'{d} ({n:,}품목행)' for d, n in date_counts.most_common()))
        if len(date_counts) > 1:
            st.warning('최종 파일에 여러 계획일이 포함되어 있습니다. 납품번호별 통합 비교이므로 두 파일의 조회 기간·대상 범위를 확인하세요.')
    st.info('최종리스트는 품목 여러 행을 Delivery 1건으로 묶습니다. 차량 이동과 처리상태는 별도로 표시합니다.')
    st.caption('PDAStepStatus 판정: L·7 = 연기 / 3·P = 취소. 원문 PDAStatusTxt도 함께 표시합니다.')
    # Explicit, consistent filtering across every tab and KPI.
    query = st.sidebar.text_input('납품번호 / Delivery 검색', key='pair_delivery_search').strip()
    vehicles = sorted({v for r in all_rows for key in ('최초 차량번호', '최종 차량번호') for v in r[key].split(' / ') if v})
    truck = st.sidebar.selectbox('차량번호 조회', ['전체'] + vehicles, key='pair_truck')
    direction = st.sidebar.radio('차량 조회 기준', ['최초 또는 최종', '최초 차량', '최종 차량'], key='pair_truck_direction')
    zones = sorted({r['최초 ZONE'] for r in all_rows if r['최초 ZONE']})
    zone = st.sidebar.selectbox('최초 ZONE', ['전체'] + zones, key='pair_zone')
    mapping = st.sidebar.multiselect('매칭구분', ['양쪽 일치', '최종미존재', '최종만 존재'], default=['양쪽 일치', '최종미존재', '최종만 존재'], key='pair_matching')
    only_move = st.sidebar.checkbox('차량 변경만', key='pair_only_move')
    only_delay = st.sidebar.checkbox('연기만', key='pair_only_delay')
    only_cancel = st.sidebar.checkbox('취소만', key='pair_only_cancel')
    rows = []
    for r in all_rows:
        if query and query not in r['납품번호 / Delivery']:
            continue
        if r['매칭구분'] not in mapping or (zone != '전체' and r['최초 ZONE'] != zone):
            continue
        truck_keys = ['최초 차량번호', '최종 차량번호'] if direction == '최초 또는 최종' else ['최초 차량번호' if direction == '최초 차량' else '최종 차량번호']
        if truck != '전체' and not any(truck in r[k].split(' / ') for k in truck_keys):
            continue
        if only_move and r['차량 이동'] != '배차변경':
            continue
        if only_delay and not only_cancel and r['처리구분'] != '연기':
            continue
        if only_cancel and not only_delay and r['처리구분'] != '취소':
            continue
        if only_delay and only_cancel and r['처리구분'] not in ('연기', '취소'):
            continue
        rows.append(r)
    st.sidebar.caption('모든 표·지표는 위 검색과 필터 결과에 함께 적용됩니다. 셀 소속은 추정하지 않고 원본 ZONE만 표시합니다.')
    initial_count = sum(r['매칭구분'] != '최종만 존재' for r in rows)
    final_count = sum(r['매칭구분'] != '최종미존재' for r in rows)
    stats = [('최초 납품건', initial_count), ('최종 등재건', final_count), ('차량 변경', sum(r['차량 이동'] == '배차변경' for r in rows)),
             ('연기', sum(r['처리구분'] == '연기' for r in rows)), ('취소', sum(r['처리구분'] == '취소' for r in rows)),
             ('최종미존재', sum(r['매칭구분'] == '최종미존재' for r in rows)), ('최종만 존재', sum(r['매칭구분'] == '최종만 존재' for r in rows))]
    for col, (label, value) in zip(st.columns(7), stats):
        col.metric(label, f'{value:,} 건')
    st.caption(f'현재 필터: 고유 납품번호 {len(rows):,}건 · 양쪽 일치 {sum(r["매칭구분"] == "양쪽 일치" for r in rows):,}건 · 상태 혼합 확인 {sum(r["처리구분"] == "상태 혼합 확인" for r in rows):,}건.')
    if result['metrics']['최종미존재'] or result['metrics']['최종만 존재']:
        st.warning('한쪽 파일에만 존재하는 건은 별도 표시합니다. 최종에 없다는 이유만으로 취소·연기로 분류하지 않습니다. 두 파일의 조회 대상·기간을 확인하세요.')
    t1, t2, t3, t4, t5 = st.tabs(['납품번호 조회', '차량 이동·반출입', '연기·취소 현황', '차량별 비교', '등록·검증 기준'])
    with t1:
        st.subheader('최초 차량에서 최종 어떤 차량으로 갔는지 확인')
        show_table(rows, '납품번호별_차량조회', 460)
        if rows:
            delivery = st.selectbox('납품번호 상세 확인', [r['납품번호 / Delivery'] for r in rows], key='pair_detail_id')
            detail = next(r for r in rows if r['납품번호 / Delivery'] == delivery)
            c1, c2, c3 = st.columns(3)
            c1.metric('최초 차량', detail['최초 차량번호'] or '원본에 없음')
            c2.metric('최종 차량', detail['최종 차량번호'] or '원본에 없음')
            c3.metric('상태', detail['처리구분'])
            st.write('차량 이동: ' + detail['차량 이동'] + ' / PDAStepStatus: ' + (detail['PDAStepStatus'] or '없음'))
            st.caption('원문 상태: ' + (detail['PDAStatusTxt(원문)'] or '없음'))
            st.caption('근거 행(헤더 포함 CSV 행번호) · 상세정보: ' + (detail['최초 근거행'] or '없음') + ' / 최종리스트: ' + (detail['최종 근거행'] or '없음'))
            with st.expander('해당 납품번호 품목별 최종 원본값'):
                item_rows = [{'Delivery': r['id'], 'item': r.get('item'), '최종 차량': r['vehicle'], 'Material': r.get('model'), 'Qty(첫 번째 열)': r.get('qty'), 'PDAStepStatus': r.get('code'), 'PDAStatusTxt': r.get('description'), 'CSV행': r['row']} for r in work['final']['rows'] if r['id'] == delivery]
                show_table(item_rows, '선택납품_최종품목', 220)
    with t2:
        st.subheader('최초 차량 → 최종 차량 · 이동 요약')
        moved = [r for r in rows if r['차량 이동'] == '배차변경']
        show_table(transfer_summary_local(moved), '차량이동_방향별요약', 320)
        st.subheader('이동한 납품번호 목록')
        show_table(moved, '차량변경_납품상세')
        st.caption('두 시점의 차량 차이입니다. 중간에 A→B→A로 돌아온 이력이나 정확한 이동시각은 두 파일만으로 알 수 없습니다.')
    with t3:
        st.subheader('연기 · 고유 Delivery 기준')
        delay_rows = [r for r in rows if r['처리구분'] == '연기']
        delay_counts = Counter(r['PDAStepStatus'] for r in delay_rows)
        st.write(' · '.join(f'{code}: {n:,}건' for code, n in sorted(delay_counts.items())) or '연기 대상 없음')
        show_table(delay_rows, '연기_납품상세', 280)
        st.subheader('취소 · 고유 Delivery 기준')
        cancel_rows = [r for r in rows if r['처리구분'] == '취소']
        cancel_counts = Counter(r['PDAStepStatus'] for r in cancel_rows)
        st.write(' · '.join(f'{code}: {n:,}건' for code, n in sorted(cancel_counts.items())) or '취소 대상 없음')
        show_table(cancel_rows, '취소_납품상세', 280)
        mixed_rows = [r for r in rows if r['처리구분'] == '상태 혼합 확인']
        if mixed_rows:
            st.subheader('상태 혼합 · 확인 필요')
            show_table(mixed_rows, '상태혼합_확인필요', 220)
        st.caption('고정 판정 규칙: L·7은 연기, 3·P는 취소입니다. 그 외 코드는 일반상태로 두며 임의로 완료라고 단정하지 않습니다.')
    with t4:
        st.subheader('차량별 최초·최종 등재건수와 반출입')
        vehicle_rows = vehicle_summary_local(rows)
        chart_rows = sorted(vehicle_rows, key=lambda x: abs(x['등재 증감']), reverse=True)[:20]
        if chart_rows:
            points = [{'차량번호': r['차량번호'], '시점': stage, '납품건수': r[key]} for r in chart_rows for stage, key in [('최초', '최초 납품건수'), ('최종', '최종 등재건수')]]
            st.vega_lite_chart(spec={'data': {'values': points}, 'mark': 'bar', 'encoding': {
                'x': {'field': '차량번호', 'type': 'nominal', 'axis': {'labelAngle': -45}},
                'xOffset': {'field': '시점'}, 'y': {'field': '납품건수', 'type': 'quantitative'},
                'color': {'field': '시점', 'type': 'nominal'}, 'tooltip': [{'field': '차량번호'}, {'field': '시점'}, {'field': '납품건수'}]}}, use_container_width=True)
        show_table(vehicle_rows, '차량별_최초최종비교')
        st.caption('등재건수 검산은 원본 등재 여부와 차량 이동을 기준으로 합니다. 연기·취소는 PDAStepStatus 판정값으로 별도 집계합니다.')
        st.caption('복수 차량으로 분할된 Delivery는 각 차량에 1건씩 포함되므로 차량 합계가 고유 Delivery 합계보다 클 수 있습니다. 해당 건은 확인사항에 표시합니다.')
    with t5:
        st.subheader('원본·중복·상태 검증')
        st.write(result['metrics'])
        issues = [r for r in rows if r['확인사항']]
        if issues:
            show_table(issues, '확인필요_납품목록', 240)
        st.markdown('''**집계 기준**  
- 납품번호 = Delivery. 숫자형·문자형 차이와 공백을 정리하되 문자 식별자의 앞자리 0은 임의 삭제하지 않습니다.
- 최종 차량은 Vehicle Number(Full). 번호 뒷자리만으로 차량을 연결하지 않습니다.
- Delivery 여러 품목행은 납품 1건. 품목별 상태가 섞이면 상태 혼합 확인으로 표시합니다.
- **L·7 = 연기 / 3·P = 취소**로 판정합니다. 원문 상태 설명은 그대로 유지합니다.
- 차량 변경과 처리상태는 독립 분류입니다. 누락·추가는 조회 범위 차이일 수 있습니다.
- 최초 파일은 사용자 지정 기준본이며 정확한 최초 완료시각은 미제공입니다. 파일 등록시각을 완료시각으로 대체하지 않습니다.
- 최종의 Qty 헤더가 두 번 나오면 품목 수량인 첫 번째 Qty 열만 사용합니다.
- 정확히 같은 품목 중복은 수량 계산에서 한 번만 사용합니다. 같은 item의 상충 내용은 확인 대상으로 표시합니다.
- 원본 두 파일은 전체 화면의 기준이며 임의로 같은 건수에 맞추거나 고객 방문수로 환산하지 않습니다.''')
        if mode != 'demo' and repository and can_edit:
            st.subheader('② 비교일별 저장 · 최초 고정 / 최종 버전 누적')
            if len(date_counts) == 1:
                try:
                    suggested = date.fromisoformat(next(iter(date_counts)))
                except ValueError:
                    suggested = datetime.now(ZoneInfo("Asia/Seoul")).date()
            else:
                suggested = datetime.now(ZoneInfo("Asia/Seoul")).date()
            save_day = st.date_input('비교일(자료 분류용, 완료시각 아님)', value=date.fromisoformat(selected) if selected != '새 파일 비교' else suggested, key='pair_save_date')
            if len(date_counts) != 1 or save_day.isoformat() not in date_counts:
                st.warning('저장할 비교일과 원본 계획일을 확인하세요. 여러 날짜가 포함된 파일은 적절한 범위로 다시 내보내는 것이 좋습니다.')
            confirmed = st.checkbox('비교일·조회 범위를 확인했습니다. 최초 원본을 고정하고 최종 파일을 새 버전으로 저장합니다.', key='pair_confirm')
            if st.button('비교자료 저장', disabled=not confirmed, type='primary', key='pair_save'):
                try:
                    n = repository.save(save_day.isoformat(), work['initial'], work['final'], actor, work['initial_name'], work['final_name'])
                    st.success(f'등록 완료 · 신규 데이터셋 {n}개. 같은 내용은 중복 저장하지 않았습니다.')
                except CompareError as exc:
                    st.error(str(exc))
                except Exception:
                    st.error('저장에 실패했습니다. 파일 전체를 반영하지 않았습니다. 연결과 권한을 확인하세요.')
            backup = json.dumps({'schema_version': 3, 'comparison_date': save_day.isoformat(), 'initial': work['initial'], 'final': work['final'], 'delay_codes': list(DELAY_CODES), 'cancel_codes': list(CANCEL_CODES)}, ensure_ascii=False, indent=2).encode('utf8')
            st.download_button('현재 비교 원자료 JSON 백업', backup, file_name='납품번호비교_백업.json', mime='application/json')
            st.caption('고객명·주소·전화번호를 제외한 비교용 필드만 저장합니다. 데이터베이스 전체 보존·복구는 관리자의 DB 백업으로 수행하세요. 예시/조회용 CSV만으로는 최초 데이터셋 복구가 되지 않습니다.')
        elif mode == 'demo':
            st.info('가상 예시 모드입니다. 실제 사용은 승인된 로그인·저장소 설정 또는 PC 로컬 모드에서 가능합니다.')
