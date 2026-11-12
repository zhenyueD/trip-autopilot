"""Entry identity from application receipts and an operator-maintained host list.

TRAVEL_OFFICIAL_DOMAINS is a comma-separated list of independently reviewed exact
hostnames. It must never be populated from model output or a page's own claim.
Empty configuration deliberately verifies no official identities. Subdomains
must be listed explicitly, and redirects are classified by their terminal host.
"""
import os
import re

from .material_reader import _validate_url


TRAVELER_SOURCE_TYPES = frozenset({
    'xiaohongshu_post', 'instagram_post', 'tripadvisor_forum', 'reddit_post',
    'google_maps_review', 'mafengwo_post',
})


def is_direct_read(receipt):
    return (receipt.get('status') == 'read' and receipt.get('method') == 'direct_http'
            and type(receipt.get('http_status')) is int and receipt['http_status'] == 200
            and receipt.get('verification') != 'search_grounded_not_direct_page')


def classify_entry(receipt):
    """Classify a server-owned receipt; never trust a supplied official flag."""
    provenance = receipt.get('source_provenance')
    provenance = provenance if isinstance(provenance, dict) else {}
    url = receipt.get('url') or provenance.get('source_ref')
    try:
        host = _validate_url(url).hostname
    except (ValueError, TypeError, UnicodeError):
        return 'unavailable'
    if receipt.get('status') in {'blocked', 'unavailable'} or (isinstance(receipt.get('http_status'), int)
                                                             and receipt['http_status'] != 200):
        return 'unavailable'
    verification = receipt.get('verification')
    verification = verification if isinstance(verification, dict) else {}
    if provenance and (not isinstance(provenance.get('source_type'), str)
                       or not isinstance(provenance.get('source_ref'), str)):
        return 'search_lead'
    if verification and not isinstance(verification.get('status'), str):
        return 'search_lead'
    if provenance.get('source_type') in TRAVELER_SOURCE_TYPES:
        # A provenance label is attribution, not proof of official facts. Neither
        # a reachable page nor a report count may raise this to official trust.
        return ('traveler_tested' if verification.get('status') == 'social_unverified'
                and provenance.get('source_ref') == url else 'search_lead')
    if verification.get('status') in {'social_unverified', 'user_provided_unverified'}:
        return 'search_lead'
    if not is_direct_read(receipt):
        return 'search_lead'
    hosts = {value.strip().lower().rstrip('.')
             for value in os.getenv('TRAVEL_OFFICIAL_DOMAINS', '').split(',')
             if re.fullmatch(r'[A-Za-z0-9.-]+', value.strip())}
    return 'official_verified' if host in hosts else 'public_page'
