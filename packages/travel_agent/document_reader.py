"""Bounded extraction for user-selected travel files.

Files are evidence containers, never instructions or supplier confirmations. Raw
bytes are discarded after extraction; the durable ledger stores a digest, label,
bounded text and extraction receipt.
"""
from __future__ import annotations

import base64
import binascii
from datetime import date, datetime, time
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

from .store import StateError

MAX_DOCUMENTS = 5
MAX_RAW_BYTES = 2_500_000
MAX_ARCHIVE_RAW_BYTES = 8_000_000
MAX_DOCUMENT_CHARS = 40_000
MAX_TOTAL_CHARS = 80_000
MAX_XLSX_ROWS = 500
MAX_XLSX_COLUMNS = 80
MAX_PDF_PAGES = 50
MAX_ARCHIVE_MEMBERS = 2_000
MAX_ARCHIVE_EXPANDED_BYTES = 50_000_000
MAX_CHAT_FILES = 100

_EXTENSIONS = {".xlsx", ".pdf", ".eml", ".txt", ".md", ".csv", ".json", ".zip"}
_MIME = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pdf": "application/pdf",
    ".eml": "message/rfc822",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".zip": "application/zip",
}

_TRAVEL_TERMS = re.compile(
    r"旅行|旅游|行程|攻略|机票|航班|机场|酒店|民宿|住宿|签证|景点|门票|"
    r"租车|自驾|高铁|火车|地铁|餐厅|拍照|出发|度假|出境|周边游|亲子游|"
    r"\b(?:travel|trip|itinerary|flight|airport|hotel|hostel|airbnb|booking|visa|"
    r"vacation|holiday|rental car|train|metro|restaurant|sightseeing)\b", re.I)


class _VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def _decode_data_url(value, max_bytes=MAX_RAW_BYTES):
    if not isinstance(value, str) or len(value) > max_bytes * 2:
        raise StateError("BAD_DOCUMENT", "文件编码无效或过大")
    match = re.fullmatch(r"data:([^;,]+);base64,([A-Za-z0-9+/=]+)", value)
    if not match:
        raise StateError("BAD_DOCUMENT", "文件必须由本地选择，不接受远程文件链接")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except binascii.Error:
        raise StateError("BAD_DOCUMENT", "文件编码无效") from None
    if not raw or len(raw) > max_bytes:
        limit = "8MB" if max_bytes == MAX_ARCHIVE_RAW_BYTES else "2.5MB"
        raise StateError("BAD_DOCUMENT", f"单个文件请小于 {limit}")
    return match.group(1).lower(), raw


def _text(raw):
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            value = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise StateError("BAD_DOCUMENT", "文本文件编码无法识别")
    value = value.replace("\x00", "")
    return value[:MAX_DOCUMENT_CHARS], f"已读取 {len(value):,} 个字符"


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return re.sub(r"[\t\r\n]+", " ", str(value)).strip()


def _check_xlsx_archive(raw):
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        raise StateError("BAD_DOCUMENT", "XLSX 文件结构无效")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        expanded = sum(item.file_size for item in members)
        if len(members) > 2_000 or expanded > 40_000_000:
            raise StateError("BAD_DOCUMENT", "XLSX 解压后过大，请只保留行程相关工作表")
        names = {item.filename for item in members}
        if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
            raise StateError("BAD_DOCUMENT", "XLSX 缺少工作簿结构")


def _xlsx(raw):
    _check_xlsx_archive(raw)
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise StateError("DOCUMENT_READER_UNAVAILABLE", "工作簿解析器尚未安装", 503) from None
    try:
        book = load_workbook(io.BytesIO(raw), read_only=False, data_only=False)
    except Exception:
        raise StateError("BAD_DOCUMENT", "无法读取这个 XLSX 工作簿") from None
    output, total_rows = [], 0
    for sheet in book.worksheets:
        matrix = []
        for row in sheet.iter_rows(max_row=MAX_XLSX_ROWS, max_col=MAX_XLSX_COLUMNS, values_only=True):
            values = [_cell(value) for value in row]
            while values and not values[-1]:
                values.pop()
            if any(values):
                matrix.append(values)
            if sum(sum(len(cell) for cell in line) for line in matrix) >= MAX_DOCUMENT_CHARS:
                break
        if matrix:
            lines = ["\t".join(row) for row in matrix]
            headers = matrix[0]
            labeled = []
            for row_number, row in enumerate(matrix[1:], 2):
                row_label = row[0] if row and row[0] else f"第{row_number}行"
                for column in range(1, len(row)):
                    if not row[column]:
                        continue
                    column_label = headers[column] if column < len(headers) and headers[column] else f"第{column + 1}列"
                    labeled.append(f"{row_label} | {column_label} = {row[column]}")
            content = f"工作表：{sheet.title}\n" + "\n".join(lines)
            if labeled:
                content += "\n\n带行列语义的单元格：\n" + "\n".join(labeled)
            output.append(content)
            total_rows += len(matrix)
        if sum(len(part) for part in output) >= MAX_DOCUMENT_CHARS:
            break
    content = "\n\n".join(output)[:MAX_DOCUMENT_CHARS]
    if not content.strip():
        raise StateError("BAD_DOCUMENT", "XLSX 中没有可读取的单元格内容")
    return content, f"已读取 {len(output)} 张工作表、{total_rows} 行非空内容"


