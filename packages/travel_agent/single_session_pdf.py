"""Offline A4 printing with fail-closed image verification; also used by host CLI."""
from __future__ import annotations

import hashlib
import http.client
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess


class PDFError(RuntimeError):
    """A document must not be advertised as PDF-ready after this failure."""


class _Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.count = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            self.count += 1


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _poppler(name, path):
    executable = shutil.which(name)
    if not executable:
        raise PDFError(f'{name} is required for PDF verification')
    return subprocess.run([executable, *path], check=True, capture_output=True,
                          text=True, timeout=60).stdout


def verify_pdf(path, expected_images):
    """Count actual rendered image occurrences, excluding alpha masks."""
    if not Path(path).read_bytes().startswith(b'%PDF-'):
        raise PDFError('invalid PDF signature')
    listing = _poppler('pdfimages', ['-list', str(path)])
    images = sum(len(parts := line.split()) > 2 and parts[0].isdigit()
                 and parts[2] == 'image' for line in listing.splitlines())
    if images != expected_images:
        raise PDFError(f'embedded image mismatch: HTML={expected_images}, PDF={images}')
    info = _poppler('pdfinfo', [str(path)])
    pages = next((int(line.split(':', 1)[1]) for line in info.splitlines()
                  if line.startswith('Pages:')), 0)
    if pages < 1:
        raise PDFError('PDF has no pages')
    return {'html_images': expected_images, 'embedded_images': images, 'pages': pages}


PRINT_CSS = '''@page {size:A4; margin:14mm 12mm;}
@media print { html,body {width:auto!important; min-width:0!important;}
img {break-inside:avoid; max-width:100%!important;}
figure, tr {break-inside:avoid;} h1,h2,h3 {break-after:avoid;}
}'''
LOAD_IMAGES = '''async () => {
 const imgs = Array.from(document.images);
 for (const img of imgs) { img.loading='eager'; img.fetchPriority='high'; }
 await Promise.all(imgs.map(async img => {
   await img.decode();
   if (!img.complete || img.naturalWidth === 0) throw Error('image did not load');
 }));
 await document.fonts.ready;
 return imgs.length;
}'''


