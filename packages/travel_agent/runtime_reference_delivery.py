"""Render admitted current evidence as an explicit pretravel reference checklist.

This module projects an admission receipt into delivery artifacts.  It does not
change source grades, extend freshness windows or decide that a pending recheck
has passed.
"""
from __future__ import annotations

import copy
from datetime import date
import hashlib
import html
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .comparison_card_budget import private_json
from .itinerary_assembly import render_handbook, render_html
from .itinerary_assembly_pipeline import content_sha256, persist_case
from .revision_plan import plan_sha256


SECTION = "pretravel_recheck"
TITLE = "出行前复核清单"
WARNING = ("以下信息仅作当前参考。核验日期与资料有效期反映本次收集的证据，"
           "不保证出行当日仍然有效。价格、开放、班次、库存及适用条件均需在出行前重新核验；"
           "保留原日期与来源等级，待复核不等于已核验。")
REFERENCE_DISPOSITIONS = {
    "current-reference",
    "current_reference_only_pretravel_recheck_required",
}
ASSUMPTION_DISPOSITION = "explicit_reversible_assumption_not_user_answer"


def _trip_days(fixture):
    confirmed = fixture["bundle"].get("confirmed_trip_context", {}).get("days", [])
    by_day = {row["day"]: row.get("date") for row in confirmed}
    days = []
    for row in fixture["plan"]["days"]:
        value = row.get("date") or by_day.get(row["day"])
        date.fromisoformat(value or "")
        days.append(dict(day=row["day"], date=value))
    if not days:
        raise ValueError("reference delivery requires dated trip days")
    return days


def _used_days(fixture, seed_id, days):
    targets = {item_id for rule in fixture["bundle"].get("price_rules", [])
               if rule.get("seed_id") == seed_id for item_id in rule.get("item_ids", [])}
    dated = {row["day"]: row for row in days}
    used, item_ids = [], []
    for day in fixture["plan"]["days"]:
        matches = [item for item in day["items"] if item.get("supply_id") == seed_id
                   or seed_id in item.get("supply_ids", []) or item["id"] in targets]
        if matches:
            used.append(copy.deepcopy(dated[day["day"]]))
            item_ids.extend(item["id"] for item in matches)
    return used, item_ids


def _default_assumptions(bundle):
    selected = {row.get("question_id") for row in bundle.get("selections", [])
                if row.get("choice_id") and row.get("application_status") != "default_assumption"}
    assumptions = []
    for question in bundle.get("intent_card", {}).get("questions", []):
        if question["id"] in selected or question.get("answer"):
            continue
        default_id = question.get("default_choice_id")
        choice = next((row for row in question.get("choices", [])
                       if row["id"] == default_id), None)
        if choice:
            assumptions.append(dict(
                question_id=question["id"], question=question["text"],
                default_choice_id=default_id, choice_label=choice["label"],
                assumption=question.get("default_assumption", ""),
                proposed_plan_effect=choice.get("plan_change", ""),
                status="unanswered_default_assumption", user_answer=None,
                application_status="not_proved", reversible=True,
            ))
    return assumptions


def _admitted_findings(admission):
    if not isinstance(admission, dict) or admission.get("admitted") is not True:
        raise ValueError("reference delivery requires an admitted receipt")
    findings = admission.get("findings")
    if not isinstance(findings, list):
        raise ValueError("admission receipt lacks per-finding dispositions")
    references, assumptions = {}, set()
    for finding in findings:
        disposition = finding.get("disposition") if isinstance(finding, dict) else None
        if disposition in REFERENCE_DISPOSITIONS:
            seed_id = finding.get("seed_id")
            if not isinstance(seed_id, str) or seed_id in references:
                raise ValueError("reference admission has missing or duplicate supply IDs")
            references[seed_id] = finding
        elif disposition == ASSUMPTION_DISPOSITION:
            evidence = (finding.get("raw") or {}).get("evidence") or {}
            question_id = (evidence.get("selection") or {}).get("question_id")
            if not isinstance(question_id, str) or question_id in assumptions:
                raise ValueError("assumption admission has missing or duplicate question IDs")
            assumptions.add(question_id)
    return references, assumptions


