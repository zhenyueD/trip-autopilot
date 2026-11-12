"""Best-effort supply side effects; no inference of destination or applicability."""
import hashlib
import json

from .comparison_card_budget import now
from .supply_cache import SupplyCache


class SessionSupply:
    def __init__(self, agent, path=None):
        self.agent = agent
        self.path = path or agent.directory.parent / 'supply-cache.sqlite3'
        self.recheck_urls = set()

    def scope(self):
        intake = self.agent.session.get('intake', {})
        destination = intake.get('destination')
        if not isinstance(destination, str) or not destination.strip():
            return None
        # Exact explicit context is conservative and keeps personal text out of
        # the shared cache. Revised requests never silently reuse initial scope.
        context = {k: v for k, v in intake.items() if k != 'case_id'}
        requests = [m.get('content') for m in self.agent.session['messages']
                    if m.get('role') == 'user']
        if requests:
            try:
                if json.loads(requests[0]) == intake:
                    requests = requests[1:]
            except (TypeError, ValueError):
                pass
        context['requests'] = requests
        digest = hashlib.sha256(json.dumps(context, sort_keys=True,
                               ensure_ascii=False).encode()).hexdigest()
        return destination, {'explicit_context_sha256': digest}

    def event(self, **values):
        with self.agent._state_lock:
            self.agent.session.setdefault('supply_cache_events', []).append(dict(at=now(), **values))
            self.agent.persist()

    def read(self, url):
        self.recheck_urls.discard(url)
        scope = self.scope()
        if scope is None:
            return None
        try:
            cache = SupplyCache(self.path)
            candidates = [v for v in cache.read_destination(scope[0])
                          if v['atom']['conditions'] == scope[1]
                          and any(s['url'] == url for s in v['atom']['sources'])]
            atoms = []
            for candidate in candidates:
                view = cache.lookup(scope[0], candidate['atom']['atom_id'], scope[1])
                self.event(action='lookup', url=url, hit=True, freshness=view['freshness'],
                           verified_at=view['atom']['verified_at'], usable=view['usable'])
                if view['usable']:
                    atoms.append(view['atom'])
            if not atoms:
                if candidates:
                    self.recheck_urls.add(url)
                self.event(action='live_fallback', url=url, hit=bool(candidates), usable=False)
                return None
            with self.agent._document_lock:
                sources = self.agent.documents.sources
                number = max((int(s[1:]) for s in sources if s.startswith('S') and s[1:].isdigit()), default=0) + 1
                sid = 'S' + str(number)
                quotes = [s for a in atoms for s in a['sources'] if s['url'] == url]
                content = '\n\n'.join(s['quote'] for s in quotes)
                source = self.agent.documents.register_source(sid, url, content,
                                                               min(s['retrieved_at'] for s in quotes))
                self.agent.session.setdefault('supply_sources', {})[sid] = atoms
            self.event(action='reuse', url=url, hit=True, usable=True)
            return dict(status='read', **source, cached=True, supply_cache=True, first_read_at=source['retrieved_at'],
                        source_authority='Cached verified excerpts only, not a full page; model must assess authority')
        except Exception as error:
            self.recheck_urls.add(url)
            self.event(action='read_error', error=type(error).__name__, url=url)
            return None

    def preserve_verification(self, record):
        scope = self.scope()
        if scope is not None:
            self.agent.session.setdefault('supply_evidence_scopes', {})[record['evidence_id']] = list(scope)
        atoms = self.agent.session.get('supply_sources', {}).get(record['source_id'], [])
        # Any evidence derived from cached excerpts inherits the original
        # oldest check, including shortened quotes; never renew freshness.
        matches = atoms
        if matches:
            original = min(matches, key=lambda a: a['verified_at'])
            ledger = self.agent.documents._read_json('ledger.json')
            record.update(verified_at=original['verified_at'],
                          verification_status=original['verification_status'])
            ledger['evidence'][record['evidence_id']] = record
            self.agent.documents._write_json('ledger.json', ledger)
        return record

    def saved(self):
        scope = self.scope()
        if scope is None:
            return
        try:
            cache = SupplyCache(self.path)
            for record in self.agent.documents.evidence.values():
                try:
                    bound = self.agent.session.get('supply_evidence_scopes', {}).get(record['evidence_id'])
                    if bound != list(scope):
                        self.event(action='scope_skipped', evidence_id=record['evidence_id'])
                        continue
                    prior = self.agent.session.get('supply_sources', {}).get(record['source_id'])
                    if prior:
                        atoms = [a for a in prior if a['destination'] == scope[0]
                                 and a['conditions'] == scope[1]]
                    else:
                        source = self.agent.documents.sources[record['source_id']]
                        atom_id = hashlib.sha256(json.dumps([record['url'], record['quote'],
                            record['kind'], record['claim']], ensure_ascii=False).encode()).hexdigest()
                        atoms = [dict(atom_id=atom_id, conditions=scope[1],
                            **{k: record[k] for k in ('claim', 'kind', 'verified_at', 'verification_status')},
                            sources=[dict(url=record['url'], quote=record['quote'],
                                content=source['content'], retrieved_at=source['retrieved_at'],
                                content_hash=source['content_hash'])])]
                    # The document has already committed. Never repeat its save
                    # callback if the optional cache write fails.
                    cache.save_handbook(scope[0], atoms, lambda: None)
                    self.event(action='saved', evidence_id=record['evidence_id'])
                except Exception as error:
                    self.event(action='write_error', error=type(error).__name__, evidence_id=record['evidence_id'])
        except Exception as error:
            self.event(action='write_error', error=type(error).__name__)
