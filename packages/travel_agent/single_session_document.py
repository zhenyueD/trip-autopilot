"""Single-session evidence ledger and versioned Markdown document mechanics.

No model, research, destination rules, or semantic adjudication lives here.
Sources must be registered by the caller immediately after an actual source read.
"""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import difflib
import hashlib
import html
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from .single_session_detail import canonical as detail_canonical, extract as detail_extract, render as detail_render
from .single_session_detail import split_overview
from .single_session_context import render as context_render, render_budget
from .single_session_presentation import render as presentation_render, accounting_view
from .single_session_detail import route_pending
from .single_session_assets import merge_assets
from .single_session_media import cover_html
from .single_session_reader_copy import reader_text, reader_markdown
from .single_session_edit_binding import validate as validate_edit_binding, is_complete_line_reorder


_NUMBER = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_NUMBER_TOKEN = re.compile(r"(?<![\d.,])" + _NUMBER + r"(?![\d.,])")
_CURRENCY = (r"(?:THB|USD|CNY|RMB|EUR|GBP|JPY|HKD|SGD|AUD|CAD|VND|KRW|MYR|"
             r"IDR|INR|TWD|NZD|CHF|PHP|AED|NT\$|US\$|HK\$|S\$|"
             r"人民币|泰铢|美元|港元|港币|欧元|英镑|日元|韩元|元|圆|"
             r"baht|dollars?|euros?|yuan|yen|[¥￥฿$€£₩₹₫])")
_BARE_PRICE = re.compile(_CURRENCY + r"\s*" + _NUMBER + r"|" +
                         _NUMBER + r"\s*" + _CURRENCY, re.IGNORECASE)
_PRICE_COLUMN = re.compile(_CURRENCY + r"|价格|金额|费用|单价|总价|预算|price|amount|cost",
                           re.IGNORECASE)
_MARKER = re.compile(r"\{\{(price|evidence):(E[1-9]\d*)\}\}")
_LEGACY_REFERENCE = re.compile(
    r"[\[［【]\s*(?:证据\s*)?(E[1-9]\d*(?:\s*[,，、/]\s*E[1-9]\d*)*)\s*[\]］】]")
_RECEIPT_FIELDS = ("changes", "judgment", "alternatives", "next_action")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _atomic_write(path, text):
    path = Path(path)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".document-", delete=False) as handle:
        handle.write(text)
        handle.flush()
        import os
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)


def _decimal(value, name, positive=False):
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")
    if abs(number.adjusted()) > 100 or len(number.as_tuple().digits) > 100:
        raise ValueError(f"{name} exceeds supported decimal precision")
    return number


def _number_text(number):
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value


def _unified_diff(before, after):
    lines = difflib.unified_diff(before.splitlines(keepends=True),
                                 after.splitlines(keepends=True),
                                 fromfile="previous", tofile="current")
    return "".join(line if line.endswith("\n") else
                   line + "\n\\ No newline at end of file\n" for line in lines)


def _safe_url(url):
    parsed = urlsplit(url)
    return parsed.scheme.lower() in ("https", "http") and bool(parsed.netloc)


def _check_price_columns(markdown):
    """Currency in a table header still binds each amount cell below it."""
    lines = markdown.splitlines()
    for index, line in enumerate(lines[:-1]):
        if ("|" not in line or not re.fullmatch(r"[\s|:\-]+", lines[index + 1])
                or "-" not in lines[index + 1]):
            continue
        headers = line.strip().strip("|").split("|")
        columns = [column for column, header in enumerate(headers)
                   if _PRICE_COLUMN.search(header)]
        for row in lines[index + 2:]:
            if "|" not in row or not row.strip():
                break
            cells = row.strip().strip("|").split("|")
            for column in columns:
                if column < len(cells) and _NUMBER_TOKEN.search(cells[column]):
                    raise ValueError("unbound price in amount column; use a price evidence marker")


def _inline(text):
    """Escape first; only the renderer can introduce tags and safe links."""
    escaped = html.escape(text, quote=True)

    def link(match):
        label, target = match.groups()
        raw_target = html.unescape(target)
        if not _safe_url(raw_target):
            return label + " (" + target + ")"
        return f'<a href="{target}" rel="noreferrer noopener">{label}</a>'

    escaped = re.sub(r"\[([^\]\n]+)\]\(([^\s)]+)\)", link, escaped)
    escaped = re.sub(r"\[\^(E[1-9]\d*)\]", lambda m:
                     f'<sup><a href="#rec{m[1][1:]}" aria-label="核查记录 {m[1][1:]}">{m[1][1:]}</a></sup>', escaped)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", escaped)
    return re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)


def _table(lines):
    cells = [line.strip().strip("|").split("|") for line in lines]
    header = "".join("<th>" + _inline(x.strip()) + "</th>" for x in cells[0])
    body = []
    for row in cells[2:]:
        body.append("<tr>" + "".join("<td>" + _inline(x.strip()) + "</td>"
                                    for x in row) + "</tr>")
    return '<div class="table-scroll"><table><thead><tr>' + header + \
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"


def _markdown_html(markdown):
    # The project environment has no Markdown dependency. This deliberately
    # small renderer never interprets raw HTML, images, or executable schemes.
    lines, output, index = markdown.splitlines(), [], 0
    while index < len(lines):
        line = lines[index]
        if ("|" in line and index + 1 < len(lines)
                and re.fullmatch(r"[\s|:\-]+", lines[index + 1])
                and "-" in lines[index + 1]):
            end = index + 2
            while end < len(lines) and "|" in lines[end] and lines[end].strip():
                end += 1
            output.append(_table(lines[index:end]))
            index = end
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        bullet = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.+)$", line)
        if heading:
            level, title = len(heading[1]), _inline(heading[2])
            output.append(f"<h{level}>{title}</h{level}>")
        elif bullet:
            output.append('<p class="list-item">• ' + _inline(bullet[1]) + "</p>")
        elif line.startswith("> "):
            output.append("<blockquote>" + _inline(line[2:]) + "</blockquote>")
        elif line.strip():
            output.append("<p>" + _inline(line) + "</p>")
        index += 1
    return "\n".join(output)