def _pdf(raw):
    if not raw.startswith(b"%PDF-"):
        raise StateError("BAD_DOCUMENT", "PDF 文件结构无效")
    try:
        from pypdf import PdfReader
    except ImportError:
        raise StateError("DOCUMENT_READER_UNAVAILABLE", "PDF 解析器尚未安装", 503) from None
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            raise StateError("BAD_DOCUMENT", "暂不支持加密 PDF，请先导出无密码版")
        pages = []
        for page in reader.pages[:MAX_PDF_PAGES]:
            pages.append((page.extract_text() or "").strip())
            if sum(len(value) for value in pages) >= MAX_DOCUMENT_CHARS:
                break
    except StateError:
        raise
    except Exception:
        raise StateError("BAD_DOCUMENT", "无法读取这个 PDF") from None
    content = "\n\n".join(value for value in pages if value)[:MAX_DOCUMENT_CHARS]
    if not content:
        raise StateError("BAD_DOCUMENT", "这个 PDF 没有可提取文字，可改为上传关键页截图")
    return content, f"已读取 {len(pages)} 页 PDF 文字"


def _email(raw):
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception:
        raise StateError("BAD_DOCUMENT", "无法读取这封邮件文件") from None
    headers = [f"{name}: {message.get(name)}" for name in ("Subject", "From", "To", "Date") if message.get(name)]
    plain, html, attachments = [], [], []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        filename = part.get_filename()
        if filename:
            attachments.append(filename)
            continue
        if part.get_content_maintype() != "text":
            continue
        try:
            value = part.get_content()
        except Exception:
            continue
        if not isinstance(value, str):
            continue
        if part.get_content_subtype() == "plain":
            plain.append(value)
        elif part.get_content_subtype() == "html":
            parser = _VisibleHTML()
            parser.feed(value)
            html.append("\n".join(parser.parts))
    body = "\n".join(plain or html)
    content = ("\n".join(headers) + "\n\n" + body).strip()[:MAX_DOCUMENT_CHARS]
    if not content:
        raise StateError("BAD_DOCUMENT", "邮件中没有可读取的正文")
    note = "已读取邮件标题与正文"
    if attachments:
        note += "；附件未自动展开：" + "、".join(attachments[:5])
    return content, note


def _message_text(message):
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    values = []
    for part in parts:
        if isinstance(part, str):
            values.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            values.append(part["text"])
    return "\n".join(values).strip()


def _chatgpt_conversations(value):
    conversations = value if isinstance(value, list) else [value]
    parsed = []
    for conversation in conversations:
        if not isinstance(conversation, dict) or not isinstance(conversation.get("mapping"), dict):
            continue
        messages = []
        for order, node in enumerate(conversation["mapping"].values()):
            message = node.get("message") if isinstance(node, dict) else None
            text = _message_text(message)
            role = ((message or {}).get("author") or {}).get("role") if isinstance(message, dict) else None
            if text and role in {"user", "assistant"}:
                created = message.get("create_time")
                messages.append((created if isinstance(created, (int, float)) else float("inf"), order,
                                 "用户" if role == "user" else "助手", text))
        messages.sort(key=lambda item: (item[0], item[1]))
        if messages:
            title = conversation.get("title") if isinstance(conversation.get("title"), str) else "未命名对话"
            body = "\n\n".join(f"{role}：{text}" for _, _, role, text in messages)
            parsed.append((title.strip() or "未命名对话", body))
    return parsed


def _generic_conversations(value):
    candidates = value if isinstance(value, list) else value.get("conversations", []) if isinstance(value, dict) else []
    parsed = []
    for index, conversation in enumerate(candidates):
        if not isinstance(conversation, dict):
            continue
        messages = conversation.get("messages") or conversation.get("conversation") or conversation.get("turns")
        if not isinstance(messages, list):
            continue
        lines = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role") or message.get("author") or message.get("sender")
            text = message.get("text") or message.get("content") or message.get("message")
            if isinstance(text, dict):
                text = _message_text({"content": text})
            if not isinstance(text, str) or not text.strip():
                continue
            label = "用户" if str(role).lower() in {"user", "human", "me"} else "助手"
            lines.append(f"{label}：{text.strip()}")
        if lines:
            title = conversation.get("title") or conversation.get("name") or f"对话 {index + 1}"
            parsed.append((str(title).strip(), "\n\n".join(lines)))
    return parsed


