from __future__ import annotations
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
import streamlit as st
from core import SH, EH, METRICS, CATS, InputError, compare, demo_data, parse_file, merge_data, raw_rows, csv_bytes, date_value
from storage import Store
st.set_page_config(page_title='가전운송계획 · 배차 비교', page_icon='🚚', layout='wide')
st.markdown('<style>\n.block-container{padding-top:2rem;max-width:1600px}\n[data-testid="stMetric"]{border:1px solid #e1e7ed;border-radius:12px;padding:16px;background:#fafcfd}\n[data-testid="stMetricLabel"]{font-size:0.9rem}\n[data-testid="stMetricValue"]{font-size:2rem}\n</style>', unsafe_allow_html=True)
st.title('가전운송계획 | 최초·최종 배차 비교')
st.caption('차량별 첫 완료 기록을 보존하고, 연기·취소·배차변경 이후의 현황과 대조합니다. 모든 업무시각은 한국 표준시입니다.')
try:
    cfg = dict(st.secrets.get('app', {}))
except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
    cfg = {}
local = os.environ.get('DISPATCH_LOCAL') == '1'
mode = 'local' if local else cfg.get('mode', 'demo')
if mode not in ('local', 'demo', 'live'):
    st.error('app.mode 설정은 demo 또는 live여야 합니다.')
    st.stop()
actor = 'local-test'
can_edit = local
store = None
if mode == 'live':
    if not st.user.is_logged_in:
        st.info('업무자료는 승인된 계정으로 로그인한 뒤에만 표시됩니다.')
        if st.button('업무 계정으로 로그인', type='primary'):
            try:
                st.login()
            except Exception:
                st.error('관리자가 OIDC 로그인 설정을 먼저 완료해야 합니다.')
        st.stop()
    actor = str(st.user.get('email', '')).lower().strip()
    allowed = {str(x).lower().strip() for x in cfg.get('allowed_emails', [])}
    if not actor or st.user.get('email_verified') is not True or actor not in allowed:
        # Fail closed. Only the signed-in user's email and diagnostic codes are shown.
        # Never print st.secrets, the full user object, or authentication tokens.
        st.error('접근 권한 확인이 필요합니다. 아래 진단번호를 확인해 주세요.')
        st.text('현재 로그인된 이메일: ' + (actor or '(전달되지 않음)'))
        if not actor:
            st.warning('AUTH-01 · 로그인한 이메일 정보를 받지 못했습니다. 로그아웃한 뒤 다시 로그인해 주세요.')
        if not isinstance(cfg.get('allowed_emails'), (list, tuple)) or not allowed:
            st.warning('AUTH-02 · 승인 이메일 목록을 제대로 읽지 못했습니다. Secrets의 allowed_emails 줄이 [app] 아래, [auth] 위에 있는지 확인해 주세요.')
        elif actor and actor not in allowed:
            st.warning('AUTH-03 · 현재 로그인한 이메일이 앱의 승인 목록과 다릅니다. 아래 버튼으로 로그아웃한 뒤, 등록한 이메일을 선택해 주세요.')
        if st.user.get('email_verified') is not True:
            st.warning('AUTH-04 · 로그인은 되었지만 구글의 이메일 확인정보가 확인되지 않았습니다. 로그아웃 후 다시 로그인해도 같으면 이 진단번호를 알려주세요.')
        st.caption('로그인된 이메일이 본인 계정인지 확인해 주세요. 문의할 때는 AUTH 진단번호만 보내고, Secrets나 비밀번호는 보내지 마세요.')
        st.button('로그아웃', on_click=st.logout)
        st.stop()
    can_edit = actor in {str(x).lower().strip() for x in cfg.get('editors', [])}
    st.sidebar.caption('로그인: ' + actor)
    st.sidebar.button('로그아웃', on_click=st.logout)
    url = str(cfg.get('database_url', ''))
    if not url.startswith(('postgresql://', 'postgres://')) or 'sslmode=' not in url:
        st.error('업무모드에는 TLS가 설정된 PostgreSQL database_url이 필요합니다. 저장 없이 실자료를 받지 않습니다.')
        st.stop()
    from urllib.parse import parse_qs, urlsplit
    if parse_qs(urlsplit(url).query).get('sslmode', [''])[0] not in ('require', 'verify-ca', 'verify-full'):
        st.error('데이터베이스 연결에 TLS를 활성화하세요.')
        st.stop()
else:
    url = 'sqlite:///data/dispatch.db' if local else ''
if mode != 'demo':
    try:
        store = Store(url)
        data = store.load()
    except Exception:
        st.error('저장소 연결에 실패했습니다. 관리자에게 연결정보와 접속권한을 확인하세요. 자료는 반영하지 않았습니다.')
        st.stop()