_CSS = """
:root{color-scheme:light;font-family:system-ui,-apple-system,sans-serif;color:#20342e;
background:#f4f3ef}body{margin:0}main{max-width:780px;margin:32px auto;padding:36px;
background:#fff;border:1px solid #dce2dc;border-radius:14px;overflow-wrap:anywhere}
h1{font-size:2rem;line-height:1.2}h2{margin-top:2.2rem;border-top:1px solid #e2e7e2;
padding-top:1.3rem}h3{margin-top:1.5rem}p,li,td{font-size:1rem;line-height:1.75}
a{color:#176447;text-underline-offset:3px}blockquote{margin:16px 0;padding:12px 18px;
border-left:3px solid #759786;background:#f5f8f4;line-height:1.7}code,pre{font-family:
ui-monospace,monospace}code{background:#eef2ee;padding:2px 4px;border-radius:4px}
pre{white-space:pre-wrap;font-size:.85rem;line-height:1.6}.list-item{padding-left:12px}
.table-scroll{overflow-x:auto}table{border-collapse:collapse;min-width:100%;margin:16px 0}
th,td{text-align:left;border:1px solid #dce2dc;padding:10px;min-width:90px}
th{background:#edf3ee}details{margin-top:28px;color:#50645b}summary{cursor:pointer}
@media(max-width:600px){main{margin:0;padding:22px 18px;border:0;border-radius:0}
h1{font-size:1.7rem}h2{font-size:1.3rem}th,td{padding:8px;font-size:.95rem}}
"""


