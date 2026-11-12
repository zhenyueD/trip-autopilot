"""Offline source policy catalogue, separate from adapter implementation.

This module does not acquire documents, approve access, or instantiate adapters.
Allowed paths describe policy options subject to each record's conditions, not
operational readiness. ``mul`` means multilingual in the specification, not a
claim that a particular document exists in every language. Registry order is
the specification's order; it is also the unranked, unknown-destination fallback.
"""

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    source_id: str
    languages: tuple[str, ...]
    regions: tuple[str, ...]
    acquisition_methods: tuple[str, ...]
    allowed_acquisition_methods: tuple[str, ...]
    compliance_status: str
    adapter_status: str
    conditions: tuple[str, ...]
    cost_status: str


SOURCE_REGISTRY = (
    SourceDefinition(
        'tripadvisor_forum', ('en',), ('global',),
        ('user_share', 'compliant_api'), ('user_share',),
        'tos_under_review', 'implemented',
        ('Offline staging only; Muse review pending; not production readiness.',
         'User-shared link/content only pending ToS review.',
         'API access is not approved or implemented.'), 'not_established',
    ),
    SourceDefinition(
        'reddit_post', ('en',), ('global',),
        ('search_public_page_read', 'official_data_api_oauth'), ('search_public_page_read',),
        'public_page_only', 'planned',
        ('Only search-discovered public post pages use the existing credential-free reader.',
         'Dedicated adapter and account connectors remain unimplemented; no live acceptance implied.',
         'Stop at login, app or challenge walls; no bypass, API, OAuth, RSS or bulk collection.',
         'Commercial direct access is not approved or implemented.',
         'Leads and experience corroboration only; retain social_unverified, never hard-fact authority.',
         'Keep short excerpts, source links, platform and dates; no whole-post or image copying.',
         'Prefer social posts within 7 days; unknown dates remain unknown and older posts are background.'),
        'not_established',
    ),
    SourceDefinition(
        'google_maps_review', ('mul',), ('global',),
        ('places_api',), ('places_api',), 'compliant_api', 'implemented',
        ('Offline staging only; Muse review pending; not production readiness.',
         'Real API calls require separate budget and authorization.',
         'Subject to Places API access, terms and attribution requirements.',),
        'not_established',
    ),
    SourceDefinition(
        'hotel_official', ('mul',), ('global',),
        ('public_page_read',), ('public_page_read',),
        'robots_and_tos_required', 'implemented',
        ('Offline staging only; Muse review pending; not production readiness.',
         'Respect robots.txt and site ToS; no whole-site or high-rate collection.',),
        'not_established',
    ),
    SourceDefinition(
        'transport_authority', ('en', 'ar'), ('united_arab_emirates',),
        ('public_page_read',), ('public_page_read',),
        'official_public_information', 'implemented',
        ('Offline staging only; Muse review pending; not production readiness.',
         'UAE ITC/admobility scope only; respect robots.txt and ToS.',
         'No whole-site or high-rate collection.'), 'not_established',
    ),
    SourceDefinition(
        'xiaohongshu_post', ('zh',), ('chinese_outbound_travelers',),
        ('user_authorized_share',), ('user_authorized_share',),
        'user_authorization_required', 'implemented',
        ('User actively supplies an authorized link/content; no public notes API.',
         'Adapter consumes obtained documents; no account connector is implied.'),
        'not_established',
    ),
    SourceDefinition(
        'mafengwo_post', ('zh',), ('chinese_outbound_travelers',),
        ('user_share',), ('user_share',), 'tos_under_review', 'planned',
        ('User-shared link/content only pending ToS review.',), 'not_established',
    ),
    SourceDefinition(
        'instagram_post', ('mul',), ('global',),
        ('user_authorized_share',), ('user_authorized_share',),
        'user_authorization_required', 'planned',
        ('User supplies authorized content; research pilot is not an adapter.',),
        'not_established',
    ),
)


