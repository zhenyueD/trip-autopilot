"""Bounded open-data discovery, with no city catalog or model-authored facts.

MediaWiki Action API: query/search, revisions, coordinates, pageimages, imageinfo.
Open-Meteo geocoding provides GeoNames destination anchors. Wikivoyage parsing
reuses the atomizer. These are source observations, not current travel guarantees.
"""
from .phase1_budget_context import ContextThreadPoolExecutor as ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import threading
import tempfile
import time
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import getproxies

from packages.atomizer.common import haversine_km
from packages.atomizer.fetch import _clean_wiki, _extract_template, _split_params
from .material_reader import _ReadDeadline, _public_ip, _read_body
from .supply import _VisibleText

WIKIPEDIA = "https://en.wikipedia.org/w/api.php"
WIKIVOYAGE = "https://en.wikivoyage.org/w/api.php"
COMMONS = "https://commons.wikimedia.org/w/api.php"
GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"
ENDPOINTS = frozenset({WIKIPEDIA, WIKIVOYAGE, COMMONS, GEOCODING})
# Context limits are shared by intent validation and discovery. Provider budgets
# below remain independent: accepting more context is not unbounded search.
MAX_DESTINATIONS, MAX_HINTS, MAX_CONTEXT_TEXT = 8, 20, 200
MAX_SEARCHES = 8
MAX_GEOCODING_RESULTS = 10
MAX_RESULTS, MAX_IMAGES, MAX_DISTRICTS = 60, 12, 3
MAX_DISTANCE_KM = 35  # Bounded urban search, not automatic intercity/day-trip expansion.
REQUEST_SECONDS = 5.0
_WORKERS = threading.BoundedSemaphore(8)
CACHE_DIR = Path(__file__).resolve().parents[2] / "data/cache/destination_discovery"
CACHE_TTL_SECONDS = 86400
LIMITS = ("公开协作资料的地点锚点，不是现场入口或当前营业证明；描述为来源说法。"
          "开放时间、票价、库存、适用性与预订均未核验；按出行日期另行确认。")


def validate_context(destinations=None, place_hints=None):
    def records(values, limit, keys):
        values = [] if values is None else values
        if not isinstance(values, list) or len(values) > limit:
            raise ValueError(f"Expected at most {limit} destination/place records")
        result = []
        for value in values:
            if (not isinstance(value, dict) or not isinstance(value.get("name"), str)
                    or not value["name"].strip() or len(value["name"]) > MAX_CONTEXT_TEXT):
                raise ValueError(f"Destination/place name must be nonempty text of at most {MAX_CONTEXT_TEXT} characters")
            item = {"name": value["name"].strip()}
            for key in keys:
                if key in value:
                    if not isinstance(value[key], str) or len(value[key]) > MAX_CONTEXT_TEXT:
                        raise ValueError("Destination/place fields must be bounded text")
                    item[key] = value[key].strip()
            result.append(item)
        return result
    destinations_out = records(destinations, MAX_DESTINATIONS, ("search_name",))
    for raw, destination in zip(destinations or [], destinations_out):
        if "country_code" in raw:
            code = raw["country_code"]
            if code is not None and (not isinstance(code, str) or not re.fullmatch(r"[A-Z]{2}", code)):
                raise ValueError("Destination country_code must be two uppercase letters or null")
            destination["country_code"] = code
    return destinations_out, records(place_hints, MAX_HINTS, ("destination", "kind"))