class DocumentStore:
    """Local, single-writer ledger with immutable sources and document versions."""

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        if not (self.directory / "ledger.json").exists():
            self._write_json("ledger.json", {"sources": {}, "evidence": {}})
        if not (self.directory / "versions.json").exists():
            self._write_json("versions.json", {"current": None, "versions": []})

    def _read_json(self, name):
        return json.loads((self.directory / name).read_text(encoding="utf-8"))

    def _write_json(self, name, value):
        _atomic_write(self.directory / name, _json(value))

    @property
    def sources(self):
        return self._read_json("ledger.json")["sources"]

    @property
    def evidence(self):
        return self._read_json("ledger.json")["evidence"]

    def register_source(self, source_id, url, content, retrieved_at):
        for name, value in (("source_id", source_id), ("url", url),
                            ("content", content), ("retrieved_at", retrieved_at)):
            _text(value, name)
        if not _safe_url(url):
            raise ValueError("source URL must use http or https")
        try:
            datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("retrieved_at must be an ISO date or timestamp") from exc
        source = dict(source_id=source_id, url=url, content=content,
                      retrieved_at=retrieved_at, content_hash=_hash(content))
        ledger = self._read_json("ledger.json")
        previous = ledger["sources"].get(source_id)
        if previous is not None and previous != source:
            raise ValueError("source_id is immutable; register a new source_id")
        ledger["sources"][source_id] = source
        self._write_json("ledger.json", ledger)
        return deepcopy(source)

    def record_evidence(self, source_id, quote, claim, kind, unit_amount=None,
                        currency=None, quantity=None):
        ledger = self._read_json("ledger.json")
        source = ledger["sources"].get(source_id)
        if source is None:
            raise ValueError("unknown source_id; register a read source first")
        for name, value in (("quote", quote), ("claim", claim), ("kind", kind)):
            _text(value, name)
        if _hash(source["content"]) != source["content_hash"]:
            raise ValueError("source content hash mismatch")
        if quote not in source["content"]:
            raise ValueError("quote must occur verbatim in the registered source")
        number = len(ledger["evidence"]) + 1
        evidence_id = f"E{number}"
        record = dict(evidence_id=evidence_id, source_id=source_id, quote=quote,
                      claim=claim, kind=kind, url=source["url"],
                      retrieved_at=source["retrieved_at"], verified_at=_now(),
                      verification_status="单源未交叉确认",
                      source_content_hash=source["content_hash"])
        if kind == "price":
            record.update(self._price_fields(quote, unit_amount, currency, quantity))
        elif any(value is not None for value in (unit_amount, currency, quantity)):
            raise ValueError("price fields require kind='price'")
        marker_kind = "price" if kind == "price" else "evidence"
        record["marker"] = "{{" + marker_kind + ":" + evidence_id + "}}"
        ledger["evidence"][evidence_id] = record
        self._write_json("ledger.json", ledger)
        return deepcopy(record)

    @staticmethod
    def _price_fields(quote, unit_amount, currency, quantity):
        amount = _decimal(unit_amount, "unit_amount")
        count = _decimal(quantity, "quantity", positive=True)
        _text(currency, "currency")
        currency = currency.upper()
        if not re.fullmatch(r"[A-Z]{3}|[¥￥฿$€£₩₹₫]", currency):
            raise ValueError("currency must be a three-letter code or currency symbol")
        tokens = _NUMBER_TOKEN.findall(quote)
        matched = next((token for token in tokens
                        if Decimal(token.replace(",", "")) == amount), None)
        if matched is None:
            raise ValueError("unit_amount must match a whole decimal number in quote")
        with localcontext() as context:
            context.prec = 220
            total = amount * count
        return dict(unit_amount=_number_text(amount), quantity=_number_text(count),
                    currency=currency, total_amount=_number_text(total),
                    matched_quote_number=matched)

    def _expand(self, markdown, ledger):
        _text(markdown, "markdown")
        checks = []
        referenced = [evidence_id for match in _LEGACY_REFERENCE.finditer(markdown)
                      for evidence_id in re.findall(r"E[1-9]\d*", match[1])]
        stripped = _LEGACY_REFERENCE.sub("", _MARKER.sub("", markdown))
        if "{{" in stripped or "}}" in stripped:
            raise ValueError("unknown or malformed evidence marker")
        _check_price_columns(stripped)
        naked = _BARE_PRICE.search(stripped)
        if naked:
            raise ValueError(f"unbound price {naked.group()!r}; use a price evidence marker")

        def replace(match):
            marker_kind, evidence_id = match.groups()
            record = ledger["evidence"].get(evidence_id)
            if record is None:
                raise ValueError(f"unknown evidence marker: {evidence_id}")
            self._validate_record(record, ledger)
            referenced.append(evidence_id)
            label = (f"〔{evidence_id}；核验 {record['verified_at']}；"
                     f"{record['verification_status']}〕")
            if marker_kind == "evidence":
                if record["kind"] == "price":
                    raise ValueError("price evidence requires a price marker")
                return label
            if record["kind"] != "price":
                raise ValueError("price marker requires price evidence")
            checks.append({key: record.get(key) for key in (
                "evidence_id", "unit_amount", "currency", "quantity", "total_amount",
                "quote", "matched_quote_number", "source_id")})
            return (f"{record['currency']} {record['unit_amount']}／人 × "
                    f"{record['quantity']}人 = {record['currency']} "
                    f"{record.get('total_amount', '待核验')} {label}")

        return _MARKER.sub(replace, markdown), checks, list(dict.fromkeys(referenced))

    def _validate_record(self, record, ledger):
        source = ledger["sources"].get(record["source_id"])
        if (not source or _hash(source["content"]) != record["source_content_hash"]
                or record["quote"] not in source["content"]):
            raise ValueError("evidence/source integrity mismatch")
        if record["kind"] == "price":
            fields = self._price_fields(record["quote"], record["unit_amount"],
                                        record["currency"], record["quantity"])
            if any(record.get(key) != value for key, value in fields.items()
                   if key != "total_amount" or record.get(key) is not None):
                raise ValueError("price evidence accounting mismatch")

    @staticmethod
    def _receipt_markdown(receipt, can_undo):
        labels = {"changes": "改了什么", "judgment": "一句判断", "alternatives": "备选",
                  "next_action": "下一步"}
        lines = ["## 修订回执"]
        for key in _RECEIPT_FIELDS:
            value = receipt.get(key)
            if isinstance(value, list) and value and all(isinstance(x, str) and x.strip()
                                                       for x in value):
                value = "；".join(value)
            _text(value, key)
            lines.append(f"- {labels[key]}：{value}")
        lines.append("- 可撤销：" + ("是，可恢复上一有效版本" if can_undo else "初始版本"))
        return "\n".join(lines)

    def _load_version(self, number, manifest=None):
        manifest = manifest or self._read_json("versions.json")
        entry = next((item for item in manifest["versions"] if item["version"] == number), None)
        if entry is None:
            raise ValueError("unknown document version")
        raw = (self.directory / f"version-{number:04d}.json").read_text(encoding="utf-8")
        if _hash(raw) != entry["snapshot_hash"]:
            raise ValueError("version snapshot hash mismatch")
        snapshot = json.loads(raw)
        if _hash(snapshot["markdown"]) != snapshot["hash"]:
            raise ValueError("document hash mismatch")
        for path_key, hash_key in (("source_path", "hash"),
                                   ("markdown_path", "rendered_markdown_hash"),
                                   ("html_path", "html_hash"), ("receipt_path", "receipt_hash")):
            path = Path(snapshot[path_key])
            if path.parent != self.directory or not path.is_file():
                raise ValueError("version artifact missing or outside document directory")
            if _hash(path.read_text(encoding="utf-8")) != snapshot[hash_key]:
                raise ValueError(f"version artifact hash mismatch: {path_key}")
        return snapshot

    def current(self):
        manifest = self._read_json("versions.json")
        return None if manifest["current"] is None else self._load_version(manifest["current"], manifest)

    def save(self, markdown):
        previous = self.current()
        receipt = dict(changes=["保存完整手册"], judgment="已保存；印出价格已完成台账数字核对，待确认项就地标注",
                       alternatives=["可继续精确编辑文档"], next_action="查看手册，按需要提出调整")
        return self._save(markdown, previous, receipt,
                          previous["version"] if previous else None)

    def _save(self, markdown, previous, receipt, undo_target, *, restored_version=None):
        intake_path = self.directory.parent / "conversation.json"
        intake = (json.loads(intake_path.read_text(encoding="utf-8")).get("intake", {})
                  if intake_path.exists() else {})
        diff = _unified_diff(previous["markdown"] if previous else "", markdown)
        receipt = validate_edit_binding(previous["markdown"] if previous else "", markdown,
                                        receipt, self.evidence, restored_version=restored_version)
        from .single_session_completeness import validate_completion
        validate_completion(markdown, intake)
        _, detail, findings = detail_canonical(markdown)
        if detail is not None:
            self._write_json("detail-check-" + _hash(markdown) + ".json",
                             dict(document_hash=_hash(markdown), phase="before_render",
                                  policy="structured_days_authoritative", findings=findings))
        created_at = _now()
        manifest = self._read_json("versions.json")
        number = max((item["version"] for item in manifest["versions"]), default=0) + 1
        output = render_snapshot(dict(markdown=markdown, receipt=receipt, diff=diff,
                                      version=number, created_at=created_at,
                                      undo_target_version=undo_target),
                                 self._read_json("ledger.json"), intake)
        return self._commit(markdown, output["rendered_markdown"], output["html"],
                            diff, receipt, output["price_checks"], previous, undo_target, created_at=created_at)

    def _commit(self, markdown, rendered, page, diff, receipt, checks, previous, undo_target, *, created_at=None):
        manifest = self._read_json("versions.json")
        if manifest["current"] != (previous["version"] if previous else None):
            raise ValueError("document changed while saving; retry from current version")
        number = max((item["version"] for item in manifest["versions"]), default=0) + 1
        prefix = self.directory / f"document-{number:04d}"
        snapshot = dict(version=number, hash=_hash(markdown), markdown=markdown,
                        rendered_markdown=rendered, diff=diff, receipt=deepcopy(receipt),
                        price_checks=checks, price_check_status="PASS", created_at=created_at or _now(),
                        previous_version=previous["version"] if previous else None,
                        undo_target_version=undo_target)
        receipt_json = _json(dict(receipt=receipt, price_checks=checks, diff=diff,
                                  document_hash=snapshot["hash"], version=number))
        for extension, path_key, hash_key, content in (
            (".source.md", "source_path", "hash", markdown),
            (".md", "markdown_path", "rendered_markdown_hash", rendered),
            (".html", "html_path", "html_hash", page),
            (".receipt.json", "receipt_path", "receipt_hash", receipt_json),
        ):
            path = Path(str(prefix) + extension)
            _atomic_write(path, content)
            snapshot[path_key], snapshot[hash_key] = str(path), _hash(content)
        snapshot_json = _json(snapshot)
        _atomic_write(self.directory / f"version-{number:04d}.json", snapshot_json)
        manifest["versions"].append(dict(version=number, snapshot_hash=_hash(snapshot_json)))
        manifest["current"] = number
        self._write_json("versions.json", manifest)
        return snapshot

    def _base(self, base_hash):
        previous = self.current()
        if previous is None or previous["hash"] != base_hash:
            raise ValueError("stale or missing base_hash; read current document first")
        return previous

    def edit(self, base_hash, edits, receipt):
        previous = self._base(base_hash)
        if not isinstance(receipt, dict):
            raise ValueError("receipt must be an object")
        self._receipt_markdown(receipt, True)
        if not isinstance(edits, list) or not edits:
            raise ValueError("edits must be a nonempty list")
        markdown = previous["markdown"]
        replacements = {}
        for edit in edits:
            if not isinstance(edit, dict):
                raise ValueError("each edit must be an object")
            old, new = edit.get("old"), edit.get("new")
            _text(old, "old")
            if not isinstance(new, str):
                raise ValueError("new must be text")
            if markdown.count(old) != 1:
                raise ValueError("old must match exactly once in the current document")
            old_ids = [m[2] for m in _MARKER.finditer(old) if m[1] == "price"]
            new_ids = [m[2] for m in _MARKER.finditer(new) if m[1] == "price"]
            if (len(old_ids) != len(new_ids) and set(old_ids) - set(new_ids)
                    and set(new_ids) - set(old_ids)):
                raise ValueError("ambiguous price replacement; split into explicit edits")
            if len(old_ids) == len(new_ids) and not is_complete_line_reorder(old, new):
                for old_id, new_id in zip(old_ids, new_ids):
                    if old_id != new_id:
                        if old_id in replacements and replacements[old_id] != new_id:
                            raise ValueError("conflicting price evidence replacements")
                        replacements[old_id] = new_id
            markdown = markdown.replace(old, new, 1)
        if set(replacements) & set(replacements.values()):
            raise ValueError("chained price replacements require separate revisions")
        markdown = _MARKER.sub(
            lambda m: "{{price:" + replacements[m[2]] + "}}"
            if m[1] == "price" and m[2] in replacements else m[0], markdown)
        if markdown == previous["markdown"]:
            raise ValueError("edit must change the document")
        return self._save(markdown, previous, receipt, previous["version"])

    def undo(self, base_hash):
        previous = self._base(base_hash)
        target_number = previous["undo_target_version"]
        if target_number is None:
            raise ValueError("no earlier document version to restore")
        target = self._load_version(target_number)
        receipt = dict(changes=[f"撤销编辑，恢复版本 {target_number}"],
                       judgment="已恢复此前保存的文档内容", alternatives=["可继续编辑恢复后的文档"],
                       next_action="审阅恢复版本")
        return self._save(target["markdown"], previous, receipt, target["undo_target_version"],
                          restored_version=target_number)