# Finite, exact aliases only: this is policy routing, not place discovery.
_DESTINATIONS = (
    ('united_arab_emirates', ('en', 'zh'), (
        'uae', 'united arab emirates', '阿联酋', '阿拉伯联合酋长国',
        'abu dhabi', '阿布扎比', 'dubai', '迪拜', 'sharjah', '沙迦',
    )),
    ('middle_east', ('en', 'zh'), (
        'middle east', '中东', 'saudi arabia', '沙特', 'riyadh', '利雅得',
        'qatar', '卡塔尔', 'doha', '多哈', 'oman', '阿曼', 'muscat', '马斯喀特',
    )),
    ('japan', ('ja', 'zh'), (
        'japan', 'jp', '日本', 'tokyo', '東京', '东京', 'osaka', '大阪',
        'kyoto', '京都', 'sapporo', '札幌', 'fukuoka', '福冈', '福岡',
    )),
    ('south_korea', ('ko',), (
        'south korea', 'korea', 'kr', '韩国', '韓國', 'seoul', '首尔', '首爾',
        'busan', '釜山', 'jeju', '济州', '濟州',
    )),
    ('thailand', ('th',), (
        'thailand', 'th', '泰国', '泰國', 'bangkok', '曼谷', 'phuket', '普吉',
        '普吉岛', 'chiang mai', '清迈', '清邁',
    )),
    ('france', ('fr',), ('france', 'fr', '法国', '法國', 'paris', '巴黎')),
    ('germany', ('de',), ('germany', 'de', '德国', '德國', 'berlin', '柏林')),
    ('italy', ('it',), ('italy', 'it', '意大利', '義大利', 'rome', '罗马', '羅馬')),
    ('spain', ('es',), ('spain', 'es', '西班牙', 'madrid', '马德里', '馬德里')),
    ('united_kingdom', ('en',), (
        'united kingdom', 'uk', 'britain', '英国', '英國', 'london', '伦敦', '倫敦',
    )),
    ('united_states', ('en',), (
        'united states', 'united states of america', 'us', 'usa', '美国', '美國',
        'new york', 'nyc', '纽约', '紐約', 'los angeles', '洛杉矶', '洛杉磯',
        'san francisco', '旧金山', '舊金山',
    )),
)


_LANGUAGE_TAG = re.compile(
    r'[A-Za-z]{2,3}(?:-[A-Za-z]{4})?(?:-(?:[A-Za-z]{2}|[0-9]{3}))?'
    r'(?:-(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*'
)


def _normalized_inputs(destination, user_languages):
    if (not isinstance(destination, str) or not destination.strip()
            or not destination.isprintable()):
        raise ValueError('destination must be nonempty printable text')
    if not isinstance(user_languages, (list, tuple)):
        raise ValueError('user_languages must be a list or tuple of language tags')
    if any(not isinstance(language, str) or not _LANGUAGE_TAG.fullmatch(language)
           for language in user_languages):
        raise ValueError('languages must use common BCP 47 language/script/region tags')
    return (' '.join(destination.casefold().split()),
            tuple(language.casefold().split('-')[0] for language in user_languages))


def select_sources(destination: str, user_languages: list[str] | tuple[str, ...]
                   ) -> list[SourceDefinition]:
    """Return policy-permitted source records; this never executes collection.

    Accept common BCP 47 language/script/region/variant tags, not extensions or
    private-use tags. Unknown places retain the catalogue order regardless of
    user language: this fallback makes no claim of destination relevance.
    """
    key, user_primary = _normalized_inputs(destination, user_languages)
    sources = [source for source in SOURCE_REGISTRY if source.allowed_acquisition_methods]
    profile = next((item for item in _DESTINATIONS if key in item[2]), None)
    if profile is None:
        return sources
    region, destination_languages, _ = profile
    languages = tuple(dict.fromkeys((*destination_languages, *user_primary)))
    sources = [source for source in sources if any(
        scope in source.regions
        for scope in ('global', 'chinese_outbound_travelers', region)
    )]

    def priority(source):
        return next((index for index, language in enumerate(languages)
                     if language in source.languages or 'mul' in source.languages),
                    len(languages))

    return sorted(sources, key=priority)