def _connect_provider(parsed, deadline):
    proxy_url = os.environ.get("TRAVEL_DISCOVERY_PROXY") or getproxies().get("https", "")
    proxy = urlsplit(proxy_url)
    if proxy_url:
        # Existing local OS proxy or explicit trusted server configuration only.
        # It cannot come from the model/query or apply to arbitrary URL readers.
        if (proxy.scheme != "http" or proxy.hostname not in {"127.0.0.1", "localhost", "::1"}
                or proxy.username or proxy.password or proxy.path not in {"", "/"}):
            raise ValueError("Only the trusted local HTTPS proxy is supported")
        connection = http.client.HTTPConnection(proxy.hostname, proxy.port or 80,
                                                 timeout=deadline.remaining())
        connection.set_tunnel(parsed.hostname, 443)
        connection.connect()
        raw = connection.sock
        deadline.track(raw)
        return _secure_socket(raw, parsed.hostname, deadline)
    # Validate every DNS answer, then prefer IPv4 on hosts with no IPv6 route.
    answers = socket.getaddrinfo(parsed.hostname, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
    if not answers or len(answers) > 64 or any(not _public_ip(a[4][0]) for a in answers):
        raise ValueError("Provider DNS is not public")
    family, socktype, protocol, _, address = min(answers, key=lambda a: a[0] != socket.AF_INET)
    raw = socket.socket(family, socktype, protocol)
    deadline.track(raw)
    try:
        raw.settimeout(deadline.remaining())
        raw.connect(address)
        return _secure_socket(raw, parsed.hostname, deadline)
    except Exception:
        raw.close()
        raise


def _secure_socket(raw, hostname, deadline):
    try:
        secured = ssl.create_default_context().wrap_socket(
            raw, server_hostname=hostname, do_handshake_on_connect=False)
        deadline.track(secured)
        secured.settimeout(deadline.remaining())
        secured.do_handshake()
        return secured
    except Exception:
        raw.close()
        raise


def _fetch_json_once(url, deadline):
    from .phase1_budget_context import physical_attempt, clamp_deadline
    deadline.end = clamp_deadline(deadline.end)
    with physical_attempt('destination_http', deadline.remaining()):
        return _fetch_json_once_unbudgeted(url, deadline)


def _fetch_json_once_unbudgeted(url, deadline):
    parsed = urlsplit(url)
    sock = _connect_provider(parsed, deadline)
    connection = http.client.HTTPConnection(parsed.hostname, 443)
    connection.sock = sock
    try:
        connection.request("GET", parsed.path + "?" + parsed.query, headers={
            "User-Agent": "TravelDestinationResearch/0.1 (public-data travel planning)",
            "Accept": "application/json", "Accept-Encoding": "identity", "Connection": "close"})
        response = connection.getresponse()
        if (response.status != 200 or response.getheader("Content-Type", "").split(";")[0]
                != "application/json" or response.getheader("Content-Encoding", "identity") != "identity"):
            raise ValueError("Provider response unavailable")  # Redirects are never followed.
        body, truncated = _read_body(response, sock, deadline)
        if truncated:
            raise ValueError("Provider body exceeds limit")
        data = json.loads(body)
        if not isinstance(data, dict) or data.get("error"):
            raise ValueError("Provider returned no usable result")
        return data
    finally:
        connection.close()


def fetch_provider_json(endpoint, params):
    """Fixed HTTPS endpoints, vetted direct DNS or trusted local proxy, hard deadline."""
    if endpoint not in ENDPOINTS:
        raise ValueError("Unknown discovery endpoint")
    url = endpoint + "?" + urlencode(params)
    cached = _cached_response(url)
    if cached is not None:
        return cached
    if len(url) > 12000 or not _WORKERS.acquire(blocking=False):
        raise ValueError("Discovery request budget unavailable")
    deadline, output = _ReadDeadline(), queue.Queue(maxsize=1)
    from .phase1_budget_context import clamp_deadline, context_target
    deadline.end = clamp_deadline(time.monotonic() + REQUEST_SECONDS)
    def worker():
        try:
            output.put((True, _fetch_json_once(url, deadline)))
        except Exception as error:
            output.put((False, type(error).__name__))
        finally:
            deadline.cancel()
            _WORKERS.release()
    threading.Thread(target=context_target(worker), name="destination-provider", daemon=True).start()
    try:
        success, data = output.get(timeout=max(0, deadline.end - time.monotonic()))
        if not success:
            raise ValueError("Provider request unavailable: " + data)
        data["_discovery_fetched_at"] = datetime.now(timezone.utc).isoformat()
        _cache_response(url, data)
        return data
    except queue.Empty:
        raise TimeoutError("Provider deadline") from None
    finally:
        deadline.cancel()


def _cache_path(url):
    return CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest() + ".json")


