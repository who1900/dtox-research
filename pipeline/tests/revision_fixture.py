"""Shared synthetic v2 artifact and independently supplied admission fixtures."""
import copy
import hashlib

from pipeline import publication as p


def sha(value):
    return hashlib.sha256(p._revision_bytes(value)).hexdigest()


def admitted_artifact(paper, projection, families, recipe, bindings, *, parent_revision=None,
                      parent_generation=0, payload_extras=None):
    artifact = {"schema": "dtox-publication-revision-2", "paper_id": paper,
        "parent_revision": parent_revision, "parent_generation": parent_generation,
        "projection": copy.deepcopy(projection), "artifact_bindings": dict(bindings),
        "vector_families": copy.deepcopy(families), "coarse_recipe": copy.deepcopy(recipe)}
    snapshots = {}
    meta = projection["metadata"]
    for name, family in families.items():
        snapshots[name] = {}
        for point in family["points"]:
            chunk = next((c for c in projection["chunks"] if c["point_id"] == point["point_id"]), None)
            text = (chunk["text"] if name == "fulltext" and chunk else
                    p._revision_coarse_input(projection, recipe, bindings))
            payload = {"arxiv_id": paper, "revision_family": name,
                "revision_model_id": family["model_id"], "revision_model_sha256": family["model_sha256"],
                "revision_recipe_sha256": family["recipe_sha256"], "revision_metadata": meta,
                "revision_reuse_from": point["reuse_from"], "title": meta["title"], "year": meta["year"],
                "layers": meta["layers"].split(",") if meta["layers"] else [], "revision_input_text": text,
                "text": text, "source_url": meta["source_url"], "abstract": meta["abstract"],
                "section_title": "Источник Δ", "future_semantic_field": {"rows": [1, None, "α"]}}
            if name == "fulltext" and chunk:
                payload.update(section_type=chunk["section_type"], element_type=chunk["element_type"])
            payload.update(payload_extras or {})
            payload = copy.deepcopy(payload)
            snapshots[name][point["point_id"]] = {"payload": payload, "sha256": sha(payload)}
    artifact["api_payload_snapshot"] = {"schema": "dtox-api-payload-snapshot-1",
        "api_source_sha256": p.REVISION_API_SOURCE_SHA256, "projection_sha256": sha(projection),
        "exclusions": list(p.REVISION_PAYLOAD_EXCLUSIONS), "families": snapshots}
    artifact["artifact_bindings"]["api_payload_snapshot"] = sha(artifact["api_payload_snapshot"])
    return artifact


def stage_artifact(conn, root, artifact):
    return p.prepare_revision(conn, root, artifact["paper_id"], artifact["projection"],
        expected_revision=artifact["parent_revision"], expected_generation=artifact["parent_generation"],
        artifact_bindings=artifact["artifact_bindings"], vector_families=artifact["vector_families"],
        coarse_recipe=artifact["coarse_recipe"], api_payload_snapshot=artifact["api_payload_snapshot"])


def stored_payload(artifact, name, point_id):
    row = artifact["api_payload_snapshot"]["families"][name][point_id]
    return {**copy.deepcopy(row["payload"]), "publication_revision": sha(artifact),
            "api_payload_sha256": row["sha256"]}


def payload_admission(artifacts):
    return {"schema": "dtox-api-payload-admission-1", "api_source_sha256": p.REVISION_API_SOURCE_SHA256,
            "revisions": {sha(a): sha(a["api_payload_snapshot"]) for a in artifacts}}
