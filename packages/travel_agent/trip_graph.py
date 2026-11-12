"""Evidence-backed, public-safe state for one evolving trip.

The graph is intentionally small: it prevents source artifacts, facts, model
hypotheses, decisions and actions from collapsing into one blob of prose.
"""
from __future__ import annotations

import hashlib

from .validation import records, text


CRITICAL_DEFAULTS = ("同行", "成人", "儿童", "孩子", "老人", "人数", "几位", "预算", "价格带",
                     "签证", "免签", "落地签", "护照", "过敏", "健康", "行动不便", "体力")
QUESTION_SCOPE = ("时间", "日期", "几号", "多久", "同行", "几位", "人数", "儿童", "孩子", "老人", "预算", "人均")
INFERENCE_MARKERS = ("说明用户", "意味着", "可见用户", "用户偏好", "用户倾向", "关注高温", "适合喜欢")


def safe_assumptions(items):
    """Critical unknowns stay questions, never silent optimization inputs."""
    result = []
    for value in items or []:
        if isinstance(value, str) and value.strip() and not any(term in value for term in CRITICAL_DEFAULTS):
            result.append(value.strip())
    return result[:6]


def safe_questions(items):
    return [value.strip() for value in items or [] if isinstance(value, str) and value.strip()
            and any(term in value for term in QUESTION_SCOPE)][:3]


def grounded_understanding(items, materials):
    sources = {source.get("id"): source for source in materials
               if source.get("status") in ("read", "image_extracted")}
    result = []
    for item in records(items or [], "当前理解", 16):
        evidence = []
        for raw in records(item.get("evidence", []), "理解证据", 5):
            source = sources.get(raw.get("source_id"))
            quote = raw.get("quote")
            if source and isinstance(quote, str) and quote.strip() and quote in source.get("content", ""):
                evidence.append({"source_id": source["id"], "quote": quote,
                                 "label": source.get("label", "用户材料")})
        kinds = {sources[row["source_id"]].get("kind") for row in evidence}
        direct = item.get("kind") == "material_fact" and evidence
        if "document_extracted" in kinds and any(marker in item.get("text", "") for marker in INFERENCE_MARKERS):
            direct = False
        claim = item.get("text", "")
        quotes = " ".join(row["quote"] for row in evidence)
        status_terms = ("已订", "已确认", "已付款", "计划材料", "非供应商", "不是订单")
        if any(term in claim for term in status_terms) and not any(term in quotes for term in status_terms):
            direct = False
        kind = ("document_extracted" if direct and "document_extracted" in kinds else
                "image_extracted" if direct and "image_extracted" in kinds else
                "source_claim" if direct and "web_link" in kinds else
                "user_provided" if direct and evidence else "hypothesis")
        result.append({"text": text(item.get("text"), "理解内容"), "kind": kind,
                       "source": "、".join(dict.fromkeys(row["label"] for row in evidence)) or "可纠正推测",
                       "evidence": evidence})
    return result


def _id(prefix, value):
    return prefix + "-" + hashlib.sha256(value.encode()).hexdigest()[:14]


def build_trip_graph(materials, direction, plan=None):
    nodes, edges = [], []
    for source in materials:
        nodes.append({"id": source["id"], "type": "source_artifact",
                      "label": source.get("label", "用户材料"), "status": source.get("status", "unknown"),
                      "kind": source.get("kind", "unknown"), "message": source.get("message", "")})
    for index, item in enumerate(direction.get("understanding", []), 1):
        node_id = _id("fact" if item.get("kind") != "hypothesis" else "hypothesis",
                      str(index) + item.get("text", ""))
        node_type = "travel_fact" if item.get("kind") != "hypothesis" else "hypothesis"
        nodes.append({"id": node_id, "type": node_type, "label": item.get("text", ""),
                      "status": "evidence_backed" if node_type == "travel_fact" else "unverified",
                      "evidence": item.get("evidence", [])})
        for evidence in item.get("evidence", []):
            edges.append({"from": evidence["source_id"], "to": node_id, "relation": "supports"})
    for question in direction.get("questions", []):
        nodes.append({"id": _id("gap", question), "type": "gap", "label": question,
                      "status": "needs_user"})
    if plan:
        for decision in plan.get("decisions", []):
            node_id = _id("decision", decision.get("category", "") + decision.get("choice", ""))
            nodes.append({"id": node_id, "type": "decision", "label": decision.get("choice", ""),
                          "category": decision.get("category"), "status": decision.get("status", "needs_check")})
        actions = ((plan.get("preparation_pack") or {}).get("actions") or [])
        for action in actions:
            nodes.append({"id": action.get("id") or _id("action", action.get("title", "")),
                          "type": "action", "label": action.get("title", ""),
                          "status": action.get("status", "needs_research"),
                          "owner": action.get("owner", "agent")})
    facts = sum(node["type"] == "travel_fact" for node in nodes)
    hypotheses = sum(node["type"] == "hypothesis" for node in nodes)
    gaps = sum(node["type"] == "gap" for node in nodes)
    open_actions = sum(node["type"] == "action" and node["status"] not in
                       ("completed", "channel_confirmed", "user_reported") for node in nodes)
    return {"version": 1, "nodes": nodes, "edges": edges,
            "summary": {"evidence_backed_facts": facts, "unverified_hypotheses": hypotheses,
                        "needs_user": gaps, "open_actions": open_actions,
                        "ready_to_delegate": bool(facts) and not gaps and not open_actions}}
