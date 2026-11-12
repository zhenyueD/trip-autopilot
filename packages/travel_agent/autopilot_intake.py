"""Reuse Studio's bounded document/link intake in the durable agent entry."""
from .intake import add_sources, material_receipts
from .store import StateError

MATERIAL_FIELDS = {'evidence_text', 'documents', 'links', 'images'}


def parse_materials(body):
    # Import lazily: api routes import AutopilotService during normal startup.
    from .api import bounded_text, parse_links
    from .document_reader import extract_documents
    if body.get('images'):
        raise StateError('MAINCORE_IMAGE_UNAVAILABLE',
            '旅行助手暂未接通图片识别。图片与文字草稿已保留，请先粘贴图片中的文字；本次未提交。', 503)
    note = bounded_text(body, 'evidence_text', 20000)
    documents = extract_documents(body.get('documents'))
    links = parse_links(body, [note, body.get('brief', ''), body.get('request', '')])
    return {'note': note, 'documents': documents, 'links': links}


def append_materials(trip, body, key):
    parsed = parse_materials(body)
    before = {source['id'] for source in trip.get('material_sources', [])}
    sources = add_sources(trip.get('material_sources', []), revision='event-' + key[:20],
                          **parsed)
    trip['material_sources'] = sources
    return material_receipts([source for source in sources if source['id'] not in before])


def check_revision(trip, body):
    expected = body.get('expected_revision')
    if expected is not None and (type(expected) is not int or expected != trip['revision']):
        raise StateError('VERSION_CONFLICT', '方案已变化，文字与材料尚未提交，请先查看最新版本。', 409)
