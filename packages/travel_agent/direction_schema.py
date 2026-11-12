"""Direction protocol supplied to the model before local semantic validation."""
import json

TEXT = {'type': 'string', 'maxLength': 1200}


def array(item, maximum):
    return {'type': 'array', 'items': item, 'maxItems': maximum}


def obj(fields, required=None):
    return {'type': 'object', 'properties': fields,
            'required': list(fields) if required is None else required}


DIRECTION_SCHEMA = obj({
    'title': TEXT, 'premise': TEXT, 'days': {'type': 'integer', 'minimum': 1, 'maximum': 14},
    'assumptions': array(TEXT, 6), 'questions': array(TEXT, 3), 'research_queries': array(TEXT, 6),
    'understanding': array(obj({'text': TEXT,
        'kind': {'type': 'string', 'enum': ['material_fact', 'hypothesis']},
        'evidence': array(obj({'source_id': TEXT, 'quote': TEXT}), 5)}), 16),
    'experience_angles': array(obj({key: TEXT for key in ('idea', 'why', 'tradeoff', 'place_query')}), 12),
    'destinations': array(obj({
        'name': {'type': 'string', 'minLength': 1, 'maxLength': 200,
                 'description': '本次选定实际停留城镇的显示名，不填宽泛地区或尚未选定的备选'},
        'search_name': {'type': 'string', 'minLength': 1, 'maxLength': 200,
                        'description': '具体停留城镇的当地或英文专名，作为地点检索锚；不填湖区、国家或氛围'},
        'country_code': {'type': ['string', 'null'], 'pattern': '^[A-Z]{2}$',
                         'description': '该城镇所在国家的 ISO 3166-1 alpha-2 代码；无法确定则 null。只是检索限定，不是来源已核验事实'}}), 8),
    'place_hints': array(obj({key: {'type': 'string', 'maxLength': 200}
                             for key in ('name', 'destination', 'kind')}), 20),
    'commitment_updates': array(obj({
        'id': {'type': 'string', 'maxLength': 80},
        'action': {'type': 'string', 'enum': ['upsert', 'cancel']},
        'label': {'type': 'string', 'maxLength': 160},
        'recurrence': {'type': 'string', 'enum': ['every_day', 'once']},
        'day': {'type': 'integer', 'minimum': 1, 'maximum': 14},
        'activity_kind': {'type': 'string', 'enum': ['rest', 'meal', 'activity', 'personal']},
        'location_kind': {'type': 'string', 'enum': ['lodging', 'any']},
        'min_duration_minutes': {'type': 'integer', 'minimum': 1, 'maximum': 1440},
        'window_start': {'type': 'string', 'pattern': '^(?:[01][0-9]|2[0-3]):[0-5][0-9]$'},
        'window_end': {'type': 'string', 'pattern': '^(?:[01][0-9]|2[0-3]):[0-5][0-9]$'},
        'source': obj({'material_id': TEXT, 'quote': {'type': 'string', 'maxLength': 600}})},
        ['id', 'action', 'source']), 8),
    'work_plan': obj({'goal': TEXT, 'actions': array(obj({
        'tool': {'type': 'string', 'enum': ['search_supply', 'inspect_source', 'inspect_geography']},
        'queries': array(TEXT, 12), 'supply_id': TEXT, 'supply_ids': array(TEXT, 20), 'reason': TEXT},
        ['tool']), 2)}),
    'learning_updates': array(obj({key: TEXT for key in ('text', 'scope', 'source_id', 'quote')}), 8),
})


def query_diagnostics(raw):
    value = raw.get('research_queries') if isinstance(raw, dict) else None
    return {'field': 'research_queries', 'value_type': type(value).__name__,
            'count': len(value) if isinstance(value, list) else None, 'max_items': 6,
            'item_types': sorted({type(row).__name__ for row in value}) if isinstance(value, list) else []}


def direction_messages(messages):
    return [{**messages[0], 'content': messages[0]['content'] +
             '\n本轮方向输出完整schema（不同字段数组上限不同）：\n' +
             json.dumps(DIRECTION_SCHEMA, ensure_ascii=False, separators=(',', ':')) +
             '\n输出前检查地点选择：备选并不等于必去。先根据用户目标、天数和转场成本选一条连贯路线，'
             '再把实际停留城镇写入destinations；宽地区只保留在premise/understanding，'
             '未选的备选及舍弃理由写入premise/experience_angles。用户明确必去的地点仍须覆盖。'
             '每个城镇给country_code；place_hints.destination逐字引用选定城镇，'
             'place_hints.name只能是具体专名，不用“当地餐厅/迷人酒店”等类别占位。'
             'research_queries与work_plan仅研究选定路线。所有城镇选择仍是可纠正建议，'
             '不是用户已确认的行程或已核验地理事实。'}, *messages[1:]]
