"""Bounded input parsing and capability-authorized local preview API."""
import base64
import binascii
import re
import threading
import os

from .model import capabilities
from .material_reader import extract_links
from .document_reader import extract_documents
from .core_runtime import CoreStudioService
from .store import StateError
from .place_media import PlaceMedia, maps_config, media_manifest, select_entities

_service = None
_service_lock = threading.Lock()


def service():
    global _service
    with _service_lock:
        if _service is None:
            _service = CoreStudioService(content_review='revisions',
                phase1_enabled=os.environ.get('TRAVEL_PHASE1_ENABLED') == '1')
    return _service


def bounded_text(body, key, limit, required=False):
    value = body.get(key, '')
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise StateError('BAD_INPUT', f'{key}需要有效文字，最多{limit}字')
    return value.strip()


def parse_images(body):
    images = body.get('images') or []
    if not isinstance(images, list) or len(images) > 3:
        raise StateError('BAD_IMAGE', '最多分享3张图片')
    if images and not capabilities()['vision_available']:
        raise StateError('VISION_UNAVAILABLE', '视觉模型尚不可用，请先粘贴订单文字', 503)
    cleaned = []
    for item in images:
        if not isinstance(item, dict):
            raise StateError('BAD_IMAGE', '图片格式不正确')
        name = bounded_text(item, 'name', 150, required=True)
        data = item.get('data_url', '')
        if not isinstance(data, str) or len(data) > 1_000_000:
            raise StateError('BAD_IMAGE', '图片过大，请压缩后重试')
        match = re.fullmatch(r'data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)', data)
        if not match:
            raise StateError('BAD_IMAGE', '仅支持JPEG、PNG、WebP图片，不接受远程图片链接')
        try:
            raw = base64.b64decode(match.group(2), validate=True)
        except binascii.Error:
            raise StateError('BAD_IMAGE', '图片编码无效') from None
        if len(raw) > 700_000 or not _image_magic(match.group(1), raw):
            raise StateError('BAD_IMAGE', '图片格式或大小不符合要求')
        cleaned.append({'name': name, 'data_url': data})
    return cleaned


def _image_magic(kind, raw):
    return ((kind == 'png' and raw.startswith(b'\x89PNG\r\n\x1a\n')) or
            (kind == 'jpeg' and raw.startswith(b'\xff\xd8\xff')) or
            (kind == 'webp' and raw.startswith(b'RIFF') and raw[8:12] == b'WEBP'))


