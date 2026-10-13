"""Read-side storage equivalence for optional bibliographic metadata.

This is not artifact serialization: raw payloads and their hashes stay intact.
Unknown citation counts remain distinct from a known count of zero.
"""

NULLABLE_BIBLIOGRAPHIC_FIELDS = frozenset({'venue', 'citation_count', 'url'})


def bibliographic_payload_equal(left, right):
    """Compare complete payloads, accepting only listed null/absent fields."""
    a, b = dict(left), dict(right)
    for field in NULLABLE_BIBLIOGRAPHIC_FIELDS:
        if a.get(field) is None and b.get(field) is None:
            a.pop(field, None)
            b.pop(field, None)
    return a == b