def _recheck_text(seed):
    parts = ["向原始来源复核所列主张在对应出行日期是否仍适用"]
    if seed.get("offers"):
        parts.append("重新确认价格、币种、计价单位、人数/资格、包含项目与可售状态")
    if seed.get("applicability"):
        parts.append("原适用条件：" + str(seed["applicability"]))
    parts.extend("待确认：" + str(value) for value in seed.get("unknowns", []))
    return "；".join(parts) + "。"


def _reference_row(seed, finding, fixture, trip_days):
    if finding.get("atom_sha256") != content_sha256(seed):
        raise ValueError("admitted supply atom hash differs from the fixture")
    used, item_ids = _used_days(fixture, seed["id"], trip_days)
    relevant = used or trip_days
    fresh = date.fromisoformat(seed["fresh_until"][:10])
    uncovered = [copy.deepcopy(row) for row in relevant
                 if date.fromisoformat(row["date"]) > fresh]
    if not uncovered:
        raise ValueError("current-reference admission has no post-freshness travel date")
    return dict(
        seed_id=seed["id"], atom_sha256=finding["atom_sha256"], claim=seed["claim"],
        source_url=seed.get("source_url", ""), source_name=seed.get("source_name", ""),
        checked_at=seed.get("checked_at"), fresh_until=seed["fresh_until"],
        source_grade=seed.get("grade"), original_status=seed.get("status"),
        verification_status=seed.get("verification_status", "unknown"),
        independent_cross_check=seed.get("independent_cross_check", "unknown"),
        binding="explicit_item_or_price_rule" if used else "all_trip_days_envelope",
        item_ids=item_ids, travel_days=copy.deepcopy(relevant),
        travel_dates=[row["date"] for row in relevant], uncovered_days=uncovered,
        status="pending", reference_status="current-reference",
        what_to_recheck=_recheck_text(seed),
    )


def build_reference_delivery(fixture, admission):
    """Project approved future-window findings into a destination-neutral checklist."""
    if admission.get("fixture_sha256") not in (None, content_sha256(fixture)):
        raise ValueError("admission receipt belongs to a different fixture")
    references, approved_assumptions = _admitted_findings(admission)
    days, rows = _trip_days(fixture), []
    seeds = fixture["bundle"].get("supply_seeds", [])
    if len({row.get("id") for row in seeds}) != len(seeds):
        raise ValueError("fixture supply atom IDs must be unique")
    for seed in seeds:
        if seed.get("id") in references:
            rows.append(_reference_row(seed, references[seed["id"]], fixture, days))
    if {row["seed_id"] for row in rows} != set(references):
        raise ValueError("admission references supply atoms absent from the fixture")
    assumptions = _default_assumptions(fixture["bundle"])
    if (approved_assumptions
            and {row["question_id"] for row in assumptions} != approved_assumptions):
        raise ValueError("unanswered defaults do not match approved assumption findings")
    checked = sorted({str(row["checked_at"]) for row in rows})
    daily_notices = [dict(
        day=row["day"], date=row["date"],
        notice=(f"第 {row['day']} 天（{row['date']}）为暂定时间表。供给资料核验日期："
                f"{'、'.join(checked)}；价格、开放、场次、库存与政策仅作当前参考，出行前须复核。"),
    ) for row in days]
    return dict(
        schema_version="runtime-reference-delivery-1", title=TITLE,
        plan_sha256=plan_sha256(fixture["plan"]), fixture_sha256=content_sha256(fixture),
        admission_sha256=content_sha256(admission), trip_dates=[row["date"] for row in days],
        travel_start=min(row["date"] for row in days), travel_end=max(row["date"] for row in days),
        warning=WARNING, rows=rows, pending_count=len(rows), rows_sha256=content_sha256(rows),
        default_assumptions=assumptions, assumptions_sha256=content_sha256(assumptions),
        daily_notices=daily_notices, daily_notices_sha256=content_sha256(daily_notices),
        price_references=[], price_references_sha256=content_sha256([]),
    )


