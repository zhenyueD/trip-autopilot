"""Read-only, model-selected supply tools over the existing research services.

Tool completion is evidence of an operation, never fulfillment of a trip. The
caller owns snapshots; this adapter does not mutate them or contact suppliers.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import inspect

from packages.graph.edges import haversine_km
from .geography import located, spatial_context
from .material_reader import read_public_link
from .research import MAX_QUERIES, READ_WARNING
from .public_search import search_public_evidence
from .research_focus import filter_candidates


MAX_GEOGRAPHY_SUPPLIES = 20
MAX_EXCERPT = 6000
TITLES = {"search_supply": "查找可用的旅行资源线索",
          "search_public": "研究公开交通价格、渠道、上车点与班次",
          "inspect_source": "读取资源公开说明",
          "inspect_geography": "核对候选地点的相对位置"}
BOUNDARY = "公开资料不是实时报价、库存、可订状态或预订确认；不代表已联系或采购。"


def _identifier(prefix, value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    return prefix + "-" + hashlib.sha256(encoded).hexdigest()[:20]


def _merge(previous, incoming):
    """Keep prior selected records and ordering; never silently evict for a cap."""
    merged = {item["id"]: deepcopy(item) for item in previous}
    for item in incoming:
        merged.setdefault(item["id"], deepcopy(item))
    return list(merged.values())


def _evidence_ids(records):
    result = []
    for source in records:
        item = deepcopy(source)
        item.setdefault("id", _identifier("evidence", item))
        result.append(item)
    return result


def capability_records(supplies, evidence=None):
    """Public capability boundaries, not a promotion of a source to a purchase."""
    evidence = _evidence_ids(evidence or [])
    capabilities = []
    for item in supplies:
        links = [entry["id"] for entry in evidence if item["id"] in entry.get("supply_ids", [])]
        capabilities.append({
            "id": item["id"], "supply_id": item["id"], "kind": item.get("kind", "place"),
            "what": item.get("title") or item.get("name") or item["id"],
            "description": item.get("description", ""), "provider": item.get("provider"),
            "constraints": deepcopy(item.get("constraints", [])),
            "location": {key: item.get(key) for key in ("city", "lat", "lng", "geo_status")},
            "acquisition": {"mode": "public_source_only", "status": "needs_provider_confirmation",
                            "source_url": item.get("source_url"), "product_url": item.get("product_url"),
                            "can_contact": False, "can_book": False, "can_pay": False},
            "price": {"status": "unknown"}, "inventory": {"status": "unknown"},
            "availability": {"status": "unknown"}, "booking_status": "not_booked",
            "evidence": deepcopy(item.get("evidence", [])), "evidence_ids": links,
            "boundary": BOUNDARY,
        })
    return capabilities


def _sources(supplies):
    """Only discovery provenance grants access; page text/redirects grant none."""
    sources = {}
    for item in supplies:
        urls = [item.get("product_url"), item.get("source_url")]
        for entry in item.get("evidence", []):
            urls += [entry.get("source_url")]
            urls += entry.get("source_urls", [])
        for url in urls:
            if isinstance(url, str) and url:
                sources.setdefault(url, [])
                if item["id"] not in sources[url]:
                    sources[url].append(item["id"])
    return sources


def _read_receipt(raw, url, ids):
    """Keep bounded source data, while refusing any claimed fact verification."""
    fields = ("status", "reason", "error_type", "fetched_at", "title", "text", "url",
              "message", "body_sha256", "truncated", "bytes_read", "http_status",
              "requested_url", "redirect_chain", "provider", "cache_hit")
    receipt = {key: deepcopy(raw[key]) for key in fields if key in raw}
    receipt["status"] = raw.get("status", "unavailable")
    body = raw.get("text", "")
    body = body if isinstance(body, str) else ""
    if raw.get('provider') and receipt['status'] == 'read' and not body.strip():
        receipt.update(status='observed', reason='provider_response_without_page_text')
    if receipt["status"] not in {"read", "blocked", "unavailable", "observed"}:
        receipt.update(status="unavailable", reason="unsupported_reader_status")
    if receipt["status"] == "read" and not body.strip():
        receipt.update(status="unavailable", reason="empty_source_text")
    receipt["text"] = body[:MAX_EXCERPT] if receipt["status"] == "read" else ""
    receipt.update(source_url=url, supply_ids=list(ids), method=raw.get('method', 'public_source_read'),
                   verified_fields=[], untrusted_external_content=True, warning=READ_WARNING,
                   fetched_at=raw.get("fetched_at") or datetime.now(timezone.utc).isoformat())
    receipt["truncated"] = bool(receipt.get("truncated") or len(body) > MAX_EXCERPT)
    receipt["id"] = _identifier("source", receipt)
    return receipt


def _action_tool(action):
    if not isinstance(action, dict):
        return "invalid_action"
    value = action.get("tool", action.get("name"))
    return value if isinstance(value, str) and value in TITLES else "invalid_action"


class CapabilityTools:
    """Stateless adapter; researcher.reader defaults to the SSRF-protected reader.

    execute returns full merged ``supplies`` and ``evidence`` snapshots. ``task``
    can be persisted in journey.tasks; ``result`` is the next model-turn result.
    Caller-provided supplies/evidence must be trusted server discovery state,
    never candidate objects returned by a model.
    """

    def __init__(self, researcher, catalog=None, *, destinations=None, place_hints=None):
        self.researcher = researcher
        self.catalog = catalog if catalog is not None else getattr(researcher, "catalog", None)
        self.reader = getattr(researcher, "reader", None) or read_public_link
        self.destinations = deepcopy(destinations or [])
        self.place_hints = deepcopy(place_hints or [])

    def execute(self, action, supplies=None, evidence=None):
        supplies, evidence = deepcopy(supplies or []), _evidence_ids(evidence or [])
        tool = _action_tool(action)
        try:
            if tool == "search_supply":
                outcome = self._search(action, supplies)
            elif tool == "search_public":
                outcome = self._public_search(action)
            elif tool == "inspect_source":
                outcome = self._inspect(action, supplies, evidence)
            elif tool == "inspect_geography":
                outcome = self._geography(action, supplies)
            else:
                outcome = self._blocked("unsupported_action", "该动作不在只读能力范围内。")
        except Exception as error:
            outcome = self._blocked("tool_exception", "工具调用失败；未完成本项，也未联系或预订供应商。")
            outcome["result"]["error_type"] = type(error).__name__
        outcome['request_signature'] = self.request_signature(action, supplies)
        return self._finish(tool, action, supplies, evidence, outcome)

    def request_signature(self, action, supplies):
        """Only effective tool inputs distinguish requests, not model labels."""
        action = action if isinstance(action, dict) else {}
        tool = action.get('tool')
        args = {'tool': tool}
        if tool == 'search_supply':
            queries = action.get('queries')
            if isinstance(queries, list) and all(isinstance(q, str) for q in queries):
                queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))
            args.update(queries=queries, destinations=action.get('destinations', self.destinations),
                        place_hints=action.get('place_hints', self.place_hints))
        elif tool == 'search_public':
            args.update(names=[' '.join(t['name'].split()) for t in action.get('public_targets', [])],
                        topics=action.get('topics'))
        elif tool == 'inspect_source':
            item = next((s for s in supplies if s['id'] == action.get('supply_id')), {})
            product = item.get('product_url')
            default = product if isinstance(product, str) and product.startswith('https://') else item.get('source_url')
            args.update(supply_id=action.get('supply_id'),
                        url=default if action.get('url') is None else action['url'])
        elif tool == 'inspect_geography':
            ids = action.get('supply_ids', [])
            args['supply_ids'] = list(dict.fromkeys(ids)) if isinstance(ids, list) and all(isinstance(x, str) for x in ids) else ids
        return json.dumps(args, sort_keys=True, ensure_ascii=False)

    def _public_search(self, action):
        targets, topics = action.get('public_targets'), action.get('topics')
        if not isinstance(targets, list) or not isinstance(topics, list):
            return self._blocked('invalid_public_query', '缺少程序生成的公共研究查询。')
        usage = {}
        leads = search_public_evidence(targets, topics, on_usage=usage.update)
        ids = [t['id'] for t in targets]
        records = []
        for lead in leads[:3]:
            metadata = {'query_id': action.get('query_id'), 'gap_ids': action.get('gap_ids', []),
                        'supply_ids': ids, 'research_topics': topics}
            records.append({**lead, **metadata, 'source_url': lead['url']})
            try:
                raw = self.reader(lead['url'])
            except Exception:
                raw = {'status': 'unavailable', 'method': 'direct_http', 'reason': 'reader_failed'}
            records.append({**_read_receipt(raw, lead['url'], ids), **metadata})
        return {'status': 'completed' if leads else 'blocked',
                'detail': '已执行定向公共搜索；线索与直读分别记录，适用规则仍需研究。',
                'supplies': [], 'supply_ids': ids, 'evidence': records,
                'result': {'lookup_scope': 'public_transport_rules', 'search_executed': True,
                           'search_usage': usage,
                           'lead_count': len(leads), 'source_attempt_count': len(leads),
                           'gap_status': 'needs_research'}}

    @staticmethod
    def _blocked(reason, detail):
        return {"status": "blocked", "detail": detail, "result": {"reason": reason},
                "supplies": [], "evidence": [], "supply_ids": []}

    @staticmethod
    def _finish(tool, action, supplies, evidence, outcome):
        merged_supplies = _merge(supplies, outcome.get("supplies", []))
        now = datetime.now(timezone.utc).isoformat()
        receipt = {"method": "tool_execution", "tool": tool, "status": outcome["status"],
                   "title": TITLES.get(tool, "未执行的能力动作"), "detail": outcome["detail"],
                   "supply_ids": outcome.get("supply_ids", []), "fetched_at": now,
                   "verified_fields": [], "untrusted_external_content": False,
                   "action_hash": _identifier("action", action), "warning": BOUNDARY}
        receipt['request_signature'] = outcome.get('request_signature')
        if action and isinstance(action, dict) and action.get('query_id'):
            receipt.update(query_id=action.get('query_id'), gap_ids=action.get('gap_ids', []),
                           research_topics=action.get('topics', []))
        receipt["id"] = _identifier("execution", receipt)
        fresh = _evidence_ids(outcome.get("evidence", [])) + [receipt]
        merged_evidence = _merge(evidence, fresh)
        task = {"id": "task-" + receipt["id"], "kind": tool, "status": outcome["status"],
                "title": receipt["title"], "detail": outcome["detail"],
                "supply_ids": receipt["supply_ids"], "evidence_ids": [e["id"] for e in fresh]}
        return {"action": tool, "status": task["status"], "title": task["title"],
                "detail": task["detail"], "task": task, "supplies": merged_supplies,
                "evidence": merged_evidence, "evidence_ids": task["evidence_ids"],
                "capabilities": capability_records(merged_supplies, merged_evidence),
                "result": {**outcome["result"], "boundary": BOUNDARY}}

    def _search(self, action, supplies):
        queries = action.get("queries")
        if (not isinstance(queries, list) or not 1 <= len(queries) <= MAX_QUERIES
                or any(not isinstance(q, str) or not q.strip() or len(q) > 2000 for q in queries)):
            return self._blocked("invalid_queries", "检索需要1—12个非空查询，每个最多2000字符。")
        queries = list(dict.fromkeys(q.strip() for q in queries))
        # ResearchService caps each result set. Search independently, then merge
        # old picks, so a full old pool cannot suppress all further discovery.
        discovery_context = {}
        destinations = action.get('destinations', self.destinations)
        if destinations:
            discovery_context['destinations'] = destinations
        hints = action.get('place_hints', self.place_hints)
        if hints:
            discovery_context['place_hints'] = hints
        if action.get('candidate_scope'):
            parameters = inspect.signature(self.researcher.research).parameters.values()
            if not any(p.name == 'candidate_scope' or p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters):
                return self._blocked('candidate_scope_unsupported', '候选研究器不支持限定范围，未执行广泛搜索；Agent 仍需补查。')
            discovery_context['candidate_scope'] = action['candidate_scope']
        research = self.researcher.research(queries, **discovery_context)
        candidates = research["supplies"]
        bound = action.get('candidate_scope')
        if bound:
            candidates = filter_candidates(candidates, bound)
        ids = [s["id"] for s in candidates]
        allowed = _sources(candidates)
        receipts = [_read_receipt(e, e.get("source_url") or e.get("url"),
                                 [id_ for id_ in e.get("supply_ids", []) if id_ in ids])
                    for e in research.get("evidence", [])
                    if e.get('provider') or (e.get("source_url") or e.get("url")) in allowed]
        read_count = sum(e["status"] == "read" for e in receipts)
        new_count = len(set(ids) - {s["id"] for s in supplies})
        detail = (f"检索返回{len(candidates)}项候选，其中新增{new_count}项；"
                  f"公开来源读到{read_count}/{len(receipts)}项。" + BOUNDARY)
        return {"status": "completed" if candidates else "blocked", "detail": detail, "supplies": candidates,
                "evidence": receipts, "supply_ids": ids,
                "result": {"supply_ids": ids, "new_supply_count": new_count,
                           "candidate_count": len(candidates), "source_read_count": read_count,
                           "source_attempt_count": len(receipts),
                           "lookup_scope": "open_destination_sources_and_cached_catalog",
                           "stats": deepcopy(research.get("stats", {}))}}

    def _inspect(self, action, supplies, evidence=None):
        id_, url = action.get("supply_id"), action.get("url")
        by_id = {s["id"]: s for s in supplies}
        if id_ is not None and (not isinstance(id_, str) or id_ not in by_id):
            return self._blocked("unknown_supply", "只能读取已发现候选的公开来源。")
        if url is None and id_ is not None:
            item = by_id[id_]
            product = item.get("product_url")
            url = product if isinstance(product, str) and product.startswith("https://") else item.get("source_url")
        allowed = _sources(supplies)
        if action.get('query_id'):
            for record in evidence or []:
                if record.get('query_id') == action['query_id'] and record.get('source_url'):
                    allowed.setdefault(record['source_url'], record.get('supply_ids', []))
        if (not isinstance(url, str) or url not in allowed
                or (id_ is not None and id_ not in allowed[url])):
            return self._blocked("source_not_discovered", "链接必须与已发现候选的来源完全一致。")
        try:
            raw = self.reader(url)
            if not isinstance(raw, dict):
                raise TypeError("Source reader must return a receipt")
        except Exception as error:
            raw = {"status": "unavailable", "reason": "reader_exception", "error_type": type(error).__name__}
        receipt = _read_receipt(raw, url, allowed[url])
        if action.get('query_id'):
            receipt['query_id'] = action['query_id']
        success = receipt["status"] == "read"
        detail = ("已读到公开页面文字；内容仍需结合日期复核。" if success
                  else "本次未读到可用的公开页面文字，不能把页面检查标为完成。") + BOUNDARY
        return {"status": "completed" if success else "blocked", "detail": detail,
                "supplies": [], "evidence": [receipt], "supply_ids": allowed[url],
                "result": {"source_url": url, "read_status": receipt["status"],
                           "reason": receipt.get("reason"), "title": receipt.get("title", ""),
                           "excerpt": receipt["text"], "evidence_id": receipt["id"],
                           "truncated": receipt["truncated"], "verified_fields": [],
                           "untrusted_external_content": True, "warning": READ_WARNING}}

    def _geography(self, action, supplies):
        ids = action.get("supply_ids")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_GEOGRAPHY_SUPPLIES
                or any(not isinstance(id_, str) or not id_ for id_ in ids)):
            return self._blocked("invalid_supply_ids", "位置核对需要1—20个已发现候选ID。")
        ids = list(dict.fromkeys(ids))
        by_id = {s["id"]: s for s in supplies}
        if any(id_ not in by_id for id_ in ids):
            return self._blocked("unknown_supply", "位置核对包含未发现的候选，不能推测其位置。")
        selected = [by_id[id_] for id_ in ids]
        known = [s for s in selected if located(s)]
        context = spatial_context(selected)
        pairs = [{"supply_ids": [left["id"], right["id"]],
                  "straight_line_km": round(haversine_km(left["lat"], left["lng"],
                                                          right["lat"], right["lng"]), 1)}
                 for index, left in enumerate(known) for right in known[index + 1:]]
        status = ("blocked" if not known else "needs_confirmation"
                  if context["unknown_location_ids"] else "completed")
        detail = (f"{len(known)}/{len(selected)}项有历史地点锚点，计算{len(pairs)}组直线距离下界；"
                  "不是车程、可步行性或实时路线，缺失坐标仍待核对。")
        return {"status": status, "detail": detail, "supplies": [], "evidence": [],
                "supply_ids": ids, "result": {**context, "pairs": pairs}}