def _cached_response(url):
    path = _cache_path(url)
    try:
        if time.time() - path.stat().st_mtime > CACHE_TTL_SECONDS or path.stat().st_size > 270000:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("url") != url or not isinstance(payload.get("data"), dict):
            return None
        return {**payload["data"], "_discovery_cache_hit": True}
    except (OSError, ValueError, TypeError):
        return None


def _cache_response(url, data):
    temporary = None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(url)
        if not path.exists() and sum(1 for _ in CACHE_DIR.glob("*.json")) >= 128:
            return
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CACHE_DIR, delete=False) as output:
            temporary = output.name
            json.dump({"url": url, "data": data}, output, ensure_ascii=False)
        os.replace(temporary, path)
        temporary = None
    except OSError:
        pass  # Read-only deployment remains usable; cache is not a prerequisite.
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def _recent_provider_data(endpoint):
    """Reuse exact page/file identities from other successful cached query shapes."""
    for path in list(CACHE_DIR.glob("*.json"))[:128]:
        try:
            if time.time() - path.stat().st_mtime > CACHE_TTL_SECONDS or path.stat().st_size > 270000:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if str(payload.get("url", "")).startswith(endpoint + "?") and isinstance(payload.get("data"), dict):
                yield payload["data"]
        except (OSError, ValueError, TypeError):
            continue


def _text(value, limit=1200):
    parser = _VisibleText()
    parser.feed(str(value or ""))
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:limit]


def _coords(lat, lng):
    try:
        lat, lng = float(lat), float(lng)
        if math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180:
            return lat, lng
    except (TypeError, ValueError):
        pass
    return None


def _pages(data):
    pages = data.get("query", {}).get("pages", [])
    return list(pages.values()) if isinstance(pages, dict) else pages


def _record(id_, name, kind, city, coords, source, description, method, checked_at):
    return {"id": id_, "name": name, "title": name, "kind": kind, "city": city,
            "provider": "Wikivoyage" if method == "wikivoyage_listing" else "Wikipedia",
            "lat": coords[0], "lng": coords[1], "geo_status": "source_place_anchor_not_entrance",
            "geo_source": {"url": source, "kind": method, "limits": LIMITS},
            "source_url": source, "product_url": None, "description": description,
            "tags": [city, kind], "constraints": [LIMITS], "checked_at": checked_at,
            "image_url": None, "image_source_url": None,
            "verification": {"status": "public_source_observation", "verified_fields": [],
                             "observed_fields": ["name", "lat", "lng"]},
            "evidence": [{"source_url": source, "method": method, "checked_at": checked_at,
                          "supports": ["source_place_identity", "source_coordinates"],
                          "limits": LIMITS, "untrusted_external_content": True}],
            "price": {"status": "unknown", "amount": None, "currency": None},
            "availability": {"status": "unknown"}, "booking_status": "not_booked"}