def _delivery_lines(delivery):
    lines = [f"## {TITLE}",
             f"出行日期：{delivery['travel_start']} 至 {delivery['travel_end']}",
             delivery["warning"], f"待复核供应事实：{delivery['pending_count']} 条。"]
    for row in delivery["rows"]:
        lines += [f"### {row['seed_id']} · 待复核", row["claim"],
                  f"核验日期：{row['checked_at']}；资料有效至：{row['fresh_until']}",
                  "对应出行日期：" + "、".join(row["travel_dates"]),
                  "覆盖方式：" + ("已绑定日程项 " + "、".join(row["item_ids"])
                              if row["item_ids"] else "尚未用于日程，按全程日期范围保留复核"),
                  f"来源：{row['source_name']}；原等级：{row['source_grade']}；"
                  f"独立交叉核验：{row['independent_cross_check']}",
                  "原始链接：" + row["source_url"], "复核事项：" + row["what_to_recheck"]]
    lines += ["### 尚未回答的默认假设", "以下均为可撤回假设，未记录为用户回答，也未证明已正确应用到计划。"]
    for row in delivery["default_assumptions"]:
        lines += ["- " + row["question"], "默认选择：" + row["choice_label"],
                  "暂定假设：" + row["assumption"], "拟议影响：" + row["proposed_plan_effect"]]
    lines.append("### 逐日日期提示")
    lines.extend("- " + row["notice"] for row in delivery["daily_notices"])
    return lines


def _verify_delivery(delivery):
    checks = ((delivery.get("rows_sha256"), content_sha256(delivery.get("rows"))),
              (delivery.get("assumptions_sha256"),
               content_sha256(delivery.get("default_assumptions"))),
              (delivery.get("daily_notices_sha256"), content_sha256(delivery.get("daily_notices"))),
              (delivery.get("price_references_sha256"),
               content_sha256(delivery.get("price_references"))))
    if any(actual != expected for actual, expected in checks):
        raise ValueError("reference delivery content changed after its hash receipt")
    return delivery


def render_reference_delivery(delivery):
    """Render the complete checklist as standalone Markdown and escaped HTML."""
    lines = _delivery_lines(_verify_delivery(delivery))
    markdown = "\n\n".join(lines) + "\n"
    blocks = [f'<section id="pretravel-recheck" data-plan-sha256="{delivery["plan_sha256"]}" '
              f'data-rows-sha256="{delivery["rows_sha256"]}">']
    for line in lines:
        if line.startswith("## "):
            blocks.append("<h2>" + html.escape(line[3:]) + "</h2>")
        elif line.startswith("### "):
            blocks.append("<h3>" + html.escape(line[4:]) + "</h3>")
        else:
            blocks.append("<p>" + html.escape(line) + "</p>")
    return dict(markdown=markdown, html="".join(blocks) + "</section>")


def _price_reference(row, pending):
    refs = [pending[key] for key in row.get("source_ids", []) if key in pending]
    if row.get("disposition") != "priced" or not refs:
        return None
    details = "；".join(f"{ref['seed_id']}：核验 {ref['checked_at']}，有效窗口至 "
                        f"{ref['fresh_until']}，出行日期 {'、'.join(ref['travel_dates'])}"
                        for ref in refs)
    notice = (f"当前参考价：{row.get('line_amount')} {row.get('currency')}；{details}。"
              "出行前待复核；不保证出行日价格、库存或适用条件。")
    return dict(budget_id=row["id"], item_ids=copy.deepcopy(row.get("item_ids", [])),
                source_ids=[ref["seed_id"] for ref in refs], status="pending",
                reference_status="current-reference", notice=notice)


def _attach_price_labels(handbook, delivery):
    pending = {row["seed_id"]: row for row in delivery["rows"]}
    item_ids = {item["id"] for day in handbook["sections"]["daily"] for item in day["items"]}
    references = []
    for row in handbook["sections"]["budget"]["items"]:
        reference = _price_reference(row, pending)
        if reference is None:
            continue
        if not reference["item_ids"] or set(reference["item_ids"]) - item_ids:
            raise ValueError("current-reference budget row lacks current daily item bindings")
        row["reference_status"] = "current-reference"
        row["pretravel_recheck"] = copy.deepcopy(reference)
        row.setdefault("reference_base_basis", str(row.get("basis", "")))
        row["basis"] = row["reference_base_basis"] + "；" + reference["notice"]
        references.append(reference)
    delivery["price_references"] = references
    delivery["price_references_sha256"] = content_sha256(references)


