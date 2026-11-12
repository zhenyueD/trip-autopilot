"""Model reading views omit withdrawn source bodies while audit data stays intact."""
from copy import deepcopy

from .detail_reviews import source_withdrawn


def source_policy_view(value):
    if isinstance(value, dict):
        if source_withdrawn(value):
            return {**{key: deepcopy(value[key]) for key in
                       ('id', 'material_id', 'content_sha256') if key in value},
                    'status': 'withdrawn',
                    'message': '此版本来源已撤下；原文仅供审计，不可引用或用于安排。'}
        return {key: source_policy_view(child) for key, child in value.items()}
    if isinstance(value, list):
        return [source_policy_view(child) for child in value]
    return deepcopy(value)