def _proxy_image_bytes(url):
    """Fetch a presentation image through the process proxy.

    The DNS-pinned transport in _image_bytes cannot reach Wikimedia/OSM hosts
    from this network (timeouts / poisoned answers). Presentation images come
    only from the media allow-list, checked by the caller before this runs.
    """
    import time
    import urllib.error
    from urllib.request import ProxyHandler, Request, build_opener
    request = Request(url, headers={
        'User-Agent': 'TravelHandbookPresentation/1.0',
        'Accept': 'image/*', 'Accept-Encoding': 'identity'})
    last_error = None
    for _attempt in range(2):
        try:
            with build_opener(ProxyHandler()).open(request, timeout=20) as response:
                mime = response.headers.get_content_type()
                body = response.read(8 * 1024 * 1024 + 1)
                if (mime not in {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}
                        or not body or len(body) > 8 * 1024 * 1024):
                    raise PDFError('Image response is not a supported raster image')
                return mime, body
        except PDFError:
            raise
        except (TimeoutError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(2)
    raise PDFError(f'{type(last_error).__name__}: {last_error}') from last_error


def _image_bytes(url):
    """Reuse the public reader's DNS-pinned HTTPS transport, bounded to 8 MiB."""
    from .material_reader import _validate_url, _connect_pinned, _ReadDeadline
    parsed = _validate_url(url)
    deadline = _ReadDeadline()
    connection = http.client.HTTPConnection(parsed.hostname, 443)
    try:
        sock = _connect_pinned(parsed, deadline)
        connection.sock = sock
        connection.request('GET', parsed.path + ('?' + parsed.query if parsed.query else ''),
                           headers={'Accept': 'image/*', 'Accept-Encoding': 'identity',
                                    'Connection': 'close'})
        response = connection.getresponse()
        mime = response.getheader('Content-Type', '').split(';')[0].lower()
        if response.status != 200 or mime not in {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}:
            raise PDFError('Image response is not a supported raster image')
        chunks, size = [], 0
        while size <= 8 * 1024 * 1024:
            if response.length == 0:
                return mime, b''.join(chunks)
            sock.settimeout(deadline.remaining())
            chunk = response.read1(65536)
            if not chunk:
                return mime, b''.join(chunks)
            chunks.append(chunk)
            size += len(chunk)
        raise PDFError('Image exceeds 8 MiB')
    finally:
        connection.close()
        deadline.cancel()


def _print(html, target, *, allow_remote_images=True):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            context = browser.new_context(service_workers='block')
            # Only image GETs can leave this isolated context; no cookies, script, or local files.
            cache = {}
            def route_image(route):
                request = route.request
                try:
                    if not allow_remote_images or request.resource_type != 'image' or request.method != 'GET':
                        raise PDFError('Resource is not an authorized image')
                    if request.url not in cache:
                        from urllib.parse import urlsplit
                        host = urlsplit(request.url).hostname
                        if host in {'upload.wikimedia.org', 'thumb.wikimedia.org',
                                    'tile.openstreetmap.org'}:
                            cache[request.url] = _proxy_image_bytes(request.url)
                        else:
                            cache[request.url] = _image_bytes(request.url)
                    mime, body = cache[request.url]
                    route.fulfill(status=200, content_type=mime, body=body)
                except Exception:
                    route.abort()
            context.route('**/*', route_image)
            page = context.new_page()
            page.set_default_timeout(60000)
            page.set_content(html, wait_until='load')
            page.emulate_media(media='print')
            page.add_style_tag(content=PRINT_CSS)
            loaded = page.evaluate('async () => Promise.race([(' + LOAD_IMAGES + ')(), '
                                   'new Promise((_, reject) => setTimeout(() => reject(Error(\"image/font timeout\")), 60000))])')
            # A closed <details> stays closed in Chromium print no matter what
            # author CSS says (verified: @media print display:block does not
            # expand it). Open the check records for the printed page only;
            # the source archive details stays closed on purpose.
            page.evaluate("document.querySelectorAll('details.records, details.quote')"
                          ".forEach(d => { d.open = true; })")
            page.pdf(path=str(target), format='A4', print_background=True,
                     prefer_css_page_size=False)
            return loaded
        finally:
            browser.close()


def render_pdf(html_path, pdf_path, *, allow_remote_images=True):
    """Commit PDF + hash receipt only after decoded images and Poppler agree.

    No model calls; remote images use bounded DNS-pinned HTTPS unless disabled.
    Failed temporary output never replaces an existing PDF.
    """
    source, target = Path(html_path), Path(pdf_path)
    raw = source.read_bytes()
    parser = _Images()
    parser.feed(raw.decode('utf-8'))
    temporary = target.with_name(target.name + '.tmp-' + secrets.token_hex(6))
    receipt_path = target.with_suffix('.pdf.json')
    receipt_tmp = receipt_path.with_name(receipt_path.name + '.tmp-' + secrets.token_hex(6))
    try:
        loaded = _print(raw.decode('utf-8'), temporary, allow_remote_images=allow_remote_images)
        if loaded != parser.count:
            raise PDFError('HTML image count changed during printing')
        receipt = {**verify_pdf(temporary, parser.count), 'status': 'ready',
                   'html_sha256': digest(raw), 'pdf_sha256': digest(temporary.read_bytes())}
        if source.read_bytes() != raw:
            raise PDFError('HTML changed during printing')
        receipt_tmp.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        os.chmod(temporary, 0o600)
        os.chmod(receipt_tmp, 0o600)
        os.replace(temporary, target)
        os.replace(receipt_tmp, receipt_path)
        return receipt
    except Exception as exc:
        if isinstance(exc, PDFError):
            raise
        raise PDFError(f'{type(exc).__name__}: {exc}') from exc
    finally:
        temporary.unlink(missing_ok=True)
        receipt_tmp.unlink(missing_ok=True)


def pdf_receipt(html_path, pdf_path):
    """Refuse stale, partial, or tampered artifacts, including after restart."""
    source, target = Path(html_path), Path(pdf_path)
    receipt_path = target.with_suffix('.pdf.json')
    if any(p.is_symlink() or not p.is_file() for p in (source, target, receipt_path)):
        raise PDFError('PDF artifacts missing or unsafe')
    receipt = json.loads(receipt_path.read_text())
    if (receipt.get('status') != 'ready' or receipt.get('html_sha256') != digest(source.read_bytes())
            or receipt.get('pdf_sha256') != digest(target.read_bytes())
            or receipt.get('html_images') != receipt.get('embedded_images')):
        raise PDFError('PDF receipt mismatch')
    return receipt