_CURRENCY_NAMES = dict(CNY="元", RMB="元", THB="泰铢", USD="美元", EUR="欧元",
                       GBP="英镑", JPY="日元", HKD="港元", SGD="新加坡元",
                       AUD="澳元", CAD="加元", VND="越南盾", KRW="韩元",
                       MYR="马来西亚林吉特", IDR="印尼盾", INR="印度卢比",
                       TWD="新台币", NZD="新西兰元", CHF="瑞士法郎", PHP="菲律宾比索",
                       AED="阿联酋迪拉姆")
_CURRENCY_NAMES.update({"¥": "元", "￥": "元", "฿": "泰铢", "$": "美元",
                        "€": "欧元", "£": "英镑", "₩": "韩元", "₹": "印度卢比", "₫": "越南盾"})


def _travelers(markdown, intake):
    # A revision can explicitly update the party while the original intake stays
    # immutable. Only a labelled total or a title's exact party size overrides it.
    plain = markdown.replace("**", "")
    match = re.search(r"(?m)^\s*(?:-\s*)?(?:出行人|人数|出行人数)[：:]\s*(\d+)\s*人", plain)
    if not match:
        match = re.search(r"(?m)^# [^\n]*[（(](\d+)\s*人[）)]", plain)
    value = match[1] if match else (intake or {}).get("travelers")
    if value is None:
        return None
    count = _decimal(value, "travelers", positive=True)
    if count != count.to_integral_value():
        raise ValueError("travelers must be a whole number")
    return count


def _display_text(markdown, ledger, count):
    # Validate the untouched input first; metadata removal must never bypass the
    # existing naked-price and currency-column accounting guards.
    store = object.__new__(DocumentStore)
    _, checks, ids = store._expand(markdown, ledger)
    display_checks = []

    def replace(match):
        kind, eid = match.groups()
        if kind != "price":
            return f"[^{eid}]"
        record = ledger["evidence"][eid]
        basis_text = record["claim"] + " " + record["quote"]
        basis = ("车" if re.search(r"[/／]车|每车|per (?:car|vehicle)", basis_text, re.I)
                 else "份" if re.search(r"套餐|双人餐|[/／]份|每份|per (?:set|portion)", basis_text, re.I)
                 else "人")
        quantity = _decimal(record["quantity"], "quantity", positive=True)
        fallback = record.get("total_amount") is None
        if fallback:
            if basis == "人" and count is not None:
                quantity = count
            with localcontext() as context:
                context.prec = 220
                total = _decimal(record["unit_amount"], "unit_amount") * quantity
        else:
            total = _decimal(record["total_amount"], "total_amount")
        currency = _CURRENCY_NAMES.get(record["currency"], record["currency"])
        display_checks.append(dict(evidence_id=eid, unit_amount=record["unit_amount"],
                                   quantity=_number_text(quantity), total_amount=_number_text(total),
                                   currency=record["currency"], basis=basis,
                                   ledger_quantity=record["quantity"],
                                   render_basis="legacy_formula" if fallback else "evidence_total",
                                   fallback_reason="missing_total_amount" if fallback else None))
        return (f"{record['unit_amount']} {currency}/{basis} × {_number_text(quantity)}{basis}"
                f" = {_number_text(total)} {currency}[^{eid}]")

    # An existing '/人' after a marker describes the unit, not the group total.
    clean = re.sub(r"(\{\{price:E[1-9]\d*\}\})\s*[/／]人", r"\1", markdown)
    clean = _MARKER.sub(replace, clean)
    clean = _LEGACY_REFERENCE.sub(lambda m: "".join(
        f"[^{eid}]" for eid in re.findall(r"E[1-9]\d*", m[1])), clean)
    clean = re.sub(r"〔[^〕]*〕", "", clean)
    clean = clean.replace("单源未交叉确认", "只有一个来源")
    return clean, checks, display_checks, ids


