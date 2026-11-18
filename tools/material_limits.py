"""Byte budgets shared by the standalone gateway and document reader.

These limits describe transport/extraction capacity, not model understanding.
Keep this module independent of the optional encrypted application core.
"""
MIB = 1024 * 1024
MAX_REQUEST_BYTES = 16 * MIB
MAX_TEXT_REQUEST_BYTES = 256 * 1024
MAX_DOCUMENT_BYTES = 5 * MIB // 2
MAX_ZIP_BYTES = 8 * MIB
MAX_ZIP_EXPANDED_BYTES = 50_000_000
MAX_ZIP_MEMBERS = 2_000

UPLOAD_LIMITS = {
    "request_body_bytes": MAX_REQUEST_BYTES,
    "gateway_body_bytes": MAX_REQUEST_BYTES,
    "document_bytes": MAX_DOCUMENT_BYTES,
    "zip_bytes": MAX_ZIP_BYTES,
    "image_bytes": 600 * 1024,
    "documents": 5,
    "images": 3,
    "zip_expanded_bytes": MAX_ZIP_EXPANDED_BYTES,
    "zip_members": MAX_ZIP_MEMBERS,
}
