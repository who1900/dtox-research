"""Pure pre-topK REST filter builder; caller supplies an admitted SQLite snapshot."""
import json
import re


class VisibilityBlocked(ValueError):
    pass


def build_visibility_filter(committed_tokens, *, root_token, max_tokens,
                            max_filter_bytes, base_filter=None):
    if (type(max_tokens) is not int or max_tokens < 1
            or type(max_filter_bytes) is not int or max_filter_bytes < 1):
        raise VisibilityBlocked("explicit positive token/byte limits required")
    if (not isinstance(committed_tokens, (list, tuple)) or not committed_tokens
            or len(committed_tokens) > max_tokens):
        raise VisibilityBlocked("committed token capacity exceeded or snapshot missing")
    if (any(not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None
            for token in committed_tokens) or len(set(committed_tokens)) != len(committed_tokens)):
        raise VisibilityBlocked("unique canonical committed tokens required")
    if not isinstance(root_token, str) or root_token not in committed_tokens:
        raise VisibilityBlocked("explicit admitted ROOT must belong to snapshot")
    tokens = sorted(committed_tokens)
    nested = {"nested": {"key": "visibility_intervals", "filter": {
        "must": [{"key": "born", "match": {"any": tokens}}],
        "must_not": [{"key": "retired", "match": {"any": tokens[:]}}],
    }}}
    result = {"must": [nested]}
    if base_filter is not None:
        if (not isinstance(base_filter, dict)
                or not set(base_filter).issubset({"must", "must_not", "should", "min_should"})):
            raise VisibilityBlocked("base must be an already validated REST Filter")
        try:
            base = json.loads(json.dumps(base_filter, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise VisibilityBlocked("base filter must be finite JSON") from exc
        result["must"].insert(0, base)
    try:
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise VisibilityBlocked("filter is not canonical UTF8 JSON") from exc
    if len(encoded) > max_filter_bytes:
        raise VisibilityBlocked("filter byte capacity exceeded; no token truncation")
    return result