def _source_name(record, source):
    for key in ("title", "name"):
        if isinstance(source.get(key), str) and source[key].strip():
            return source[key].strip()
    host = (urlsplit(record["url"]).hostname or "").lower()
    names = {"bendibao.com": "本地宝", "cncn.com": "欣欣旅游", "qunar.com": "去哪儿",
             "toutiao.com": "今日头条", "sina.cn": "新浪",
             "thailandtravel.or.jp": "泰国国家旅游局日本站",
             "tourismthailand.org": "泰国国家旅游局", "outthailand.com": "Out Thailand",
             "baanlyy.com": "Baan Lyy"}
    return next((name for domain, name in names.items()
                 if host == domain or host.endswith("." + domain)), host or "来源名称未提供")


def _explicit_resolution(eid, body):
    for line in body.splitlines():
        if f"[^{eid}]" not in line:
            continue
        match = re.search(r"以[^，。；|\n]{1,80}为准", line.replace("**", ""))
        if match:
            return match[0]
    return "采用依据待确认"


def _records_html(ledger, missing_ids, body):
    rows = []
    for eid, record in sorted(ledger["evidence"].items(), key=lambda item: int(item[0][1:])):
        source = ledger["sources"][record["source_id"]]
        # Status is an explicit ledger assertion, never inferred from equal
        # amounts, similar claims, shared domains, or the number of records.
        status = record.get("verification_status", "")
        reliability = {"多源一致": "多源一致", "单源未交叉确认": "只有一个来源"}.get(status, "来源一致性未确认")
        if status == "两源不一致" or "不一致" in record["claim"]:
            # Preserve an explicitly recorded conflict and its own cited line's
            # resolution; do not compare amounts or adjudicate sources here.
            reliability = "两源不一致；" + record.get("resolution", _explicit_resolution(eid, body))
        claim = record["claim"].split("（")[0].split("——")[0]
        claim = re.sub(r"〔[^〕]*〕", "", claim).replace("单源未交叉确认", "只有一个来源")
        date = record.get("verified_at", record["retrieved_at"])[:10]
        label = f"{eid[1:]}. {claim} · {_source_name(record, source)} · 核查 {date} · {reliability}"
        url = html.escape(record["url"], quote=True)
        rows.append(f'<div class="rec" id="rec{eid[1:]}"><div class="record-line">{html.escape(label)}</div>'
                    '<details class="quote"><summary>查看逐字引文与来源</summary>'
                    + '<p>' + html.escape(record["claim"]) + '</p><blockquote>'
                    + html.escape(record["quote"]) + '</blockquote>'
                    + f'<a href="{url}" rel="noreferrer noopener">{url}</a></details></div>')
    for eid in missing_ids:
        rows.append(f'<div class="rec" id="rec{eid[1:]}">{eid[1:]}. 来源记录缺失，相关内容待确认</div>')
    if not rows:
        rows.append('<p class="empty-ledger">本会话暂无核查记录；不代表相关事实已核实。</p>')
    return ('<details class="records"><summary>核查记录</summary>' + "\n".join(rows)
            + '</details>')


# Presentation adapters consume existing prose and destination_asset claims only.
_DAY = re.compile(r'^#{1,6}\s+(?:第\s*[一二三四五六七八九十\d]+\s*天|Day\s*\d+|D\d+)(?:\b|\s|[：:·｜|—-])', re.I)
_TIME = re.compile(r'(?<!\d)(?:[01]?\d|2[0-3])[:：][0-5]\d(?:\s*[–—~-]\s*(?:[01]?\d|2[0-3])[:：][0-5]\d)?')
_FIVE = ('看什么', '怎么走', '停多久', '何时最好', '坑在哪')


def _assets(ledger):
    result = {}
    for record in ledger['evidence'].values():
        try:
            asset = json.loads(record['claim']).get('destination_asset')
        except (ValueError, AttributeError):
            continue
        if isinstance(asset, dict) and isinstance(asset.get('place'), str):
            result.setdefault(asset['place'], []).append(asset)
    return result


def _coordinate(assets):
    points = set()
    for asset in assets:
        c = asset.get('coordinates')
        if not isinstance(c, dict):
            continue
        lat, lon, crs = c.get('latitude'), c.get('longitude'), c.get('crs')
        if (isinstance(lat, (int, float)) and not isinstance(lat, bool)
                and isinstance(lon, (int, float)) and not isinstance(lon, bool)
                and -90 <= lat <= 90 and -180 <= lon <= 180
                and crs in ('WGS84', 'WGS-84', 'GCJ02', 'GCJ-02', 'BD09', 'BD-09')):
            points.add((lat, lon, crs.replace('-', '')))
    return next(iter(points)) if len(points) == 1 else None


def _spot_card(title, lines, assets):
    images = []
    for asset in assets:
        for item in asset.get('images', []) if isinstance(asset.get('images'), list) else []:
            if not isinstance(item, dict):
                continue
            url = item.get('url')
            try:
                safe = isinstance(url, str) and _safe_url(url)
            except ValueError:
                safe = False
            if safe and url not in images:
                images.append(url)
    picture = ''.join('<img loading="lazy" referrerpolicy="no-referrer" src="'
                      + html.escape(url, quote=True) + '" alt="' + html.escape(title, quote=True)
                      + '" onerror="this.hidden=true">' for url in images[:2])
    fields, remaining = {}, []
    for line in lines:
        plain = re.sub(r'^\s*[-*]\s+', '', line).replace('**', '')
        match = re.match(r'^(看什么|怎么走|停多久|何时最好|什么时候最好|坑在哪)[：:]\s*(.*)', plain)
        if match:
            key = '何时最好' if match[1] == '什么时候最好' else match[1]
            fields[key] = fields.get(key, '') + ('；' if key in fields else '') + match[2]
        else:
            remaining.append(line)
    facts = ''.join('<div><dt>' + key + '</dt><dd>' + _inline(fields.get(key) or '正文未单列，待补充')
                    + '</dd></div>' for key in _FIVE)
    return ('<section class="destination-card">' + picture + '<div class="spot-copy"><h3>'
            + _inline(title) + '</h3>' + _markdown_html('\n'.join(remaining))
            + '<dl class="spot-facts">' + facts + '</dl></div></section>')


