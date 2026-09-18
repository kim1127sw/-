from __future__ import annotations

import os
from urllib.parse import parse_qs, urlsplit

import streamlit as st

from storage import Store

st.set_page_config(
    page_title="가전운송 배차 현황",
    page_icon="🚚",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────
try:
    cfg = dict(st.secrets.get("app", {}))
except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
    cfg = {}

local = os.environ.get("DISPATCH_LOCAL") == "1"
mode = "local" if local else cfg.get("mode", "demo")

if mode not in ("local", "demo", "live"):
    st.error("app.mode 설정은 demo 또는 live여야 합니다.")
    st.stop()

actor = "local-test" if local else "public-viewer"
can_edit = local
store = None

# ─────────────────────────────────────────────────────────────
# 업무모드
# - 일반 방문자: 로그인 없이 조회
# - editors 등록 계정: Google 로그인 후 관리자/등록 권한
# ─────────────────────────────────────────────────────────────
if mode == "live":
    url = str(cfg.get("database_url", "")).strip()

    if not url.startswith(("postgresql://", "postgres://")) or "sslmode=" not in url:
        st.error("업무모드에는 TLS가 설정된 PostgreSQL database_url이 필요합니다.")
        st.stop()

    try:
        sslmode = parse_qs(urlsplit(url).query).get("sslmode", [""])[0]
    except ValueError:
        st.error("database_url 형식을 확인하세요.")
        st.stop()

    if sslmode not in ("require", "verify-ca", "verify-full"):
        st.error("데이터베이스 연결에 TLS를 활성화하세요.")
        st.stop()

    editors = {
        str(x).lower().strip()
        for x in cfg.get("editors", [])
        if str(x).strip()
    }

    # 로그인한 사람만 관리자 여부 확인.
    # 로그인하지 않은 사람은 그대로 공개 조회 사용.
    if st.user.is_logged_in:
        actor = str(st.user.get("email", "")).lower().strip()
        verified = st.user.get("email_verified") is True
        can_edit = bool(actor and verified and actor in editors)

        if can_edit:
            st.sidebar.success("관리자 모드")
            st.sidebar.caption(actor)
        else:
            st.sidebar.info("조회 전용")
            if actor:
                st.sidebar.caption(actor)

        st.sidebar.button("로그아웃", on_click=st.logout)
    else:
        actor = "public-viewer"
        can_edit = False
        st.sidebar.caption("공개 조회 모드")
        if st.sidebar.button("관리자 로그인", type="primary"):
            try:
                st.login()
            except Exception:
                st.sidebar.error("관리자 로그인 설정을 확인하세요.")

    try:
        store = Store(url)
    except Exception:
        st.error("저장소 연결에 실패했습니다. 잠시 후 다시 시도해 주세요.")
        st.stop()

elif mode == "local":
    url = "sqlite:///data/dispatch.db"
    try:
        store = Store(url)
    except Exception:
        st.error("로컬 저장소 연결에 실패했습니다.")
        st.stop()
    st.sidebar.warning("PC 테스트 모드")

else:
    # demo
    store = None
    can_edit = False
    actor = "demo"

# ─────────────────────────────────────────────────────────────
# 현재 사용하는 납품번호/차량 비교 대시보드만 표시
# 공개 방문자는 delivery_ui에서 자동으로:
#   - 차량별 현황을 첫 탭으로 표시
#   - 자료등록 탭 숨김
# 관리자만:
#   - 자료등록 탭 표시
# ─────────────────────────────────────────────────────────────
from delivery_ui import render

render(mode, can_edit, actor, store)
