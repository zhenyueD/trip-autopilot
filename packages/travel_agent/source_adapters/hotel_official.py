"""Caller-obtained hotel text, never a booking or verification client."""
from .official import OfficialAdapter


class HotelOfficialAdapter(OfficialAdapter):
    source_type = "hotel_official"
