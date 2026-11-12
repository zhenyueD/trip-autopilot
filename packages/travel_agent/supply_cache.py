"""Verified supply atoms, persisted only as a side effect of a successful save.

No research, semantic verification or document rendering happens here. Callers
supply explicit applicability and verification; quote matching is an integrity
check, not an assertion of source authority or independent corroboration.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit


TTL_DAYS = {"price": 1, "hours": 1, "closure": 1, "last_service": 1,
            "address": 30, "distance": 30, "other": 1}
VERIFIED_STATUSES = {"verified", "单源未交叉确认", "多源一致"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected nonempty text")
    return value


def _time(value):
    parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps require an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _now():
    return datetime.now(timezone.utc)


def _identity(destination, atom_id, conditions):
    _text(destination)
    _text(atom_id)
    if not isinstance(conditions, dict):
        raise ValueError("explicit applicability conditions must be an object")
    return hashlib.sha256(_json([destination, atom_id, conditions]).encode()).hexdigest()


def _prepare(destination, atom, clock):
    """Validate a live, verified atom and discard full pages and private fields."""
    atom = deepcopy(atom)
    key = _identity(destination, atom["atom_id"], atom["conditions"])
    for field in ("claim", "kind", "verification_status", "verified_at"):
        _text(atom[field])
    if atom["verification_status"] not in VERIFIED_STATUSES:
        raise ValueError("only explicitly verified atoms may enter the cache")
    verified = _time(atom["verified_at"])
    if verified > clock:
        raise ValueError("verification time is in the future")
    if not isinstance(atom.get("sources"), list) or not atom["sources"]:
        raise ValueError("verified atoms require verbatim source evidence")
    sources = [_source(source, verified) for source in atom["sources"]]
    result = {field: atom[field] for field in
              ("atom_id", "claim", "kind", "conditions", "verification_status")}
    result.update(cache_key=key, destination=destination, sources=sources,
                  verified_at=verified.isoformat(), schema_version=1)
    # Preserve expiry supplied by the verifier (e.g. the end of a ticket season).
    if atom.get("valid_until") is not None:
        result["valid_until"] = _time(atom["valid_until"]).isoformat()
    return result


def _source(source, verified):
    url, quote, content = (_text(source[k]) for k in ("url", "quote", "content"))
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("source must be a public HTTP(S) URL without credentials")
    retrieved = _time(source["retrieved_at"])
    if retrieved > verified:
        raise ValueError("source retrieval cannot follow verification")
    if quote not in content:
        raise ValueError("quote must occur verbatim in source content")
    digest = hashlib.sha256(content.encode()).hexdigest()
    if source.get("content_hash", digest) != digest:
        raise ValueError("source content hash mismatch")
    return dict(url=url, quote=quote, retrieved_at=retrieved.isoformat(), content_hash=digest)


def _view(atom, clock):
    days = TTL_DAYS.get(atom["kind"], 1)
    times = [_time(atom["verified_at"])] + [_time(s["retrieved_at"]) for s in atom["sources"]]
    expires = min(times) + timedelta(days=days)
    if atom.get("valid_until"):
        expires = min(expires, _time(atom["valid_until"]))
    fresh = max(times) <= clock < expires
    return dict(atom=deepcopy(atom), freshness="fresh" if fresh else "expired",
                usable=fresh, requires_reverification=not fresh,
                checked_at=clock.isoformat(), expires_at=expires.isoformat(), ttl_days=days)


class SupplyCache:
    """SQLite transactions serialize competing writers; the latest check wins."""

    def __init__(self, path, *, clock=_now):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS atoms "
                       "(cache_key TEXT PRIMARY KEY, destination TEXT NOT NULL, "
                       "verified_at TEXT NOT NULL, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS events "
                       "(id INTEGER PRIMARY KEY, body TEXT NOT NULL)")

    def _db(self):
        # Connections are short-lived; context manager below closes explicitly.
        from contextlib import contextmanager

        @contextmanager
        def connection():
            db = sqlite3.connect(self.path, timeout=10)
            try:
                with db:
                    yield db
            finally:
                db.close()
        return connection()

    @staticmethod
    def _event(db, event):
        db.execute("INSERT INTO events(body) VALUES (?)", (_json(event),))

    def save_handbook(self, destination, atoms, save):
        """Invoke an existing save callback; only its successful return writes.

        Adapters must pass a callback that raises on rejection. This module does
        not install itself into an agent or change a document/API contract.
        """
        prepared = [self._for_save(destination, atom) for atom in atoms]
        result = save()
        if isinstance(result, dict) and (result.get("error") or result.get("not_completed")):
            return result
        self._store(prepared)
        return result

    def _for_save(self, destination, atom):
        # A cache hit carries only short quotes, never a whole copied page.
        # Accept that form only if it exactly matches the durable cache record.
        key = _identity(destination, atom["atom_id"], atom["conditions"])
        with self._db() as db:
            row = db.execute("SELECT body FROM atoms WHERE cache_key=?", (key,)).fetchone()
        if row and json.loads(row[0]) == atom:
            prepared = deepcopy(atom)
        else:
            prepared = _prepare(destination, atom, self.clock())
        if not _view(prepared, self.clock())["usable"]:
            raise ValueError("expired atom cannot be used to save a handbook")
        return prepared

    def _store(self, atoms):
        with self._db() as db:
            for atom in atoms:
                cursor = db.execute(
                    "INSERT INTO atoms VALUES (?, ?, ?, ?) ON CONFLICT(cache_key) DO UPDATE "
                    "SET verified_at=excluded.verified_at, body=excluded.body "
                    "WHERE excluded.verified_at > atoms.verified_at",
                    (atom["cache_key"], atom["destination"], atom["verified_at"], _json(atom)))
                self._event(db, dict(action="write" if cursor.rowcount else "older_or_equal_ignored",
                                     cache_key=atom["cache_key"], destination=atom["destination"],
                                     verified_at=atom["verified_at"], at=self.clock().isoformat()))

    def read_destination(self, destination):
        """Inspection only: expired rows remain visible, explicitly unusable."""
        _text(destination)
        with self._db() as db:
            rows = db.execute("SELECT body FROM atoms WHERE destination=? ORDER BY cache_key",
                              (destination,)).fetchall()
        clock = self.clock()
        return [_view(json.loads(row[0]), clock) for row in rows]

    def lookup(self, destination, atom_id, conditions):
        key = _identity(destination, atom_id, conditions)
        with self._db() as db:
            row = db.execute("SELECT body FROM atoms WHERE cache_key=?", (key,)).fetchone()
            view = _view(json.loads(row[0]), self.clock()) if row else None
            event = dict(action="lookup", destination=destination, cache_key=key,
                         atom_id=atom_id, conditions=conditions, at=self.clock().isoformat(),
                         hit=row is not None, freshness=view["freshness"] if view else "missing",
                         usable=view["usable"] if view else False,
                         verified_at=view["atom"]["verified_at"] if view else None,
                         expires_at=view["expires_at"] if view else None)
            self._event(db, event)
        return view

    def resolve(self, destination, atom_id, conditions, live):
        """Return only fresh verified atoms; miss/expiry invokes the live tool.

        live receives a query, never stale evidence. Its result is revalidated,
        and is not cached until a later successful handbook save.
        """
        view = self.lookup(destination, atom_id, conditions)
        if view and view["usable"]:
            return deepcopy(view["atom"])
        query = dict(destination=destination, atom_id=atom_id, conditions=deepcopy(conditions))
        with self._db() as db:
            self._event(db, dict(action="live_fallback", **query, at=self.clock().isoformat()))
        atom = live(query)
        if atom is None:
            return None
        prepared = _prepare(destination, atom, self.clock())
        if prepared["cache_key"] != _identity(destination, atom_id, conditions):
            raise ValueError("live result does not match requested atom/applicability")
        if not _view(prepared, self.clock())["usable"]:
            raise ValueError("live result is expired and needs reverification")
        # Keep the live source text only in the transient return so the save
        # boundary can independently check quotes. _store strips it again.
        for clean, source in zip(prepared["sources"], atom["sources"]):
            clean["content"] = source["content"]
        return prepared

    def ledger(self):
        with self._db() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM events ORDER BY id")]