def _render_conversations(conversations):
    if not conversations:
        raise StateError("BAD_DOCUMENT", "JSON 中没有识别到可读取的对话文字")
    matched = [item for item in conversations if _TRAVEL_TERMS.search(item[0] + "\n" + item[1])]
    selected = matched or conversations
    parts = []
    for title, body in selected:
        block = f"对话：{title}\n{body}"
        if sum(len(part) for part in parts) + len(block) > MAX_DOCUMENT_CHARS:
            break
        parts.append(block)
    if not parts:
        raise StateError("BAD_DOCUMENT", "对话导出内容过长，请先选择与旅行相关的对话")
    suffix = f"，其中 {len(matched)} 个命中旅行线索" if matched else "；未检出明确旅行词，保留导出顺序"
    return "\n\n---\n\n".join(parts), f"已读取 {len(parts)}/{len(conversations)} 个对话{suffix}"


def _json_document(raw):
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise StateError("BAD_DOCUMENT", "JSON 文件编码或结构无法识别") from None
    conversations = _chatgpt_conversations(value)
    platform = "chatgpt" if conversations else "generic_ai_chat"
    if not conversations:
        conversations = _generic_conversations(value)
    content, message = _render_conversations(conversations)
    return content, message, platform


def _zip_document(raw):
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        raise StateError("BAD_DOCUMENT", "ZIP 文件结构无效")
    parts, readable = [], 0
    platform = "generic_ai_chat"
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) > MAX_ARCHIVE_MEMBERS or sum(item.file_size for item in members) > MAX_ARCHIVE_EXPANDED_BYTES:
            raise StateError("BAD_DOCUMENT", "ZIP 解压后过大，请只保留旅行相关对话")
        preferred = sorted(members, key=lambda item: (Path(item.filename).name != "conversations.json", item.filename))
        for member in preferred:
            extension = Path(member.filename).suffix.lower()
            if extension not in {".json", ".md", ".txt"} or member.file_size > MAX_RAW_BYTES:
                continue
            data = archive.read(member)
            if extension == ".json":
                try:
                    content, _, detected = _json_document(data)
                    platform = detected if detected == "chatgpt" else platform
                except StateError:
                    continue
            else:
                content, _ = _text(data)
            if not content.strip():
                continue
            block = f"文件：{member.filename}\n{content}"
            if sum(len(part) for part in parts) + len(block) > MAX_DOCUMENT_CHARS:
                break
            parts.append(block)
            readable += 1
            if readable >= MAX_CHAT_FILES:
                break
    if not parts:
        raise StateError("BAD_DOCUMENT", "ZIP 中没有可读取的 JSON、Markdown 或文本对话")
    return "\n\n---\n\n".join(parts), f"已读取 ZIP 中 {readable} 个可读对话文件", platform


def extract_documents(items):
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > MAX_DOCUMENTS:
        raise StateError("BAD_DOCUMENT", f"每次最多添加 {MAX_DOCUMENTS} 个文件")
    result, seen, total = [], set(), 0
    for item in items:
        if not isinstance(item, dict):
            raise StateError("BAD_DOCUMENT", "文件格式不正确")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 180:
            raise StateError("BAD_DOCUMENT", "文件名无效")
        name = Path(name).name
        extension = Path(name).suffix.lower()
        if extension not in _EXTENSIONS:
            raise StateError("BAD_DOCUMENT", "仅支持 XLSX、PDF、EML、TXT、MD、CSV、JSON 和 ZIP 文件")
        max_bytes = MAX_ARCHIVE_RAW_BYTES if extension == ".zip" else MAX_RAW_BYTES
        supplied_mime, raw = _decode_data_url(item.get("data_url"), max_bytes=max_bytes)
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
            raise StateError("BAD_DOCUMENT", "同一文件不需重复添加")
        seen.add(digest)
        if extension == ".xlsx":
            content, message = _xlsx(raw)
        elif extension == ".pdf":
            content, message = _pdf(raw)
        elif extension == ".eml":
            content, message = _email(raw)
        elif extension in {".txt", ".md", ".csv"}:
            content, message = _text(raw)
        elif extension == ".json":
            content, message, platform = _json_document(raw)
        else:
            content, message, platform = _zip_document(raw)
        total += len(content)
        if total > MAX_TOTAL_CHARS:
            raise StateError("BAD_DOCUMENT", "文件可读内容过多，请只保留本次旅行相关文件")
        record = {"id": "document-" + digest[:16], "kind": "document_extracted",
                       "label": "已读文件：" + name, "name": name,
                       "mime_type": _MIME[extension], "supplied_mime": supplied_mime,
                       "sha256": digest, "status": "read", "content": content,
                       "message": message}
        if extension in {".json", ".zip"}:
            record.update({"content_type": "ai_conversation_history", "platform": platform})
        result.append(record)
    return result