def _day_events(lines):
    events, columns = [], None
    aliases = ({'时刻', '时间', '时间段'}, {'事项', '活动', '安排'}, {'地点', '站点', '点位'})
    for line in lines:
        plain = line.replace('**', '').strip()
        if plain.startswith('|'):
            cells = [x.strip() for x in plain.strip('|').split('|')]
            indices = [[i for i, cell in enumerate(cells) if cell in names] for names in aliases]
            if all(len(matches) == 1 for matches in indices):
                columns = [matches[0] for matches in indices]
            elif columns and len(cells) > max(columns) and _TIME.fullmatch(cells[columns[0]]):
                time, action, place = [cells[i] for i in columns]
                extras = [cell for i, cell in enumerate(cells) if i not in columns and cell]
                events.append((time, '；'.join([action] + extras), place or '地点见事项'))
        else:
            columns = None
            match = re.match(r'^\s*(?:[-*]|\d+[.)])?\s*(' + _TIME.pattern + r')\s+(.+)', plain)
            if match:
                # Preserve unstructured prose rather than guess a location.
                parts = re.split(r'\s+[｜|@]\s+|[，,]\s*地点[：:]\s*', match[2], maxsplit=1)
                events.append((match[1], parts[0], parts[1] if len(parts) > 1 else '地点见事项'))
    return events


def _segment_duration(action):
    number = r'\d+(?:\.\d+)?(?:[–—~-]\d+(?:\.\d+)?)?'
    duration = number + r'\s*(?:小时(?:\s*' + number + r'\s*分钟)?|分钟)'
    match = re.search(r'(?:乘车|车程|步行|交通|耗时|行驶|公交|地铁|打车|返程)\s*(?:约)?\s*'
                      + duration, action)
    return match[0] if match else '耗时未提供，待确认'


def _bound_segment(start, end, events):
    # Only a complete, standalone route clause binds a segment. Any surrounding
    # prose may negate/qualify it and remains unbound; do not guess semantics.
    prefix = re.compile(r'^(?:从)?' + re.escape(start) + r'\s*(?:到|→)\s*'
                        + re.escape(end) + r'[，,:：\s]*')
    values = set()
    for _, action, _ in events:
        match = prefix.match(action)
        if match:
            tail = action[match.end():].rstrip('。').strip()
            duration = _segment_duration(tail)
            if duration == tail:
                values.add(duration)
    return next(iter(values)) if len(values) == 1 else '耗时待确认'


def _route_html(events, assets):
    stops = [(place, action) for _, action, place in events if place != '地点见事项']
    if not stops:
        return '<p class="route-fallback">文字路线：地点未单列，请按下方正文行程。</p>'
    labels = [place for place, _ in stops]
    route_text = ' → '.join(labels)
    coords = [_coordinate(assets.get(place, [])) for place in labels]
    if len(coords) < 2 or any(c is None for c in coords) or len({c[2] for c in coords}) != 1:
        return '<p class="route-fallback">文字路线：' + _inline(route_text) + '（坐标缺失或不兼容，未绘图）</p>'
    lats, lons = [c[0] for c in coords], [c[1] for c in coords]
    dx, dy = max(lons) - min(lons), max(lats) - min(lats)
    scale = 220 / max(dx, dy) if max(dx, dy) else 1
    points = [(160 + (lon - (max(lons) + min(lons)) / 2) * scale,
               140 - (lat - (max(lats) + min(lats)) / 2) * scale) for lat, lon, _ in coords]
    visits = {}
    for i, point in enumerate(points, 1):
        visits.setdefault(point, []).append(str(i))
    marks = ''.join(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="14"/>'
                    f'<text x="{x:.2f}" y="{y+4:.2f}" text-anchor="middle">'
                    + '/'.join(numbers) + '</text>' for (x, y), numbers in visits.items())
    # Names and segment durations live in a wrapping figure legend;
    # no duration is derived from geographic distance or event time differences.
    legend = ''.join('<p>' + str(i) + '. ' + _inline(place) + (' · 本站事项所载交通：' + _inline(_segment_duration(action)) + '（对应路段见正文）'
                     ) + '</p>' for i, (place, action) in enumerate(stops, 1))
    segments = ''.join('<p>' + str(i) + '→' + str(i+1) + ' ' + _inline(start) + ' → '
                       + _inline(end) + '：' + _inline(_bound_segment(start, end, events)) + '</p>'
                       for i, (start, end) in enumerate(zip(labels, labels[1:]), 1))
    return ('<figure class="daily-route"><figcaption>每日线路 · 示意级精度、非导航图</figcaption>'
            '<svg viewBox="0 0 320 280" role="img" aria-label="' + html.escape(route_text, quote=True)
            + '"><title>' + html.escape(route_text) + '</title><polyline points="'
            + ' '.join(f'{x:.2f},{y:.2f}' for x, y in points) + '"/>' + marks
            + '</svg><div class="segment-times">' + segments + '</div><div class="route-legend">' + legend + '</div></figure>')