def _atomic_text(path, value):
    path = Path(path)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                prefix="." + path.name + ".", delete=False) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _refresh_rendered_outputs(handbook, paths):
    delivery = _verify_delivery(handbook["sections"][SECTION])
    standalone = render_reference_delivery(delivery)
    markdown, rendered = render_handbook(handbook), render_html(handbook)
    if "## 02 " not in markdown or '<section id="section-2">' not in rendered:
        raise ValueError("handbook renderer lacks reference-delivery insertion points")
    markdown = markdown.replace("## 02 ", standalone["markdown"] + "\n## 02 ", 1)
    rendered = rendered.replace('<section id="section-2">',
                                standalone["html"] + '<section id="section-2">', 1)
    rendered = rendered.replace('<a href="#section-2">',
                                f'<a href="#pretravel-recheck">{TITLE}</a><a href="#section-2">', 1)
    notices = {row["day"]: row for row in delivery["daily_notices"]}
    for day in handbook["sections"]["daily"]:
        notice = notices[day["day"]]["notice"]
        heading = f"第 {day['day']} 天 · {day['title']}"
        markdown_marker = "### " + heading + "\n"
        html_marker = "<h3>" + html.escape(heading) + "</h3>"
        if markdown_marker not in markdown or html_marker not in rendered:
            raise ValueError("handbook renderer lacks a daily date insertion point")
        markdown = markdown.replace(markdown_marker, markdown_marker + "\n" + notice + "\n", 1)
        rendered = rendered.replace(html_marker, html_marker +
                                    '<p class="notice" data-reference-schedule="pending">' +
                                    html.escape(notice) + "</p>", 1)
    daily_items = {item["id"]: item for day in handbook["sections"]["daily"]
                   for item in day["items"]}
    for reference in delivery["price_references"]:
        for item_id in reference["item_ids"]:
            item = daily_items[item_id]
            label = f"{item['time']}–{item['end_time']} {item['title']} [{item_id}]"
            markdown = markdown.replace("- " + label + "\n\n",
                                        "- " + label + "\n\n  " + reference["notice"] + "\n\n", 1)
            marker = '<p class="entry">' + html.escape(label) + "</p>"
            rendered = rendered.replace(marker, marker + '<p class="notice" data-price-reference="' +
                                        html.escape(item_id, quote=True) + '">' +
                                        html.escape(reference["notice"]) + "</p>", 1)
    _atomic_text(paths["handbook_markdown"], markdown)
    _atomic_text(paths["handbook_html"], rendered)
    return dict(plan_sha256=handbook["plan_sha256"], handbook_sha256=content_sha256(handbook),
                rows_sha256=delivery["rows_sha256"],
                markdown_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
                html_sha256=hashlib.sha256(rendered.encode()).hexdigest())


def apply_reference_delivery(case, admission, directory):
    """Persist a new case edition with the admitted checklist retained in JSON and renderings."""
    result = copy.deepcopy(case)
    current_fixture = dict(copy.deepcopy(result["fixture"]), plan=copy.deepcopy(result["plan"]))
    delivery = build_reference_delivery(current_fixture, admission)
    handbook = result["handbook"]
    _attach_price_labels(handbook, delivery)
    handbook["sections"][SECTION] = delivery
    rows = [row for row in handbook["delivery_checklist"] if row.get("id") != SECTION]
    rows.append(dict(id=SECTION, label=TITLE, structure_status="已做", evidence_status="待复核",
                     pending_count=delivery["pending_count"], rows_sha256=delivery["rows_sha256"]))
    handbook["delivery_checklist"] = rows
    handbook.setdefault("audit", {})["runtime_reference_delivery"] = dict(
        admission_sha256=delivery["admission_sha256"], rows_sha256=delivery["rows_sha256"],
        original_paths=copy.deepcopy(case.get("paths", {})),
    )
    result = persist_case(result, directory)
    result["reference_delivery"] = _refresh_rendered_outputs(result["handbook"], result["paths"])
    private_json(result["paths"]["case"], result)
    return result
