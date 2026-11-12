"""Official transport documents obtained by callers; no acquisition."""
from .official import OfficialAdapter


class TransportAuthorityAdapter(OfficialAdapter):
    source_type = "transport_authority"
