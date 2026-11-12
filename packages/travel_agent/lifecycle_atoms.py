"""Offline, destination-scoped supply evidence ledger. No research is performed.

Replay identity is (destination, claim, source_url) across all history. Renewal
requires an explicit supersedes link; ordinary harvest cannot revive old rows.
SQLite transactions make a batch and each replacement indivisible.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from uuid import uuid4

CATEGORIES = frozenset(('开放', '票价', '交通', '住宿', '日照', '联系方式', '其他'))
FIELDS = frozenset(('atom_id', 'destination', 'claim', 'category', 's_level',
                    'source_name', 'source_url', 'verbatim_quote', 'checked_date',
                    'season_scope', 'expires_on', 'status', 'supersedes',
                    'evidence_kind', 'notes', 'metadata_unknown'))
SOURCE_PRIORITY = {
    '开放': '经营者当期官方开放公告；再找同日期条件的独立原文对点',
    '票价': '经营者当期官方票价及包含项目；再找同日期条件的独立原文对点',
    '交通': '运营方或交通主管机关公告；路线距离耗时用匹配条件的 Maps/Routes',
    '住宿': '住宿经营者当期官方条款；再找同日期条件的独立原文对点',
    '日照': '匹配地点与日期的天文数据；再找独立计算或原始数据对点',
    '联系方式': '该机构官方联系页面；再找独立原文对点',
    '其他': '该声明的主管机关或经营者当期原文；再找独立原文对点',
}


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} must be a nonempty string')
    return value


def _date(value: object, field: str) -> str:
    value = _text(value, field)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{field} must be an ISO YYYY-MM-DD date') from exc
    if parsed.isoformat() != value:
        raise ValueError(f'{field} must be an ISO YYYY-MM-DD date')
    return value


def _record(destination: str, record: Mapping) -> dict:
    if not isinstance(record, Mapping):
        raise ValueError('each record must be a mapping')
    if set(record) - FIELDS:
        raise ValueError(f'unknown fields: {set(record) - FIELDS}')
    atom = dict(record)
    if atom.get('destination', destination) != destination:
        raise ValueError('record destination differs from partition')
    atom['destination'] = destination
    atom.setdefault('atom_id', str(uuid4()))
    for field in ('atom_id', 'claim', 'source_name', 'source_url', 'season_scope'):
        atom[field] = _text(atom.get(field), field)
    for field, choices, default in (
        ('category', CATEGORIES, None), ('s_level', ('S1', 'S2', 'S3'), None),
        ('status', ('active',), 'active'),
        ('evidence_kind', ('recorded', 'fixture'), 'recorded'),
    ):
        value = atom.get(field, default)
        if not isinstance(value, str) or value not in choices:
            raise ValueError(f'invalid {field}: {value!r}')
        atom[field] = value
    atom['expires_on'] = _date(atom.get('expires_on'), 'expires_on')
    atom['supersedes'] = atom.get('supersedes')
    if atom['supersedes'] is not None:
        _text(atom['supersedes'], 'supersedes')
    return _evidence(atom)


def _evidence(atom: dict) -> dict:
    notes = atom.get('notes', [])
    if not isinstance(notes, list) or any(not isinstance(x, str) for x in notes):
        raise ValueError('notes must be a list of strings')
    atom['notes'] = list(notes)
    supplied_unknown = atom.get('metadata_unknown', [])
    if (not isinstance(supplied_unknown, list)
            or any(x not in ('verbatim_quote', 'checked_date') for x in supplied_unknown)):
        raise ValueError('metadata_unknown must list missing quote/date fields')
    atom['metadata_unknown'] = []
    for field in ('verbatim_quote', 'checked_date'):
        value = atom.get(field)
        if value is None:
            atom['metadata_unknown'].append(field)
            note = f'{field}: unknown in supplied record'
            if note not in atom['notes']:
                atom['notes'].append(note)
        elif field == 'checked_date':
            _date(value, field)
        else:
            _text(value, field)
        atom[field] = value
    if atom['checked_date'] and atom['expires_on'] < atom['checked_date']:
        raise ValueError('expires_on precedes checked_date')
    return atom


class AtomStore:
    """SQLite ledger. Use close() or a context manager to release the connection."""

    def __init__(self, path: str | Path):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), timeout=30, isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS lifecycle_atoms (
                atom_id TEXT PRIMARY KEY,
                destination TEXT NOT NULL,
                claim TEXT NOT NULL,
                source_url TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'superseded')),
                supersedes TEXT UNIQUE REFERENCES lifecycle_atoms(atom_id),
                payload TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS lifecycle_active_identity
            ON lifecycle_atoms(destination, claim, source_url) WHERE status='active';
            CREATE INDEX IF NOT EXISTS lifecycle_destination
            ON lifecycle_atoms(destination);
        ''')
        self._db.execute('PRAGMA foreign_keys=ON')

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> AtomStore:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        atom = json.loads(row['payload'])
        atom['status'] = row['status']
        return atom

    def get(self, destination: str, atom_id: str) -> dict:
        _text(destination, 'destination')
        _text(atom_id, 'atom_id')
        with self._lock:
            row = self._db.execute(
                'SELECT * FROM lifecycle_atoms WHERE destination=? AND atom_id=?',
                (destination, atom_id)).fetchone()
            if row is None:
                raise KeyError(atom_id)
            return self._decode(row)

    def list_atoms(self, destination: str, status: str | None = None) -> list[dict]:
        _text(destination, 'destination')
        if status is not None and status not in ('active', 'superseded'):
            raise ValueError('invalid status')
        sql = 'SELECT * FROM lifecycle_atoms WHERE destination=?'
        args = [destination]
        if status is not None:
            sql += ' AND status=?'
            args.append(status)
        with self._lock:
            rows = self._db.execute(sql + ' ORDER BY rowid', args).fetchall()
            return [self._decode(row) for row in rows]

    def harvest(self, destination: str, records: Iterable[Mapping]) -> list[dict]:
        _text(destination, 'destination')
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                result = [self._insert(_record(destination, item)) for item in records]
                self._db.execute('COMMIT')
                return result
            except BaseException:
                self._db.execute('ROLLBACK')
                raise

    def _insert(self, atom: dict) -> dict:
        predecessor = atom['supersedes']
        if predecessor is not None:
            old = self.get(atom['destination'], predecessor)
            if old['status'] != 'active':
                return self._replacement_replay(atom)
        else:
            existing = self._db.execute('''
                SELECT * FROM lifecycle_atoms
                WHERE destination=? AND claim=? AND source_url=?
                ORDER BY rowid DESC LIMIT 1
            ''', (atom['destination'], atom['claim'], atom['source_url'])).fetchone()
            if existing:
                return self._decode(existing)
        if atom['atom_id'] == predecessor:
            raise ValueError('atom cannot supersede itself')
        if predecessor is not None:
            self._db.execute("UPDATE lifecycle_atoms SET status='superseded' WHERE atom_id=?",
                             (predecessor,))
        try:
            self._db.execute('INSERT INTO lifecycle_atoms VALUES (?, ?, ?, ?, ?, ?, ?)',
                             (atom['atom_id'], atom['destination'], atom['claim'],
                              atom['source_url'], atom['status'], predecessor,
                              json.dumps(atom, ensure_ascii=False)))
        except sqlite3.IntegrityError as exc:
            raise ValueError('atom id, active identity, or supersession conflict') from exc
        return atom

    def _replacement_replay(self, atom: dict) -> dict:
        row = self._db.execute('SELECT * FROM lifecycle_atoms WHERE supersedes=?',
                               (atom['supersedes'],)).fetchone()
        if row:
            child = self._decode(row)
            ignored = {'atom_id', 'status'}
            if all(child[k] == atom[k] for k in FIELDS - ignored):
                return child
        raise ValueError('superseded atom cannot be replaced again')

    def recheck(self, destination: str, departure_date: str) -> list[dict]:
        departure_date = _date(departure_date, 'departure_date')
        result = []
        for atom in self.list_atoms(destination, 'active'):
            if atom['s_level'] == 'S1' and atom['expires_on'] < departure_date:
                result.append({**atom,
                    'recheck_question': (
                        f'核查“{atom["claim"]}”是否适用于 {departure_date}；'
                        f'确认实体、日期和条件（原适用范围：{atom["season_scope"]}），'
                        '保留不确定性并记录原文与来源。'),
                    'source_priority': SOURCE_PRIORITY[atom['category']],
                })
        return result

    def writeback(self, destination: str, atom_id: str, *, claim: str,
                  verbatim_quote: str | None, checked_date: str | None,
                  expires_on: str, evidence_kind: str, notes: list[str] | None = None) -> dict:
        # Read and replace under one write transaction, including cross-process writers.
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                old = self.get(destination, atom_id)
                new = {**old, 'atom_id': str(uuid4()), 'claim': claim,
                       'verbatim_quote': verbatim_quote, 'checked_date': checked_date,
                       'expires_on': expires_on, 'evidence_kind': evidence_kind,
                       'notes': [] if notes is None else notes, 'status': 'active',
                       'supersedes': atom_id, 'metadata_unknown': []}
                result = self._insert(_record(destination, new))
                self._db.execute('COMMIT')
                return result
            except BaseException:
                self._db.execute('ROLLBACK')
                raise


def harvest(store: AtomStore, destination: str, records: Iterable[Mapping]) -> list[dict]:
    return store.harvest(destination, records)


def recheck(store: AtomStore, destination: str, departure_date: str) -> list[dict]:
    return store.recheck(destination, departure_date)


def writeback(store: AtomStore, destination: str, atom_id: str, **updates) -> dict:
    return store.writeback(destination, atom_id, **updates)