def _wikipedia_records(data, anchors, checked_at):
    checked_at = data.get("_discovery_fetched_at", checked_at)
    records = []
    for page in _pages(data):
        coordinate = (page.get("coordinates") or [{}])[0]
        coords = _coords(coordinate.get("lat"), coordinate.get("lon"))
        if not coords or not page.get("pageid") or not anchors:
            continue
        nearest = min(anchors, key=lambda a: haversine_km(*coords, a["lat"], a["lng"]))
        if haversine_km(*coords, nearest["lat"], nearest["lng"]) > MAX_DISTANCE_KM:
            continue  # Geographic relevance comes from sources, not suggested city labels.
        name = _text(page.get("title"), 240)
        if not name or name.casefold() in {nearest["name"].casefold(), nearest["search_name"].casefold()}:
            continue  # A city's centroid is not an experience venue.
        source = "https://en.wikipedia.org/wiki/" + quote(name.replace(" ", "_"))
        item = _record("wp-en-" + str(page["pageid"]), name, "place", nearest["name"], coords,
                       source, _text(page.get("extract") or page.get("description")),
                       "wikipedia_place", checked_at)
        item["_image_file"] = page.get("pageimage")
        records.append(item)
    return records


def _wikivoyage_records(data, destination, anchor, checked_at):
    checked_at = data.get("_discovery_fetched_at", checked_at)
    records = []
    for page in _pages(data):
        revisions = page.get("revisions") or []
        if not revisions:
            continue
        wikitext = revisions[0].get("slots", {}).get("main", {}).get("content", "")
        for match in re.finditer(r"\{\{\s*(see|do|eat|drink|sleep|buy|listing)\b", wikitext, re.I):
            extracted = _extract_template(wikitext, match.start())
            if not extracted:
                continue
            params = _split_params(extracted[0][match.end() - match.start():-2])
            name = _text(_clean_wiki(params.get("name", "")), 240)
            coords = _coords(params.get("lat"), params.get("long"))
            if not name or not coords:
                continue
            if anchor and haversine_km(*coords, anchor["lat"], anchor["lng"]) > MAX_DISTANCE_KM:
                continue
            kind = {"sleep": "hotel", "eat": "dining", "drink": "dining", "do": "activity"}.get(
                match[1].lower(), "place")
            title = page.get("title", destination["search_name"])
            source = "https://en.wikivoyage.org/wiki/" + quote(title.replace(" ", "_"))
            id_ = "wv-" + hashlib.sha256(f"{title}:{name}".encode()).hexdigest()[:16]
            item = _record(id_, name, kind, destination["name"], coords, source,
                           _text(_clean_wiki(params.get("content", ""))), "wikivoyage_listing", checked_at)
            item["_image_file"] = params.get("image")
            item["_wikidata"] = params.get("wikidata")
            records.append(item)
    return records


def _attach_images(records, data, checked_at):
    checked_at = data.get("_discovery_fetched_at", checked_at)
    images = {page.get("title", "").replace("File:", "", 1).replace("_", " "):
              (page.get("imageinfo") or [{}])[0] for page in _pages(data)}
    for item in records:
        info = images.get(str(item.get("_image_file") or "").removeprefix("File:").replace("_", " "), {})
        metadata = info.get("extmetadata", {})
        license_ = _text(metadata.get("LicenseShortName", {}).get("value"), 120)
        author = _text(metadata.get("Artist", {}).get("value"), 500)
        url = info.get("thumburl") or info.get("url") or ""
        source = info.get("descriptionurl") or ""
        if (not license_ or not author or urlsplit(url).scheme != "https"
                or urlsplit(url).hostname not in {"upload.wikimedia.org", "thumb.wikimedia.org"}
                or urlsplit(source).scheme != "https" or urlsplit(source).hostname != "commons.wikimedia.org"):
            continue
        item.update(image_url=url, image_source_url=source, image_author=author,
                    image_license=license_, image_license_url=metadata.get("LicenseUrl", {}).get("value"),
                    image_attribution=f"{author} · {license_} · Wikimedia Commons",
                    image_is_generated=False, image_checked_at=checked_at,
                    image_usage_notes="来源条目关联图片；许可与署名由 Commons 返回，不是实时现场画面。")


