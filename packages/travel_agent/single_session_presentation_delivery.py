"""Immutable post-save presentation artifacts; recovery is read-only and offline."""
from copy import deepcopy
import hashlib
import json
import os
import re
from pathlib import Path
import tempfile


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')


def _read(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('presentation.artifact: regular_file_required')
    return path.read_bytes()


def _publish(path, raw):
    """Atomic, exclusive publication: an existing version is never replaced."""
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.presentation-', delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _source(directory, version):
    raw = _read(directory / f'version-{version:04d}.json')
    manifest = json.loads(_read(directory / 'versions.json'))
    entry = next(row for row in manifest['versions'] if row['version'] == version)
    snapshot = json.loads(raw)
    if (entry['snapshot_hash'] != _digest(raw) or snapshot['version'] != version
            or snapshot['hash'] != _digest(snapshot['markdown'].encode('utf-8'))):
        raise ValueError('presentation.source: snapshot_hash_mismatch')
    return snapshot, _digest(raw)


def _public(receipt):
    return {key: deepcopy(value) for key, value in receipt.items() if key != 'ledger_snapshot'}


def select_presentation(directory, version):
    """Return a verified sidecar or the original HTML, never fetching or writing."""
    directory = Path(directory)
    base = directory / f'document-{version:04d}.html'
    receipt_path = directory / f'presented-{version:04d}.json'
    if not receipt_path.exists():
        return base, None
    try:
        receipt = json.loads(_read(receipt_path))
        snapshot, source_hash = _source(directory, version)
        if (receipt['version'] != version or receipt['source_snapshot_sha256'] != source_hash
                or receipt['document_hash'] != snapshot['hash']
                or receipt['ledger_sha256'] != _digest(_json(receipt['ledger_snapshot']))):
            raise ValueError('presentation.receipt: source_or_ledger_hash_mismatch')
        if receipt.get('artifact') == 'base':
            return base, _public(receipt)
        path = directory / f'presented-{version:04d}.html'
        if receipt['html_sha256'] != _digest(_read(path)):
            raise ValueError('presentation.html: hash_mismatch')
        return path, _public(receipt)
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return base, {'status': 'degraded', 'version': version, 'media_records': [],
                      'degradations': [{'field_path': 'presentation.artifact', 'rule': 'integrity_check_failed'}]}


def _failure(error):
    """Keep validation identifiers, never raw exception text or private values."""
    field, rule = 'presentation.render', 'stage_failed'
    if isinstance(error, ValueError):
        match = re.match(r'^(?:destination detail: )?(\$[.][A-Za-z0-9_.\[\]/]{1,160}) '
                         r'\[([a-z][a-z0-9_]{0,80})\](?: |$)', str(error))
        internal = re.fullmatch(r'(presentation[.][a-z_.]+): ([a-z_]+)', str(error))
        if match or internal:
            field, rule = (match or internal).groups()
    return {'field_path': field, 'rule': rule, 'error_type': type(error).__name__}


def present_saved(directory, snapshot, intake=None, *, media_provider=None):
    """Only call after document and completion gates have succeeded."""
    from .single_session_document import render_snapshot
    directory = Path(directory)
    version = snapshot['version']
    receipt_path = directory / f'presented-{version:04d}.json'
    if receipt_path.exists():
        return select_presentation(directory, version)
    base = directory / f'document-{version:04d}.html'
    receipt = {'status': 'degraded', 'version': version, 'artifact': 'base',
               'media_records': [], 'degradations': []}
    try:
        saved, source_hash = _source(directory, version)
        if saved != snapshot:
            raise ValueError('presentation.source: snapshot_identity_required')
        ledger = json.loads(_read(directory / 'ledger.json'))
        receipt.update(source_snapshot_sha256=source_hash, document_hash=snapshot['hash'],
                       ledger_sha256=_digest(_json(ledger)), ledger_snapshot=ledger)
        output = render_snapshot(deepcopy(snapshot), deepcopy(ledger), deepcopy(intake),
                                 media_provider=media_provider)
        report = output['presentation_receipt']
        receipt.update({key: deepcopy(report[key]) for key in ('status', 'degradations', 'media_records')})
        raw = output['html'].encode('utf-8')
        _publish(directory / f'presented-{version:04d}.html', raw)
        receipt.update(artifact='presented', html_sha256=_digest(raw))
    except Exception as exc:
        receipt.update(status='degraded', artifact='base', media_records=[],
                       degradations=[_failure(exc)])
    try:
        _publish(receipt_path, _json(receipt))
    except OSError:
        receipt.update(status='degraded', artifact='base')
        receipt['degradations'].append({'field_path': 'presentation.receipt', 'rule': 'persist_failed'})
        return base, _public(receipt)
    return select_presentation(directory, version)