else:
    data = demo_data()
    st.warning('예시 전용 · 화면의 차량과 수치는 모두 허구입니다. 실제 자료 업로드와 저장은 비활성화되어 있습니다.')
if local:
    st.warning('PC 테스트 모드 · 로그인 없이 로컬 SQLite에 저장됩니다. 이 모드를 인터넷에 공개하지 마세요.')
screen = st.sidebar.radio('대시보드 선택', ['납품번호·차량이동 비교', '기존 스냅샷·변경이력'], key='dashboard_mode')
if screen == '납품번호·차량이동 비교':
    from delivery_ui import render
    render(mode, can_edit, actor, store)
    st.stop()
st.sidebar.header('조회 조건')
dates = sorted({r['date'] for k in ('snapshots', 'events') for r in data[k]})
day = st.sidebar.selectbox('운영일', dates or [datetime.now().date().isoformat()], index=max(0, len(dates) - 1))
cutoff = ''
if st.sidebar.checkbox('최종 조회 기준시각 지정'):
    value = st.sidebar.text_input('기준시각(KST)', value=f'{day} 23:59:59', help='예: 2026-09-16 18:00:00. 운영일 전날 시각도 선택 가능합니다.')
    try:
        cutoff = date_value(value, True)
    except InputError as e:
        st.sidebar.error(str(e))
        st.stop()
rows = compare(data, day, cutoff)
cells = sorted({s['cell'] for s in data['snapshots'] if s['date'] == day})
cell = st.sidebar.selectbox('셀 · 최초 또는 최종 소속', ['전체'] + cells)
query = st.sidebar.text_input('차량번호 검색').strip()
rows = [r for r in rows if (not query or query.lower() in r['vehicle'].lower()) and (cell == '전체' or any((s and s['cell'] == cell for s in (r['first'], r['last']))))]
st.sidebar.caption('차량번호·셀 필터는 모든 비교표와 지표에 적용됩니다.')
if st.sidebar.button('저장자료 새로고침'):
    st.rerun()
st.sidebar.divider()
st.sidebar.caption('예시 모드' if mode == 'demo' else 'PC SQLite 저장' if local else '인증 + PostgreSQL 저장')
tab1, tab2, tab3, tab4 = st.tabs(['배차 비교', '차량 상세', '자료 등록·백업', '집계 기준·배포'])
paired = [r for r in rows if r['paired']]
first_total = sum((r['first']['total'] for r in paired))
last_total = sum((r['last']['total'] for r in paired))
changed = sum((r['changed'] for r in paired))
needs = sum((not r['paired'] or bool(r['quality']) for r in rows))
counts = {c: sum((r['counts'][c] for r in paired)) for c in CATS}
with tab1:
    cols = st.columns(6)
    values = [('비교 가능 차량', f'{len(paired)} 대', None), ('최초 배차', f'{first_total:,} 건' if paired else '—', None), ('최종 배차', f'{last_total:,} 건' if paired else '—', f'{last_total - first_total:+,} 건' if paired else None), ('변경 차량', f'{changed} 대', None), ('확인 필요', f'{needs} 대', None), ('후속자료 없음', f"{sum((r['status'] == '후속자료 없음' for r in rows))} 대", None)]
    for col, (label, v, d) in zip(cols, values):
        col.metric(label, v, d, delta_color='off')
    st.caption(f'KPI는 동일한 비교 가능 차량 {len(paired)}대만 합산합니다. 최신 기록이 최종확정이 아니면 미확정으로 표시합니다.')
    st.subheader('최종 배차가 달라진 이유')
    for c, col in zip(CATS, st.columns(len(CATS))):
        col.metric(c, f'{counts[c]:,} 건')
    st.caption('누적 처리 건수입니다. 취소 후 복원·왕복 이동도 각각 집계하며, 차량별 최초시각이 달라 전체 반출과 반입이 다를 수 있습니다.')
    st.subheader('차량별 최초 ↔ 최종')
    metric = st.selectbox('비교 지표', list(METRICS), format_func=lambda k: METRICS[k])
    if paired:
        chart_rows = sorted(paired, key=lambda r: abs(r['last'][metric] - r['first'][metric]), reverse=True)[:20]
        chart = [{'차량번호': r['vehicle'], '최초': r['first'][metric], '최종': r['last'][metric]} for r in chart_rows]
        st.bar_chart(chart, stack=False, height=320, x='차량번호', y=['최초', '최종'])
    else:
        st.info('최초 완료와 그 이후 현황이 함께 있는 차량이 아직 없습니다.')
    table = []
    for r in rows:
        item = {'차량번호': r['vehicle'], '최초 셀': r['first']['cell'] if r['first'] else '', '최종 셀': r['last']['cell'] if r['paired'] else '', '최초 완료시각': r['first']['at'].replace('T', ' ') if r['first'] else '', '최종 기록시각': r['last']['at'].replace('T', ' ') if r['paired'] else '', '확인 상태': r['status']}
        for k, label in METRICS.items():
            item['최초 ' + label] = r['first'][k] if r['first'] else None
            item['최종 ' + label] = r['last'][k] if r['paired'] else None
            item['증감 ' + label] = round(r['last'][k] - r['first'][k], 6) if r['paired'] else None
        item.update({c: r['counts'][c] if r['paired'] else None for c in CATS})
        item['예상 최종건수'] = r['expected']
        item['미설명 차이'] = r['gap']
        table.append(item)
    st.subheader('차량별 비교표')
    show_only = st.checkbox('변경 또는 확인 필요 차량만 보기')
    visible = [item for item, r in zip(table, rows) if not show_only or r['changed'] or (not r['paired']) or r['quality']]
    if visible:
        st.dataframe(visible, hide_index=True, use_container_width=True, height=420)
        st.download_button('비교표 CSV 내려받기', csv_bytes(visible), file_name=f"{('예시_' if mode == 'demo' else '')}배차비교_{day}.csv", mime='text/csv')
    else:
        st.info('표시할 차량이 없습니다. 자료를 등록하거나 조회 조건을 바꾸세요.')