def _visual_body(markdown, ledger):
    assets, output, lines, index = _assets(ledger), [], markdown.splitlines(), 0
    while index < len(lines):
        line = lines[index]
        if _DAY.match(line.replace('**', '')):
            end = index + 1
            level = len(line) - len(line.lstrip('#'))
            while end < len(lines):
                heading = re.match(r'^(#{1,6})\s+', lines[end])
                if heading and len(heading[1]) <= level:
                    break
                end += 1
            events = _day_events(lines[index+1:end])
            output.append(_markdown_html(line))
            output.append(_route_html(events, assets))
            rows = ''.join('<li><time>' + _inline(time) + '</time><div>' + _inline(action)
                           + '<span class="event-place">' + _inline(place) + '</span></div></li>'
                           for time, action, place in events)
            output.append('<section class="daily-timeline"><h3>当日时间轴</h3><ol>' + rows
                          + '</ol>' + ('' if rows else '<p>正文未提供可识别时刻，待补充。</p>') + '</section>')
            index += 1
            continue
        heading = re.match(r'^(#{2,6})\s+(.+)', line)
        if heading:
            title = heading[2].replace('**', '').strip()
            end = index + 1
            while end < len(lines) and not re.match(r'^#{1,6}\s+', lines[end]):
                end += 1
            section = lines[index+1:end]
            if title in assets or any(re.search(r'(看什么|停多久|坑在哪)[：:]', s) for s in section):
                output.append(_spot_card(title, section, assets.get(title, [])))
                index = end
                continue
        # Keep whole table blocks intact for the existing Markdown renderer.
        end = index + 1
        while end < len(lines) and not re.match(r'^#{1,6}\s+', lines[end]):
            end += 1
        output.append(_markdown_html('\n'.join(lines[index:end])))
        index = end
    return '\n'.join(output)


_VISUAL_CSS = """
.destination-card{margin:24px 0;border:1px solid #e5e5ea;border-radius:12px;overflow:hidden}
.destination-card img{display:block;width:100%;max-height:320px;object-fit:cover}
.destination-card img[hidden]{display:none}.spot-copy{padding:0 18px 16px}
.spot-facts{margin:12px 0}.spot-facts>div{display:grid;grid-template-columns:5em 1fr;gap:10px;
padding:9px 0;border-top:1px solid #e5e5ea}.spot-facts dt{font-weight:600}.spot-facts dd{margin:0;line-height:1.7}
.daily-route{margin:18px 0;padding:16px;background:#f5f5f7;border-radius:12px}
.daily-route svg{display:block;width:100%;max-width:360px;margin:auto}
.daily-route polyline{fill:none;stroke:#0071e3;stroke-width:2;stroke-dasharray:5 4}
.daily-route circle{fill:#0071e3}.daily-route text{fill:white;font-size:11px}
.route-legend p{margin:5px 0}.daily-timeline ol{list-style:none;padding:0}
.daily-timeline li{display:grid;grid-template-columns:7em 1fr;gap:12px;padding:12px 0;border-bottom:1px solid #e5e5ea}
.daily-timeline time{font-variant-numeric:tabular-nums;font-weight:600}.event-place{display:block;color:#6e6e73}
@media(max-width:600px){.daily-timeline li{grid-template-columns:6em 1fr}.spot-copy{padding:0 12px 12px}}
"""


