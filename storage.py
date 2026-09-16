"""Append-only storage: SQLite for local test; PostgreSQL for authenticated cloud use."""
from __future__ import annotations
import hashlib
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from core import canonical, record_key, InputError, normalize

SCHEMA='''CREATE TABLE IF NOT EXISTS dispatch_records (
 kind TEXT NOT NULL, record_id TEXT NOT NULL, payload TEXT NOT NULL,
 imported_at TEXT NOT NULL, imported_by TEXT NOT NULL, file_hash TEXT NOT NULL,
 PRIMARY KEY(kind,record_id))'''

class Store:
    def __init__(self, url: str):
        self.url=url
        self.pg=not url.startswith('sqlite:///')
        if not self.pg and url=='sqlite:///:memory:':
            raise ValueError('파일 기반 SQLite 경로를 사용하세요.')
        con=self.connection()
        try:
            con.execute(SCHEMA)
            con.commit()
        finally:
            con.close()
    def connection(self):
        if self.pg:
            import psycopg
            return psycopg.connect(self.url,connect_timeout=10)
        path=self.url[len('sqlite:///'):]
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        con=sqlite3.connect(path,timeout=15)
        con.execute('PRAGMA journal_mode=WAL')
        return con
    def load(self):
        data={'snapshots':[],'events':[]}
        con=self.connection()
        try:
            for kind,payload in con.execute('SELECT kind,payload FROM dispatch_records ORDER BY imported_at,record_id').fetchall():
                data[kind].append(json.loads(payload))
        finally:
            con.close()
        return data
    def append(self, incoming:dict, actor:str, file_hash:str):
        """A transaction spans both sheets. Never UPDATE an existing record."""
        con=self.connection()
        p='%s' if self.pg else '?'
        added=skipped=0
        try:
            if self.pg:
                # One transaction-scoped lock serializes batch imports to avoid races.
                con.execute('SELECT pg_advisory_xact_lock(82573418)')
            else:
                con.execute('BEGIN IMMEDIATE')
            for kind in ('snapshots','events'):
                for item in incoming.get(kind,[]):
                    row=normalize(item,kind)
                    key=record_key(kind,row)
                    payload=canonical(row)
                    existing=con.execute(f'SELECT payload FROM dispatch_records WHERE kind={p} AND record_id={p}',(kind,key)).fetchone()
                    if existing:
                        if existing[0]!=payload:
                            raise InputError(f'기존 기록과 충돌합니다. 파일 전체를 반영하지 않았습니다: {key}')
                        skipped+=1
                    else:
                        con.execute(f'INSERT INTO dispatch_records VALUES ({",".join([p]*6)})',
                                    (kind,key,payload,datetime.now(timezone.utc).isoformat(),actor,file_hash))
                        added+=1
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return added,skipped
    def audit(self):
        con=self.connection()
        try:
            rows=con.execute('SELECT kind,record_id,imported_at,imported_by,file_hash FROM dispatch_records ORDER BY imported_at DESC LIMIT 1000').fetchall()
            return [dict(zip(['구분','기록키','등록시각(UTC)','등록계정','파일SHA256'],r)) for r in rows]
        finally:
            con.close()
