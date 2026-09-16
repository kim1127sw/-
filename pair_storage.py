"""Append-only, projected delivery datasets; uses the existing authenticated Store connection."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from delivery_compare import CompareError

SCHEMA = '''CREATE TABLE IF NOT EXISTS delivery_datasets (
 comparison_id TEXT NOT NULL, kind TEXT NOT NULL, content_hash TEXT NOT NULL,
 payload TEXT NOT NULL, imported_at TEXT NOT NULL, imported_by TEXT NOT NULL,
 source_name TEXT NOT NULL, PRIMARY KEY(comparison_id,kind,content_hash))'''

def canonical(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

class PairStore:
    def __init__(self, store):
        self.store = store
        self.p = '%s' if store.pg else '?'
        con = store.connection()
        try:
            con.execute(SCHEMA)
            con.commit()
        finally:
            con.close()

    def list_ids(self):
        con = self.store.connection()
        try:
            return [r[0] for r in con.execute('SELECT DISTINCT comparison_id FROM delivery_datasets ORDER BY comparison_id DESC').fetchall()]
        finally:
            con.close()

    def load(self, comparison_id, final_hash=None):
        con = self.store.connection()
        try:
            rows = con.execute(f'SELECT kind,content_hash,payload,imported_at,imported_by,source_name FROM delivery_datasets WHERE comparison_id={self.p} ORDER BY imported_at,content_hash', (comparison_id,)).fetchall()
        finally:
            con.close()
        result = {'versions': [], 'comparison_id': comparison_id}
        for kind, digest, payload, at, actor, name in rows:
            if kind == 'initial':
                result['initial'] = json.loads(payload)
                result['initial_name'] = name
            else:
                result['versions'].append({'hash': digest, '등록시각(UTC)': at, '등록자': actor, '파일명': name})
                if final_hash is None or final_hash == digest:
                    result['final'] = json.loads(payload)
                    result['final_name'] = name
        return result

    def save(self, comparison_id, initial, final, actor, initial_name, final_name):
        # The comparison date is a grouping label, never a claimed completion time.
        try:
            datetime.strptime(comparison_id, '%Y-%m-%d')
        except ValueError as exc:
            raise CompareError('비교일은 YYYY-MM-DD 형식이어야 합니다.') from exc
        if initial.get('kind') != 'initial' or final.get('kind') != 'final':
            raise CompareError('최초·최종 파일 구분 오류')
        con = self.store.connection()
        inserted = 0
        try:
            if self.store.pg:
                con.execute('SELECT pg_advisory_xact_lock(82573419)')
            else:
                con.execute('BEGIN IMMEDIATE')
            for kind, doc, name in [('initial', initial, initial_name), ('final', final, final_name)]:
                payload = canonical(doc)
                digest = hashlib.sha256(payload.encode('utf8')).hexdigest()
                existing = con.execute(f'SELECT content_hash FROM delivery_datasets WHERE comparison_id={self.p} AND kind={self.p}', (comparison_id, kind)).fetchall()
                if kind == 'initial' and existing and digest != existing[0][0]:
                    raise CompareError('이 비교일의 최초 상세정보는 이미 고정되었습니다. 다른 최초 파일로 덮어쓸 수 없습니다. 비교일과 원본을 확인하세요.')
                if any(r[0] == digest for r in existing):
                    continue
                con.execute(f'INSERT INTO delivery_datasets VALUES ({",".join([self.p]*7)})', (comparison_id, kind, digest, payload, datetime.now(timezone.utc).isoformat(), actor, name))
                inserted += 1
            con.commit()
            return inserted
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