with tab2:
    if rows:
        vehicle = st.selectbox('상세 차량', [r['vehicle'] for r in rows])
        r = next((r for r in rows if r['vehicle'] == vehicle))
        st.subheader(vehicle + ' · ' + r['status'])
        details = []
        for k, label in METRICS.items():
            details.append({'지표': label, '최초': r['first'][k] if r['first'] else None, '최종': r['last'][k] if r['paired'] else None, '증감': round(r['last'][k] - r['first'][k], 6) if r['paired'] else None})
        st.dataframe(details, hide_index=True, use_container_width=True)
        st.markdown('**저장된 차량별 현황**')
        st.dataframe(raw_rows('snapshots', r['snapshots']), hide_index=True, use_container_width=True)
        st.markdown('**비교 구간에 반영된 변경이력**')
        if r['events']:
            st.dataframe(raw_rows('events', r['events']), hide_index=True, use_container_width=True)
        else:
            st.info('비교 구간에 해당하는 반영 이력이 없습니다.')
        outside = [e for e in data['events'] if e['date'] == day and vehicle in (e['from'], e['to']) and (e not in r['events'])]
        with st.expander('비교 구간 밖·미반영 이력 확인'):
            st.caption('기준시각 이전, 최종기록 이후, 반영여부 N 등의 기록입니다. KPI에 반영하지 않습니다.')
            st.dataframe(raw_rows('events', outside), hide_index=True, use_container_width=True)
    else:
        st.info('자료를 등록하면 차량별 상세 이력을 확인할 수 있습니다.')
with tab3:
    st.subheader('기존 기록은 유지하고 새 시점의 자료를 추가합니다')
    st.info('기존 엑셀의 스냅샷입력·변경이력 시트를 읽습니다. 원본 배차 시스템 자동접속·자동수집은 포함하지 않습니다.')
    if mode == 'demo':
        st.warning('현재는 예시 전용입니다. 로그인과 저장소 설정 후 실자료 등록이 활성화됩니다.')
    elif not can_edit:
        st.info('현재 계정은 조회 전용입니다. 등록은 관리자에게 요청하세요.')
    else:
        st.caption('업로드하면 이 앱을 운영하는 서버로 파일이 전송됩니다. 회사에서 승인한 호스팅 환경에서만 실자료를 사용하세요.')
        csv_kind = st.radio('CSV 파일 종류', ['snapshots', 'events'], format_func=lambda k: '스냅샷입력' if k == 'snapshots' else '변경이력', horizontal=True)
        upload = st.file_uploader('XLSX / CSV / 기존 HTML JSON 백업', type=['xlsx', 'csv', 'json'])
        incoming = None
        if upload:
            try:
                incoming = parse_file(upload.name, upload.getvalue(), csv_kind)
                _, added, skipped = merge_data(data, incoming)
                st.success(f'입력 검증 통과 · 신규 {added:,}행 / 같은 내용의 중복 {skipped:,}행')
                for kind in ('snapshots', 'events'):
                    if incoming.get(kind):
                        with st.expander(('스냅샷' if kind == 'snapshots' else '변경이력') + ' 미리보기'):
                            st.dataframe(raw_rows(kind, incoming[kind][:100]), hide_index=True, use_container_width=True)
            except InputError as exc:
                st.error(str(exc))
                incoming = None
        confirmed = st.checkbox('운영일·차량·시각을 확인했으며, 기존 기록을 덮어쓰지 않고 추가합니다.')
        if st.button('검증된 자료를 저장소에 등록', type='primary', disabled=incoming is None or not confirmed):
            try:
                added, skipped = store.append(incoming, actor, hashlib.sha256(upload.getvalue()).hexdigest())
                st.session_state['saved_notice'] = f'{added:,}행 저장 · 중복 {skipped:,}행 제외'
                st.rerun()
            except InputError as exc:
                st.error(str(exc))
            except Exception:
                st.error('저장에 실패했습니다. 파일 전체를 반영하지 않았습니다. 저장소 접속을 확인하세요.')
        if st.session_state.get('saved_notice'):
            st.success(st.session_state.pop('saved_notice'))
    st.divider()
    st.markdown('**백업·입력 양식**')
    backup = {'format': 'dispatch-compare-v1', 'mode': 'demo' if mode == 'demo' else 'real', **data}
    st.download_button('전체 JSON 백업', json.dumps(backup, ensure_ascii=False, indent=2), file_name=('예시_' if mode == 'demo' else '') + '배차비교_백업.json', mime='application/json')
    for kind, headers, name in [('snapshots', SH, '스냅샷입력'), ('events', EH, '변경이력')]:
        c1, c2 = st.columns(2)
        c1.download_button(name + ' CSV 입력양식', csv_bytes([], headers), file_name=name + '_양식.csv', mime='text/csv')
        c2.download_button(name + ' 저장자료 CSV', csv_bytes(raw_rows(kind, data[kind]), headers), file_name=('예시_' if mode == 'demo' else '') + name + '.csv', mime='text/csv')
    if store and can_edit:
        with st.expander('등록 감사기록 · 최근 1,000행'):
            st.dataframe(store.audit(), hide_index=True, use_container_width=True)