def handle(method, path, body=None, token=''):
    if path.startswith('/api/agent/autopilot/'):
        from .autopilot_api import handle as autopilot_handle
        return autopilot_handle(method, path, body, token)
    if method == 'GET' and path == '/api/agent/capabilities':
        return capabilities(), 200
    if method == 'GET' and path == '/api/agent/maps-config':
        return maps_config(), 200
    media = re.fullmatch(r'/api/agent/trips/([a-f0-9]{32})/media', path)
    if media and method in ('GET', 'POST'):
        # Authorization precedes input/ID validation and all public-provider work.
        trip = service().store.get(media[1], token)
        manifest = media_manifest(trip)
        if method == 'GET':
            return manifest, 200
        selected = select_entities(manifest, body)
        return PlaceMedia().enrich(trip, selected), 200
    if body is not None and not isinstance(body, dict):
        raise StateError('BAD_INPUT', '请求体必须为JSON对象')
    body = body or {}
    uncertainty = re.fullmatch(r'/api/agent/trips/([a-f0-9]{32})/uncertainty(?:/(availability))?', path)
    if uncertainty:
        studio = service()
        studio.store.get(uncertainty[1], token)
        if not getattr(studio, 'phase1_enabled', False):
            raise StateError('NOT_FOUND', '本服务尚未开启校准核验试点', 404)
        from .phase1_service import handle_phase1
        return handle_phase1(studio, method, uncertainty[1], uncertainty[2], body, token)
    if method == 'POST' and path == '/api/agent/trips':
        if not capabilities()['planner_available']:
            raise StateError('MODEL_UNAVAILABLE', '规划模型未配置，不能生成新方案', 503)
        brief = bounded_text(body, 'brief', 6000)
        material = bounded_text(body, 'evidence_text', 20000)
        images = parse_images(body)
        documents = extract_documents(body.get('documents'))
        links = parse_links(body, [brief, material])
        if not (brief or material or images or links or documents):
            raise StateError('BAD_INPUT', '分享一句想法、一张照片、订单文字或公开链接就可以开始')
        return service().start(brief, material, images, links, documents), 202
    match = re.fullmatch(r'/api/agent/trips/([a-f0-9]{32})(?:/(message|profile|revise|undo|retry|prepare|actions/report))?', path)
    if not match:
        raise StateError('NOT_FOUND', '旅行接口不存在', 404)
    trip_id, action = match.groups()
    studio = service()
    if method == 'GET' and not action:
        enabled = getattr(studio, 'phase1_enabled', False)
        projection = getattr(getattr(studio, 'pool', None), 'public_trip', None)
        trip = projection(trip_id, token) if enabled and callable(projection) else studio.store.get(trip_id, token)
        if enabled:
            from .phase1_service import get_phase1
            pending = trip['status'] == 'running' or (trip.get('metrics') or {}).get('terminal_persistence') == 'pending'
            instance = None if pending else get_phase1(studio)
            trip['uncertainty'] = instance.view(trip_id, token) if instance else {
                'claims': [], 'reservation_status': 'not_attempted', 'opening_status': 'unknown',
                'blind_spots': (trip.get('metrics') or {}).get('blind_spots')}
        return {'trip': trip}, 200
    if method == 'DELETE' and not action:
        studio.store.delete(trip_id, token)
        forget = getattr(getattr(studio, 'pool', None), 'forget_trip', None)
        if callable(forget):
            forget(trip_id)
        return {'deleted': True}, 200
    if method == 'POST' and action == 'undo':
        return {'trip': studio.store.undo(trip_id, token, body.get('expected_revision'))}, 200
    if method == 'POST' and action == 'actions/report':
        return {'trip': studio.store.report_action(trip_id, token, body.get('expected_revision'),
            bounded_text(body, 'action_id', 160, required=True),
            bounded_text(body, 'status', 30, required=True))}, 200
    if method == 'POST' and action == 'prepare':
        return studio.prepare(trip_id, token, body.get('expected_revision'),
                              bounded_text(body, 'feedback', 4000)), 202
    if method == 'POST' and action == 'message':
        return studio.message(trip_id, token, body.get('expected_revision'), body)
    if method == 'POST' and action == 'profile':
        return {'trip': studio.store.update_profile(trip_id, token, body.get('expected_revision'),
            body.get('expected_profile_revision'), body.get('traveler_profile'))}, 200
    if method == 'POST' and action in ('revise', 'retry'):
        feedback = bounded_text(body, 'feedback', 4000)
        material = bounded_text(body, 'evidence_text', 20000)
        images = parse_images(body)
        documents = extract_documents(body.get('documents'))
        links = parse_links(body, [feedback, material])
        additions = {'note': material, 'images': images, 'links': links, 'documents': documents}
        if action == 'revise' and not (feedback or material or images or links or documents) and 'locked_item_ids' not in body:
            raise StateError('BAD_INPUT', '请说一句想调整的地方，或选择要保留的安排')
        return studio.revise(trip_id, token, body.get('expected_revision'), feedback,
                             body.get('locked_item_ids'), retry=action == 'retry', additions=additions), 202
    raise StateError('NOT_FOUND', '旅行接口不存在', 404)


def parse_links(body, texts):
    try:
        return extract_links(texts, body.get('links'))
    except ValueError as error:
        raise StateError('BAD_LINK', str(error)) from None
