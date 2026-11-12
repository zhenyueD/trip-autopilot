"""Destination-neutral, offline cache for accepted itinerary supply atoms.

Cache hits are evidence atoms, never a count of independent sources.  Each read
rechecks the stored bytes, source identity, quote and original validity window;
``checked_at`` and ``fresh_until`` are retained rather than renewed by caching.
"""
from __future__ import annotations

import copy
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
import unicodedata
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .comparison_card_budget import private_json
from .run_reliability import RunPreflightError


SCHEMA_VERSION = "runtime-evidence-pool-1"
REQUIRED_SEED_FIELDS = (
    "id", "claim", "source_url", "quote", "checked_at", "fresh_until", "grade"
)


def _digest(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _destination(value):
    if not isinstance(value, dict):
        raise ValueError("destination must contain city, currency and timezone")
    if any(not isinstance(value.get(key), str) or not value[key].strip()
           for key in ("city", "currency", "timezone")):
        raise ValueError("destination city, currency and timezone must be nonempty strings")
    result = {key: value[key].strip() for key in ("city", "currency", "timezone")}
    if not re.fullmatch(r"[A-Z]{3}", result["currency"]):
        raise ValueError("destination currency must be a three-letter uppercase code")
    try:
        zone = ZoneInfo(result["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("destination timezone must be a valid IANA timezone") from exc
    return result, zone


def _city_key(destination) -> str:
    city = unicodedata.normalize("NFKC", destination["city"])
    city = " ".join(city.casefold().split())
    return hashlib.sha256(city.encode()).hexdigest()


def _local_day(value, zone):
    if isinstance(value, datetime):
        return value.astimezone(zone).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not re.match(r"\d{4}-\d{2}-\d{2}(?:$|[T ])", value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _local_day(parsed, zone)


def _valid_source_url(value) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _canonical_source(value) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
    return urlunsplit((parsed.scheme.casefold(), host, path, parsed.query, ""))


def _bundle_seeds(bundle):
    if not isinstance(bundle, dict):
        raise ValueError("bundle must be an object")
    seeds, alias = bundle.get("supply_seeds"), bundle.get("seeds")
    if not isinstance(seeds, list) or not seeds or any(not isinstance(row, dict) for row in seeds):
        raise ValueError("bundle supply_seeds must be a nonempty object list")
    if alias != seeds:
        raise ValueError("bundle seeds and supply_seeds must describe the same atoms")
    identifiers = [row.get("id") for row in seeds]
    if any(not isinstance(value, str) or not value for value in identifiers):
        raise ValueError("supply atom IDs must be nonempty strings")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("supply atom IDs must be unique")
    return seeds


def _seed_rejection(seed, as_of, zone):
    if any(not isinstance(seed.get(key), str) or not seed[key].strip()
           for key in REQUIRED_SEED_FIELDS):
        return "missing_source_field"
    if not _valid_source_url(seed["source_url"]):
        return "invalid_source_url"
    checked = _local_day(seed["checked_at"], zone)
    fresh = _local_day(seed["fresh_until"], zone)
    if checked is None or fresh is None or checked > fresh:
        return "invalid_freshness_window"
    if checked > as_of:
        return "future_observation"
    if fresh < as_of:
        return "expired"
    return None


def _validate_put_bundle(destination, bundle):
    stored_destination, _ = _destination(bundle.get("destination"))
    if stored_destination != destination:
        raise ValueError("bundle destination does not match cache destination")
    seeds = _bundle_seeds(bundle)
    for seed in seeds:
        if any(not isinstance(seed.get(key), str) or not seed[key].strip()
               for key in REQUIRED_SEED_FIELDS):
            raise ValueError(f'supply atom {seed.get("id", "unknown")} lacks source evidence')
        if not _valid_source_url(seed["source_url"]):
            raise ValueError(f'supply atom {seed["id"]} has an invalid source URL')
    for key, identity in (("sources", "id"), ("supply_evidence", "seed_id")):
        rows = bundle.get(key)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"bundle {key} must be an object list")
        identifiers = [row.get(identity) for row in rows]
        if (len(identifiers) != len(seeds) or len(set(identifiers)) != len(identifiers)
                or set(identifiers) != {row["id"] for row in seeds}):
            raise ValueError(f"bundle {key} must cover every supply atom exactly")
    sources = {row["id"]: row for row in bundle["sources"]}
    evidence = {row["seed_id"]: row for row in bundle["supply_evidence"]}
    for seed in seeds:
        if _evidence_mismatch(seed, sources[seed["id"]], evidence[seed["id"]]):
            raise ValueError(f'supply atom {seed["id"]} differs from source evidence')
    return seeds


def _evidence_mismatch(seed, source, evidence):
    source_fields = {
        "url": "source_url", "content": "quote", "checked_at": "checked_at",
        "fresh_until": "fresh_until", "grade": "grade",
    }
    evidence_fields = {
        "claim": "claim", "quote": "quote", "source_url": "source_url",
        "checked_at": "checked_at", "fresh_until": "fresh_until",
    }
    return (any(source.get(left) != seed.get(right) for left, right in source_fields.items())
            or any(evidence.get(left) != seed.get(right)
                   for left, right in evidence_fields.items()))


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                prefix="." + path.name + ".", delete=False) as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _filtered_bundle(bundle, accepted, destination, as_of, cache_meta):
    result = copy.deepcopy(bundle)
    identifiers = {row["id"] for row in accepted}
    result["seeds"] = copy.deepcopy(accepted)
    result["supply_seeds"] = copy.deepcopy(accepted)
    for seed in result["supply_seeds"]:
        seed["as_of"] = as_of.isoformat()
        seed["freshness_status"] = "current"
    result["seeds"] = copy.deepcopy(result["supply_seeds"])
    result["sources"] = [copy.deepcopy(row) for row in bundle["sources"]
                         if row.get("id") in identifiers]
    for source in result["sources"]:
        source["freshness_status"] = "current"
    result["supply_evidence"] = [copy.deepcopy(row) for row in bundle["supply_evidence"]
                                 if row.get("seed_id") in identifiers]
    for evidence in result["supply_evidence"]:
        evidence["freshness_status"] = "current"
    result["destination"] = copy.deepcopy(destination)
    provenance = copy.deepcopy(result.get("provenance") or {})
    provenance.update(supply_count=len(accepted),
                      verified_supply_count=sum(
                          row.get("verification_status") == "verified" for row in accepted),
                      current_fresh_supply_count=len(accepted))
    provenance["runtime_evidence_pool"] = copy.deepcopy(cache_meta)
    result["provenance"] = provenance
    return result


class EvidencePool:
    """Cache accepted supply bundles by a normalized, destination-neutral city key."""

    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir)

    def _path(self, destination):
        return self.cache_dir / ("city-" + _city_key(destination) + ".json")

    def put(self, destination, bundle):
        destination, _ = _destination(destination)
        seeds = _validate_put_bundle(destination, bundle)
        atoms = [{"seed_id": seed["id"], "atom_sha256": _digest(seed)} for seed in seeds]
        envelope = dict(schema_version=SCHEMA_VERSION, city_key=_city_key(destination),
                        destination=copy.deepcopy(destination), bundle_sha256=_digest(bundle),
                        atoms=atoms, bundle=copy.deepcopy(bundle))
        path = self._path(destination)
        _atomic_json(path, envelope)
        return dict(path=str(path), city_key=envelope["city_key"],
                    bundle_sha256=envelope["bundle_sha256"], atom_count=len(atoms))

    def put_selected(self, destination, selected_supply_path):
        pointer_path = Path(selected_supply_path).resolve()
        pointer_raw = pointer_path.read_bytes()
        pointer = json.loads(pointer_raw)
        if set(pointer) != {"path"} or not isinstance(pointer["path"], str):
            raise ValueError("selected supply pointer must contain only a result path")
        result_path = Path(pointer["path"])
        if not result_path.is_absolute():
            result_path = (pointer_path.parent / result_path).resolve()
        result_raw = result_path.read_bytes()
        receipt = self.put(destination, json.loads(result_raw))
        return dict(receipt, pointer_path=str(pointer_path), result_path=str(result_path),
                    pointer_sha256=hashlib.sha256(pointer_raw).hexdigest(),
                    result_sha256=hashlib.sha256(result_raw).hexdigest())

    def load(self, destination, as_of):
        destination, zone = _destination(destination)
        planning_day = _local_day(as_of, zone)
        if planning_day is None:
            raise ValueError("as_of must be a valid ISO date or timestamp")
        path = self._path(destination)
        if not path.exists():
            return dict(bundle=None, hits=[], rejected=[])
        envelope = json.loads(path.read_text())
        if (envelope.get("schema_version") != SCHEMA_VERSION
                or envelope.get("city_key") != _city_key(destination)
                or envelope.get("destination") != destination):
            raise ValueError("cached destination identity is inconsistent")
        bundle = envelope.get("bundle")
        seeds = _bundle_seeds(bundle)
        sources = {row.get("id"): row for row in bundle.get("sources", [])
                   if isinstance(row, dict)}
        evidence = {row.get("seed_id"): row for row in bundle.get("supply_evidence", [])
                    if isinstance(row, dict)}
        expected = {row.get("seed_id"): row.get("atom_sha256")
                    for row in envelope.get("atoms", []) if isinstance(row, dict)}
        if len(expected) != len(seeds):
            raise ValueError("cached atom ledger does not cover the bundle")
        accepted, hits, rejected = [], [], []
        for seed in seeds:
            actual = _digest(seed)
            reason = "atom_hash_mismatch" if expected.get(seed["id"]) != actual else None
            source, support = sources.get(seed["id"]), evidence.get(seed["id"])
            if source is None or support is None or _evidence_mismatch(seed, source or {}, support or {}):
                reason = reason or "source_evidence_mismatch"
            reason = reason or _seed_rejection(seed, planning_day, zone)
            record = dict(seed_id=seed["id"], atom_sha256=actual,
                          checked_at=seed.get("checked_at"), fresh_until=seed.get("fresh_until"))
            if reason:
                rejected.append(dict(record, reason=reason))
                continue
            record.update(source_group_sha256=_digest(_canonical_source(seed["source_url"])),
                          quote_sha256=_digest(seed["quote"]), status="cache-hit")
            accepted.append(copy.deepcopy(seed))
            hits.append(record)
        cache_meta = dict(city_key=envelope["city_key"], path=str(path),
                          stored_bundle_sha256=envelope.get("bundle_sha256"),
                          read_as_of=planning_day.isoformat(), hit_count=len(hits),
                          rejected_count=len(rejected))
        filtered = _filtered_bundle(bundle, accepted, destination, planning_day, cache_meta)
        return dict(bundle=filtered, hits=hits, rejected=rejected)


def admit_runtime_supply(bundle, *, as_of, travel_dates):
    """Admit current source atoms and demote post-freshness travel use to reference."""
    destination, zone = _destination(bundle.get("destination") if isinstance(bundle, dict) else None)
    planning_day = _local_day(as_of, zone)
    if planning_day is None:
        raise ValueError("as_of must be a valid ISO date or timestamp")
    if not isinstance(travel_dates, (list, tuple)) or not travel_dates:
        raise ValueError("travel_dates must be a nonempty date list")
    parsed_dates = [_local_day(value, zone) for value in travel_dates]
    if any(value is None for value in parsed_dates):
        raise ValueError("travel_dates contains an invalid date")
    if len(set(parsed_dates)) != len(parsed_dates):
        raise ValueError("travel_dates must not contain duplicates")
    if any(value < planning_day for value in parsed_dates):
        raise ValueError("travel_dates must not precede the planning date")
    seeds = _validate_put_bundle(destination, bundle)
    findings = []
    for seed in seeds:
        rejection = _seed_rejection(seed, planning_day, zone)
        if rejection:
            raise ValueError("supply atom is not current at planning time: " + rejection)
        fresh = _local_day(seed["fresh_until"], zone)
        outside = [value for value in parsed_dates if value > fresh]
        findings.append(dict(
            seed_id=seed["id"], atom_sha256=_digest(seed),
            checked_at=seed["checked_at"], fresh_until=seed["fresh_until"],
            verification_status=seed.get("verification_status", "unknown"),
            disposition="current-reference" if outside else "current",
            pretravel_recheck_required=bool(outside),
            travel_dates=[value.isoformat() for value in parsed_dates],
            outside_freshness_dates=[value.isoformat() for value in outside],
        ))
    return dict(schema_version="runtime-supply-admission-1", admitted=True,
                destination=copy.deepcopy(destination), as_of=planning_day.isoformat(),
                travel_dates=[value.isoformat() for value in parsed_dates],
                bundle_sha256=_digest(bundle), findings=findings)


def _planning_context(fixture, as_of):
    if not isinstance(fixture, dict) or not isinstance(fixture.get("bundle"), dict):
        raise ValueError("fixture must contain a supply bundle")
    bundle = fixture["bundle"]
    _, zone = _destination(bundle.get("destination"))
    planning_day = _local_day(as_of, zone)
    if planning_day is None:
        raise ValueError("as_of must be a valid ISO date or timestamp")
    seeds = bundle.get("supply_seeds")
    if (not isinstance(seeds, list) or not seeds
            or any(not isinstance(row, dict) for row in seeds)):
        raise ValueError("fixture bundle must contain supply atoms")
    identifiers = [row.get("id") for row in seeds]
    if (any(not isinstance(value, str) or not value for value in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise ValueError("fixture supply atom IDs must be unique nonempty strings")
    return bundle, zone, planning_day, {seed["id"]: seed for seed in seeds}


def _validate_default_finding(finding, evidence, bundle):
    selection = evidence.get("selection")
    if not isinstance(selection, dict):
        raise ValueError("default-assumption finding lacks a selection")
    questions = bundle.get("intent_card", {}).get("questions", [])
    matches = [(index, row) for index, row in enumerate(questions)
               if isinstance(row, dict) and row.get("id") == selection.get("question_id")]
    if len(matches) != 1:
        raise ValueError("default-assumption finding references an unknown question")
    index, question = matches[0]
    default_id = question.get("default_choice_id")
    choice = next((row for row in question.get("choices", [])
                   if isinstance(row, dict) and row.get("id") == default_id), None)
    expected_path = f"/bundle/intent_card/questions/{index}/default_choice_id"
    explicit = [row for row in bundle.get("selections", []) if isinstance(row, dict)
                and row.get("question_id") == question["id"] and row.get("choice_id")
                and row.get("application_status") != "default_assumption"]
    if (question.get("answer") or explicit or choice is None
            or selection.get("choice_id") != default_id
            or evidence.get("default_choice_id") != default_id
            or evidence.get("selection_path") != expected_path
            or finding.get("path") != expected_path
            or evidence.get("choice") != choice
            or evidence.get("default_assumption") != question.get("default_assumption", "")):
        raise ValueError("only the fixture's unanswered default may be admitted as an assumption")


def _classify_fixture_finding(finding, bundle, seeds, planning_day, zone):
    if not isinstance(finding, dict) or finding.get("classification") != "fixture":
        raise ValueError("preflight finding is outside the approved fixture class")
    reason = finding.get("reason")
    evidence = finding.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("preflight finding lacks structured evidence")
    if reason == "selection_plan_mapping_unresolved":
        if (finding.get("status") != "unresolved"
                or evidence.get("application_status") != "default_assumption"):
            raise ValueError("only unanswered defaults may be admitted as assumptions")
        _validate_default_finding(finding, evidence, bundle)
        return dict(disposition="explicit_reversible_assumption_not_user_answer")
    allowed = {
        "seed_trip_window_not_covered": "conflict",
        "seed_trip_coverage_unresolved": "unresolved",
    }
    if reason not in allowed or finding.get("status") != allowed[reason]:
        raise ValueError("finding is outside approved current-reference classes")
    seed_id = evidence.get("seed_id")
    seed = seeds.get(seed_id)
    if seed is None:
        raise ValueError("preflight finding references an unknown supply atom")
    rejection = _seed_rejection(seed, planning_day, zone)
    if rejection:
        raise ValueError("supply atom is not current at planning time: " + rejection)
    if (evidence.get("checked_at") != seed["checked_at"]
            or evidence.get("fresh_until") != seed["fresh_until"]):
        raise ValueError("preflight freshness evidence differs from the supply atom")
    fresh = _local_day(seed["fresh_until"], zone)
    days = evidence.get("days")
    if not isinstance(days, list) or not days:
        raise ValueError("current-reference finding must identify travel dates")
    travel_days = []
    for row in days:
        day = _local_day(row.get("date"), zone) if isinstance(row, dict) else None
        if day is None:
            raise ValueError("current-reference finding contains an invalid travel date")
        travel_days.append(day)
    if not any(day > fresh for day in travel_days):
        raise ValueError("finding does not describe freshness ending before travel")
    return dict(disposition="current_reference_only_pretravel_recheck_required",
                seed_id=seed_id, atom_sha256=_digest(seed))


def preflight_runtime_fixture(fixture, approval, model, output_dir, as_of):
    """Run the existing binary preflight and admit only two recorded finding classes.

    The raw preflight remains failed.  This receipt records the caller-approved
    per-finding disposition without changing fixture data or treating a future
    travel date as fresh evidence.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle, zone, planning_day, seeds = _planning_context(fixture, as_of)
    raw_path = output_dir / "raw-fixture-preflight.json"
    try:
        raw = model.preflight(fixture=fixture, approval=approval, output_path=raw_path)
    except RunPreflightError:
        raw = json.loads(raw_path.read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("passed"), bool):
        raise ValueError("preflight did not return a structured report")
    receipt = dict(admitted=False, raw_preflight_passed=raw["passed"],
                   fixture_sha256=_digest(fixture), approval_sha256=_digest(approval),
                   as_of=planning_day.isoformat(), findings=[])
    try:
        top_findings = raw.get("findings")
        if not isinstance(top_findings, list):
            raise ValueError("preflight report lacks top-level findings")
        if raw["passed"]:
            if top_findings:
                raise ValueError("passing preflight report contains findings")
            receipt["admitted"] = True
            return receipt
        if len(top_findings) != 1:
            raise ValueError("failed preflight report must contain one fixture wrapper")
        for issue in top_findings:
            if (not isinstance(issue, dict)
                    or issue.get("code") != "frozen_fixture_inconsistent"
                    or issue.get("path") != "fixtures[0]"):
                code = issue.get("code", "unknown") if isinstance(issue, dict) else "unknown"
                raise ValueError("unrelated preflight failure: " + code)
        fixtures = raw.get("fixtures")
        if (not isinstance(fixtures, list) or len(fixtures) != 1
                or not isinstance(fixtures[0], dict)
                or not isinstance(fixtures[0].get("findings"), list)):
            raise ValueError("preflight report lacks fixture findings")
        fixture_findings = fixtures[0]["findings"]
        if not fixture_findings:
            raise ValueError("failed preflight report contains no fixture findings")
        wrapped = (top_findings[0].get("evidence") or {}).get("findings")
        if wrapped != fixture_findings:
            raise ValueError("top-level and fixture finding ledgers differ")
        for finding in fixture_findings:
            disposition = _classify_fixture_finding(
                finding, bundle, seeds, planning_day, zone
            )
            receipt["findings"].append(dict(raw=copy.deepcopy(finding),
                                            finding_sha256=_digest(finding),
                                            **disposition))
        receipt["admitted"] = True
        return receipt
    finally:
        private_json(output_dir / "approved-admission.json", receipt)