with tab4:
    st.markdown('\n### 비교 기준\n**운영일 × 차량번호**별로 가장 이른 `완료 / 최종확정` 기록을 최초 기준으로 잡습니다.\n그 뒤의 가장 늦은 스냅샷을 최종 현황으로 사용합니다. 최종확정이 아니면 미확정으로 표시합니다.\n최초 기록만 있는 차량은 최종을 0건으로 만들지 않으며, 합산 KPI에서 제외합니다.\n\n**예상 최종 = 최초 − 연기 − 취소 − 반출 + 반입 + 추가 + 복원 − 배차해제**\n\n미설명 차이는 실제 최종과 예상 최종의 차이입니다. 0이라고 해서 이력 누락이 없음을 보증하지 않습니다.\n이력은 `최초시각 < 변경시각 ≤ 최종시각`에만 반영합니다. 최초와 같은 초의 변경은 최초에 포함된 것으로 간주합니다.\n원천에 초 미만의 시각/변경순번이 있다면 수집 단계에서 순서를 확정해야 합니다.\n\n연기는 다른 날로 빠지는 건수입니다. 당일 시간만 늦어지는 것은 건수 차감 대상이 아닙니다.\n차량 이동을 여러 번 하면 반입·반출을 매번 기록합니다. 취소 후 복원도 두 이력을 모두 남깁니다.\n누락된 파일 행만으로 연기·취소를 추정하지 않습니다.\n\n### 데이터 유지·권한\n스냅샷 키는 운영일+차량번호+스냅샷일시, 변경 키는 운영일+변경ID입니다.\n동일 키·동일 내용은 제외하고, 같은 키에 다른 내용이 있으면 업로드 전체를 거부합니다.\n늦게 확보된 더 이른 완료 기록을 추가하면 최초 기준시각이 앞당겨질 수 있습니다. 과거 기록 자체는 바뀌지 않습니다.\n잘못 저장한 기록의 정정·삭제는 이 화면에서 제공하지 않습니다. 백업 후 관리자가 검토해야 합니다.\n\n세션 메모리를 영구저장소로 쓰지 않습니다. 업무모드는 PostgreSQL, PC 테스트는 로컬 SQLite에 저장합니다.\n다른 담당자가 등록한 내역은 ‘저장자료 새로고침’으로 읽습니다. 자동 수집·실시간 푸시·자동 백업 스케줄은 없습니다.\n업무모드에서 승인된 이용자는 동일 배차 데이터셋을 봅니다. 고객사별 분리 등 다중조직 권한은 별도 설계가 필요합니다.\n\n### 사이트 배포\n배포 패키지의 README.md를 참고하세요. 기본 배포는 실자료가 없는 예시 사이트입니다.\n업무자료 사용 전 OIDC 로그인, 이메일 승인 목록, 편집자 목록, PostgreSQL 연결과 회사의 외부 전송 승인을 설정해야 합니다.\n기존 고객·차량자료, 백업 파일, 비밀번호는 GitHub 저장소에 올리지 마세요.\n')
    st.caption('코드의 비교 로직은 자동 테스트 대상입니다. 실제 클라우드 배포, OIDC 계정, 외부 PostgreSQL 접속은 배포 환경에서 별도 검증이 필요합니다.')
