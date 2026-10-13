"""Qdrant grouped vector search helpers."""


def grouped_hits(post, base_url, collection, vector, qfilter, limit, timeout,
                 group_size=1):
    """Return grouped search hits as one flat, score-ordered list."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if group_size <= 0:
        raise ValueError("group_size must be positive")

    body = {
        "vector": vector,
        "group_by": "arxiv_id",
        "limit": limit,
        "group_size": group_size,
        "with_payload": True,
        "with_vector": False,
    }
    if qfilter is not None:
        body["filter"] = qfilter

    response = post(
        f"{base_url.rstrip('/')}/collections/{collection}/points/search/groups",
        json=body,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        raise ValueError("malformed Qdrant grouped-search response: result must be an object")
    result = payload["result"]
    if "groups" not in result or not isinstance(result["groups"], list):
        raise ValueError("malformed Qdrant grouped-search response: groups must be a list")
    groups = result["groups"]
    if any(not isinstance(group, dict) or not isinstance(group.get("hits", []), list)
           for group in groups):
        raise ValueError("malformed Qdrant grouped-search response: group hits must be lists")
    return [hit for group in groups for hit in (group.get("hits") or [])]