class DestinationDiscovery:
    """Finite provider work per call; injected fetcher makes tests entirely offline."""
    def __init__(self, fetcher=None):
        self.fetcher = fetcher or fetch_provider_json

    def _batch(self, jobs, evidence):
        def run(job):
            key, endpoint, params = job
            try:
                data = self.fetcher(endpoint, params)
                return key, data, {"status": "read", "provider": endpoint, "query": params,
                    "fetched_at": data.get("_discovery_fetched_at", datetime.now(timezone.utc).isoformat()),
                    "cache_hit": bool(data.get("_discovery_cache_hit")),
                    "source_url": endpoint + "?" + urlencode(params), "verified_fields": [],
                    "body_sha256": hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
                    "untrusted_external_content": True, "warning": LIMITS}
            except Exception as error:
                return key, {}, {"status": "unavailable", "provider": endpoint,
                    "source_url": endpoint, "error_type": type(error).__name__, "verified_fields": [],
                    "untrusted_external_content": True, "warning": LIMITS}
        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="destination-discovery") as pool:
            results = list(pool.map(run, jobs))
        evidence.extend(receipt for _, _, receipt in results)
        return {key: data for key, data, _ in results}

    def enrich_images(self, supplies):
        """Fill at most 12 selected missing images by exact source identity only.

        No searches or approximate name matches. Existing fields/IDs are preserved;
        unavailable source/licence data leaves the original record unchanged.
        """
        output = deepcopy(supplies)
        targets = [s for s in output if not s.get("image_url")][:MAX_IMAGES]
        wikipedia, voyage = {}, {}
        for item in targets:
            source = urlsplit(item.get("source_url") or "")
            if source.scheme != "https" or not source.path.startswith("/wiki/") or source.query:
                continue
            match = re.fullmatch(r"wp-en-(\d+)", item.get("id", ""))
            if source.hostname == "en.wikipedia.org" and match:
                wikipedia[match[1]] = item
            elif source.hostname == "en.wikivoyage.org" and item.get("id", "").startswith("wv-"):
                title = unquote(source.path.removeprefix("/wiki/")).replace("_", " ")
                voyage.setdefault(title, []).append(item)
        jobs = [(title, WIKIVOYAGE, {"action": "query", "format": "json", "formatversion": 2,
                 "prop": "revisions", "rvprop": "content", "rvslots": "main", "titles": title})
                for title in voyage]
        if self.fetcher is fetch_provider_json:
            for payload in _recent_provider_data(WIKIPEDIA):
                for page in _pages(payload):
                    item = wikipedia.get(str(page.get("pageid")))
                    if item is not None and page.get("pageimage"):
                        item["_image_file"] = page["pageimage"]
        missing_ids = [id_ for id_, item in wikipedia.items() if not item.get("_image_file")]
        if missing_ids:
            jobs.append(("wikipedia", WIKIPEDIA, {"action": "query", "format": "json", "formatversion": 2,
                "prop": "pageimages", "pageids": "|".join(missing_ids), "piprop": "name",
                "pilimit": MAX_IMAGES, "pilicense": "free"}))
        evidence, now = [], datetime.now(timezone.utc).isoformat()
        data = self._batch(jobs, evidence) if jobs else {}
        for page in _pages(data.get("wikipedia", {})):
            item = wikipedia.get(str(page.get("pageid")))
            if item is not None and page.get("pageimage"):
                item["_image_file"] = page["pageimage"]
        for title, items in voyage.items():
            records = _wikivoyage_records(data.get(title, {}),
                {"name": items[0].get("city", ""), "search_name": title}, None, now)
            by_id = {record["id"]: record for record in records}
            for item in items:
                image = by_id.get(item["id"], {}).get("_image_file")
                if image:
                    item["_image_file"] = image
        self._enrich_image_files(targets, evidence, now)
        for item in targets:
            item.pop("_image_file", None)
        return output

    def _enrich_image_files(self, records, evidence, now):
        if self.fetcher is fetch_provider_json:
            for payload in _recent_provider_data(COMMONS):
                _attach_images([r for r in records if not r.get("image_url")], payload, now)
        filenames = list(dict.fromkeys(r["_image_file"] for r in records
                                      if r.get("_image_file") and not r.get("image_url")))[:MAX_IMAGES]
        jobs = [(f"images{index}", COMMONS, {"action": "query", "format": "json", "formatversion": 2,
            "prop": "imageinfo", "titles": "|".join("File:" + name.removeprefix("File:") for name in filenames[index:index + 4]),
            "iiprop": "url|extmetadata", "iiurlwidth": 640,
            "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl"}) for index in range(0, len(filenames), 4)]
        for payload in self._batch(jobs, evidence).values() if jobs else ():
            _attach_images(records, payload, now)

    def discover(self, queries, destinations=None, place_hints=None, candidate_scope=None):
        from .research_focus import filter_candidates
        filter_candidates([], candidate_scope)
        destinations, hints = validate_context(destinations, place_hints)
        if not destinations:
            return {"supplies": [], "evidence": [], "stats": {"request_count": 0}}
        destinations = [{**d, "search_name": d.get("search_name") or d["name"]} for d in destinations]
        now = datetime.now(timezone.utc).isoformat()
        evidence, jobs = [], []
        for index, destination in enumerate(destinations):
            search = destination["search_name"]
            jobs.append((f"geo{index}", GEOCODING, {"name": search.split(",")[0], "count": MAX_GEOCODING_RESULTS,
                         "language": "en", "format": "json"}))
            jobs.append((f"wv{index}", WIKIVOYAGE, {"action": "query", "format": "json", "formatversion": 2,
                         "prop": "revisions|coordinates", "rvprop": "content", "rvslots": "main", "redirects": 1,
                         "titles": search, "list": "prefixsearch", "pssearch": search + "/", "pslimit": MAX_DISTRICTS}))
        searches = list(dict.fromkeys([h["name"] + " " + h.get("destination", destinations[0]["search_name"])
                                      for h in hints] + list(queries)))[:MAX_SEARCHES]
        if not searches:
            searches = [d["search_name"] + " attractions" for d in destinations]
        for index, search in enumerate(searches):
            jobs.append((f"wp{index}", WIKIPEDIA, {"action": "query", "format": "json", "formatversion": 2,
                "generator": "search", "gsrsearch": search[:500], "gsrlimit": 5,
                "prop": "coordinates|pageimages|extracts|description", "coprimary": "primary",
                "exintro": 1, "explaintext": 1, "exchars": 900, "piprop": "name", "pilicense": "free"}))
        data = self._batch(jobs, evidence)
        anchor_rejections = []
        anchors = self._anchors(data, destinations, anchor_rejections)
        records, districts = [], []
        for index, destination in enumerate(destinations):
            anchor = next((a for a in anchors if all(a.get(key) == destination.get(key)
                for key in ('name', 'search_name', 'country_code'))), None)
            if destination.get("country_code") and not anchor:
                continue  # Country-less page coordinates cannot override the requested country.
            records.extend(_wikivoyage_records(data[f"wv{index}"], destination, anchor, now))
            for page in data[f"wv{index}"].get("query", {}).get("prefixsearch", [])[:MAX_DISTRICTS]:
                title = page.get("title", "")
                if title.startswith(destination["search_name"] + "/") and title.count("/") == 1:
                    districts.append((f"district{index}:{title}", WIKIVOYAGE, {"action": "query", "format": "json",
                        "formatversion": 2, "prop": "revisions", "rvprop": "content", "rvslots": "main", "titles": title}))
        for key, payload in data.items():
            if key.startswith("wp"):
                records.extend(_wikipedia_records(payload, anchors, now))
        district_data = self._batch(districts[:6], evidence) if districts else {}
        for key, payload in district_data.items():
            destination = destinations[int(key.split(":", 1)[0].replace("district", ""))]
            anchor = next((a for a in anchors if all(a.get(key) == destination.get(key)
                for key in ('name', 'search_name', 'country_code'))), None)
            records.extend(_wikivoyage_records(payload, destination, anchor, now))
        records = self._select(filter_candidates(records, candidate_scope), queries, hints)
        self._enrich_image_files(records, evidence, now)
        for record in records:
            record.pop("_image_file", None)
            record.pop("_wikidata", None)
        request_count = len(evidence)
        evidence.extend(anchor_rejections)
        for record in records:
            evidence.append({"status": "read", "source_url": record["source_url"],
                "supply_ids": [record["id"]], "fetched_at": record["checked_at"],
                "method": "provider_api_observation", "text": record["description"],
                "verified_fields": [], "untrusted_external_content": True, "warning": LIMITS})
        return {"supplies": records, "evidence": evidence,
                "stats": {"request_count": request_count, "destination_matches": anchors,
                          "provider_failures": [{key: receipt[key] for key in ("provider", "status", "error_type")
                                                 if key in receipt} for receipt in evidence
                                                if receipt["status"] == "unavailable"],
                          "open_candidate_count": len(records)}}

    @staticmethod
    def _anchors(data, destinations, rejections=None):
        anchors = []
        for index, destination in enumerate(destinations):
            before = len(anchors)
            country_code = destination.get("country_code")
            for result in data[f"geo{index}"].get("results", [])[:MAX_GEOCODING_RESULTS]:
                if country_code and result.get("country_code") != country_code:
                    if rejections is not None:
                        rejections.append({"status": "rejected_country", "provider": GEOCODING,
                            "source_url": GEOCODING + "?" + urlencode({"name": destination["search_name"].split(",")[0],
                                "count": MAX_GEOCODING_RESULTS, "language": "en", "format": "json"}),
                            "source_id": result.get("id"), "requested_country_code": country_code,
                            "observed_country_code": result.get("country_code"), "verified_fields": [],
                            "untrusted_external_content": True, "warning": LIMITS})
                    continue
                coords = _coords(result.get("latitude"), result.get("longitude"))
                if coords:
                    anchors.append({**destination, "lat": coords[0], "lng": coords[1],
                                    "matched_name": result.get("name"), "country": result.get("country"),
                                    "provider": "Open-Meteo / GeoNames", "source_id": result.get("id")})
                    break
            if len(anchors) == before and not country_code:
                for page in _pages(data[f"wv{index}"]):
                    coordinate = (page.get("coordinates") or [{}])[0]
                    coords = _coords(coordinate.get("lat"), coordinate.get("lon"))
                    if coords:
                        anchors.append({**destination, "lat": coords[0], "lng": coords[1],
                                        "matched_name": page.get("title"), "provider": "Wikivoyage",
                                        "source_id": page.get("pageid")})
                        break
        return anchors

    @staticmethod
    def _select(records, queries, hints):
        names = [h["name"].casefold() for h in hints]
        tokens = set(re.findall(r"\w{3,}", " ".join(queries).casefold()))
        def score(item):
            content = (item["name"] + " " + item["description"]).casefold()
            return (any(name in content for name in names), bool(item.get("_image_file")),
                    sum(token in content for token in tokens))
        ranked = sorted(records, key=score, reverse=True)
        kinds = dict.fromkeys(item["kind"] for item in ranked)
        reserved = [item for kind in kinds for item in [r for r in ranked if r["kind"] == kind][:4]]
        selected, identities = [], set()
        for item in reserved + ranked:
            identity = (re.sub(r"\W", "", item["name"]).casefold(), item["city"])
            if identity not in identities:
                selected.append(item)
                identities.add(identity)
        return selected[:MAX_RESULTS]


def enrich_supply_images(supplies):
    """Return the same selected supply IDs with only source-backed image fields filled."""
    return DestinationDiscovery().enrich_images(supplies)