def render_snapshot(snapshot, ledger, intake=None, *, media_provider=None):
    """Pure offline rendering: returns HTML/Markdown and display accounting.

    Does not instantiate a store or read/write files. Source Markdown, ledger
    quantities, original receipts and their integrity checks remain unchanged.
    """
    store = object.__new__(DocumentStore)
    media_records, degradations = [], []
    for record in ledger["evidence"].values():
        store._validate_record(record, ledger)
        if not _safe_url(record["url"]):
            raise ValueError("source URL must use http or https")
    canonical, detail, findings = detail_canonical(snapshot["markdown"])
    count = _travelers(canonical, intake)
    body, checks, display_checks, ids = _display_text(canonical, ledger, count)
    presentation_checks, presented_texts = [], []
    def display_inline(text):
        if not text.strip():
            return ""
        presented_texts.append(text)
        shown, _, rows, _ = _display_text(text, ledger, count)
        presentation_checks.extend(rows)
        return _inline(reader_text(shown))
    def display_markdown(text):
        if not text.strip():
            return ""
        presented_texts.append(text)
        shown, _, rows, _ = _display_text(text, ledger, count)
        presentation_checks.extend(rows)
        return _markdown_html(reader_markdown(shown))
    if detail is None:
        visual = _visual_body(body, ledger)
        presentation_checks = display_checks
    else:
        prose, _, _ = detail_extract(snapshot["markdown"])
        prose, detail, reference_ids = accounting_view(prose, detail, ledger,
            {row["evidence_id"]: row["total_amount"] for row in display_checks})
        detail = merge_assets(detail, ledger)
        # Raw saved fields cannot supply renderer-owned HTML.
        for day in detail.get('days', []):
            day.pop('_presentation_map_html', None)
        if media_provider is not None:
            try:
                detail, media_records, degradations = media_provider.enrich(detail)
            except Exception as exc:
                degradations.append(dict(stage='media', field_path='presentation.media',
                                         rule='stage_failure_isolation', message=str(exc)))
        visual = presentation_render(prose, detail, display_inline, display_markdown,
                                     detail_render, context_render, render_budget, route_pending)
        visual = cover_html(detail, display_inline) + visual
    if detail is not None:
        # Compaction must never make an otherwise valid bound amount disappear.
        # Keep the complete qualified source line, not a detached price marker.
        def source_strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from source_strings(item)
            elif isinstance(value, list):
                for item in value:
                    yield from source_strings(item)
        supplements = []
        supplement_data = {key: value for key, value in detail.items()
                           if key not in ('presentation_replacements', 'price_applicability')}
        for text in dict.fromkeys(source_strings(supplement_data)):
            if (any(match[1] == "price" for match in _MARKER.finditer(text))
                    and not any(match[2] in reference_ids for match in _MARKER.finditer(text))
                    and not any(text in shown for shown in presented_texts)):
                supplements.append('<p>' + display_inline(text) + '</p>')
        if supplements:
            marker = '<section class="trip-corrections">'
            addition = '<div class="qualified-price-details"><h3>价格适用条件补充</h3>' + ''.join(supplements) + '</div>'
            boundary = '</section>\n' + marker
            visual = visual.replace(boundary, addition + boundary, 1)
    receipt = store._receipt_markdown(snapshot["receipt"], snapshot.get("undo_target_version") is not None)
    receipt, receipt_checks, receipt_display, receipt_ids = _display_text(receipt, ledger, count)
    body_prices = {row["evidence_id"]: row for row in display_checks}
    for row in receipt_display:
        if row["evidence_id"] not in body_prices or row != body_prices[row["evidence_id"]]:
            raise ValueError("receipt price is not present in the rendered handbook")
    # Verify the actual rendered body before claiming that a displayed price was checked.
    for eid, row in body_prices.items():
        currency = _CURRENCY_NAMES.get(row["currency"], row["currency"])
        printed = (f"{row['unit_amount']} {currency}/{row['basis']} × "
                   f"{row['quantity']}{row['basis']} = {row['total_amount']} {currency}")
        expected = _inline(printed + f"[^{eid}]")
        occurrences = sum(item["evidence_id"] == eid for item in presentation_checks)
        if occurrences == 0 or visual.count(expected) != occurrences:
            raise ValueError("price missing from final rendered handbook")
    if body_prices:
        receipt += "\n- 印出价格核对：" + "；".join(
            f"{eid} 合计 {row['total_amount']} {_CURRENCY_NAMES.get(row['currency'], row['currency'])}"
            for eid, row in body_prices.items())
    missing = [eid for eid in dict.fromkeys(ids + receipt_ids) if eid not in ledger["evidence"]]
    records = _records_html(ledger, missing, body)
    from .single_session_presentation_copy import concentrate, media_records_html
    try:
        visual = concentrate(visual)
    except Exception as exc:
        degradations.append(dict(stage='copy', field_path='presentation.html',
                                 rule='stage_failure_isolation', message=str(exc)))
    if not ledger['evidence']:
        degradations.append(dict(stage='ledger', field_path='ledger.evidence',
                                 rule='empty_ledger_explicit', message='本会话暂无核查记录'))
    records = records[:-len('</details>')] + media_records_html(media_records, degradations) + '</details>'
    if detail is not None:
        archive = ('<details class="presentation-source"><summary>完整生成字段与原稿</summary><pre>'
                   + html.escape(snapshot["markdown"]) + '</pre></details>')
        records = records[:-len('</details>')] + archive + '</details>'
    rendered = "\n\n".join((body, records))
    styles = _CSS + _VISUAL_CSS + """
:root{color:#1d1d1f;background:#fff;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
*{box-sizing:border-box}main{max-width:800px;margin:0 auto;padding:28px 20px 56px;border:0;border-radius:0}
h1{font-size:34px;line-height:1.25;letter-spacing:-.02em}h2{font-size:25px;margin:34px 0 10px;padding:0 0 6px;
border-top:0;border-bottom:2px solid #0071E3}h3{font-size:19px;margin:24px 0 10px}h4{font-size:17px;margin:0 0 8px}
.trip-decision{margin-bottom:40px}.trip-decision strong{font-weight:700}
.trip-todo{padding:18px 22px;background:#f5f8fc;border-radius:12px}
.handbook>section{margin-bottom:42px}.day-footer{border-top:1px solid #ddd;padding-top:12px}
.presentation-source pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}
.day-theme{font-size:18px;font-weight:600}.itinerary-day{margin:42px 0}
.day-stay{border-top:1px solid #ddd;padding-top:12px}.day-date{color:#6e6e73}
p,li,td{font-size:15px;line-height:1.75}a,.quote summary{color:#0071E3}
th,td{border:0;border-bottom:1px solid #e5e5ea;padding:8px 10px;vertical-align:top}
th{background:#fff;color:#6e6e73}sup a{text-decoration:none;font-size:11px}
blockquote{background:#f5f5f7;border-color:#c7c7cc;color:#333}
details.records{margin-top:36px;border:1px solid #e5e5ea;border-radius:12px;padding:6px 18px 12px}
.budget-tier{margin:12px 0;padding:12px 16px;border:1px solid #e5e5ea;border-radius:12px;overflow-wrap:anywhere}
.budget-tier[open]{background:#f5f8fc;border-color:#cfe6ff}.budget-tier summary{line-height:1.75;color:#1d1d1f}
.destination-overview p{overflow-wrap:anywhere}
.handbook-cover{margin:0 -20px 32px}.handbook-cover img{display:block;width:100%;height:360px;object-fit:cover}
.handbook-cover img[hidden]{display:none}.handbook-cover figcaption{padding:8px 20px;color:#6e6e73;font-size:12px}
.destination-card figure{margin:0}.destination-card figcaption{padding:8px 18px;color:#6e6e73;font-size:12px}
.destination-video{position:relative;border-top:1px solid #e5e5ea}.destination-video span{display:block;padding:10px 18px;font-weight:600}
.records>summary{font-weight:600;padding:10px 0 4px}.rec{border-top:1px solid #e5e5ea;padding:10px 0}
.record-line{font-size:14px;line-height:1.75}.quote{margin-top:4px}
.quote summary{font-size:13px}.quote blockquote{white-space:pre-wrap;font-size:13.5px}
.quote a{font-size:12.5px}.receipt{background:#eef6ff;border:1px solid #cfe6ff;border-radius:12px;padding:4px 18px}
@media(max-width:600px){main{padding:28px 20px 56px}h1{font-size:26px}h2{font-size:19px}.handbook-cover img{height:240px}}
"""
    cover_metadata = ''
    if snapshot.get('version') is not None:
        dates = [day.get('date') for day in (detail or {}).get('days', []) if day.get('date')]
        metadata = '版本 v' + str(snapshot['version']) + ' · 生成/修订日期 ' + str(snapshot.get('created_at', '未记录'))[:10]
        if dates:
            metadata += ' · 行程日期 ' + min(dates) + ' 至 ' + max(dates)
        cover_metadata = '<p class="document-version">' + html.escape(metadata) + '</p>'
    page = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>旅行手册</title><style>' + styles + '</style></head><body><main>'
            + '<article class="handbook">' + cover_metadata + visual + '</article>'
            + records
            + '</main><script>document.addEventListener("click",function(e){'
            'var a=e.target.closest("a");if(!a||!a.getAttribute("href").startsWith("#rec"))return;'
            'var r=document.querySelector(a.getAttribute("href"));if(r){'
            'r.closest("details.records").open=true;r.scrollIntoView();}});</script></body></html>')
    return dict(html=page, rendered_markdown=rendered, body_markdown=body,
                price_checks=[dict(check, **display) for check, display in
                              zip(checks + receipt_checks, display_checks + receipt_display)],
                display_price_checks=display_checks + receipt_display,
                presentation_receipt=dict(status='degraded' if degradations else 'ready',
                                          media_records=media_records, degradations=degradations),
                travelers=_number_text(count) if count is not None else None)
