#!/usr/bin/env python3
"""Stage and safely add verified author/repository PDFs for selected IACR papers."""
import argparse
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import sys
import unicodedata

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import iacr_oa_fulltext as io_

DATA_DIR = os.getenv("DTOX_DATA_DIR", "/opt/dtox-research")
CATALOG = HERE / "web3_author_copies.json"
MAX_PDF_BYTES = 20 * 1024 * 1024
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
IACR_RE = re.compile(r"\d{4}/\d{3,6}\Z")


def load_catalog(path):
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    return load_catalog_from_items(items)


def load_catalog_from_items(items):
    if not isinstance(items, list):
        raise ValueError("catalog must be a JSON list")
    for x in items:
        original = x.get("originaliacr_id")
        valid_iacr = isinstance(original, str) and bool(IACR_RE.fullmatch(original))
        x["standalone"] = x.get("curated_standalone") is True and (valid_doi(x.get("doi")) or valid_iacr) and standalone_metadata_valid(x)
        x["id"] = "iacr:" + original if valid_iacr else ("doi:" + x["doi"].strip().lower() if x["standalone"] and valid_doi(x.get("doi")) else None)
    return items


def valid_doi(value):
    return isinstance(value, str) and bool(DOI_RE.fullmatch(value.strip()))


def standalone_metadata_valid(item):
    authors = item.get("authors")
    return (isinstance(item.get("title"), str) and len(item["title"].strip()) >= 12
            and isinstance(authors, list) and bool(authors)
            and all(isinstance(a, str) and len(a.strip().split()) >= 2 for a in authors)
            and isinstance(item.get("year"), int) and 1900 <= item["year"] <= 2100
            and isinstance(item.get("identity_source"), str) and item["identity_source"].startswith("https://")
            and isinstance(item.get("copy_url"), str) and item["copy_url"].startswith("https://"))


def validation_text(text):
    return unicodedata.normalize("NFKC", text or "")


def title_opens(text, title):
    return io_.text_opens_with_title(validation_text(text), validation_text(title))


def safe_id(pid):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", pid)


def catalog_title_matches(row_title, item):
    accepted = [item.get("title", "")] + (item.get("original_title_aliases") or [])
    return any(io_.norm_title(row_title) == io_.norm_title(title) for title in accepted if title)


def open_db(path, write=False):
    uri = pathlib.Path(path).resolve().as_uri() + ("?mode=rw" if write else "?mode=ro")
    c = sqlite3.connect(uri, uri=True)
    c.row_factory = sqlite3.Row
    return c


def audit(conn, catalog):
    ids = sorted(set(io_.CANON_IDS) | {x["id"] for x in catalog if x["id"] and x["id"].startswith("iacr:")})
    clauses = ["arxiv_id IN (" + ",".join("?" for _ in ids) + ")"]
    params = list(ids)
    for pattern in io_.CANON_TITLE_PATTERNS:
        clauses.append("lower(title) LIKE ?")
        params.append("%" + pattern + "%")
    for x in catalog:
        if (not x.get("id") or x.get("standalone")) and x.get("title"):
            phrase = " ".join(io_.norm_title(x["title"]).split()[:5])
            if phrase:
                clauses.append("lower(title) LIKE ?")
                params.append("%" + phrase + "%")
    clauses.append("twin_of IN (" + ",".join("?" for _ in ids) + ")")
    params.extend(ids)
    rows = conn.execute("SELECT arxiv_id,title,status,fulltext_source,twin_of FROM papers WHERE " + " OR ".join(clauses), params).fetchall()
    byid = {r["arxiv_id"]: r for r in rows}
    actions = []
    for item in catalog:
        if item.get("standalone") and item["id"].startswith("doi:"):
            matches = [r["arxiv_id"] for r in rows if io_.norm_title(r["title"]) == io_.norm_title(item["title"])]
            actions.append({"id": item["id"], "action": "standalone_candidate", "exact_title_matches": matches, "license": item.get("license") or "unknown"})
            continue
        if not item["id"]:
            matches = [r["arxiv_id"] for r in rows if item.get("title") and io_.norm_title(r["title"]) == io_.norm_title(item["title"])]
            actions.append({"id": None, "title": item.get("title"), "exact_title_matches": matches, "action": "skip_missing_original_id", "license": item.get("license") or "unknown"})
            continue
        r = byid.get(item["id"])
        actions.append({"id": item["id"], "action": "stage" if r and r["fulltext_source"] == "iacr-abstract" and r["status"] == "done" else ("standalone_candidate" if item.get("standalone") and not r else "skip"), "license": item.get("license") or "unknown"})
    return {"counts": {"selected": len(ids), "existing": sum(i in byid for i in ids), "canon": len(io_.CANON_IDS), "catalog": len(catalog)},
            "papers": [{"id": r["arxiv_id"], "title": r["title"], "status": r["status"], "source": r["fulltext_source"], "twin": r["twin_of"]} for r in rows], "plan": actions}


def default_to_text(raw):
    import pymupdf
    import pymupdf4llm
    if callable(getattr(pymupdf4llm, "use_layout", None)):
        pymupdf4llm.use_layout(False)
    doc = pymupdf.open(stream=raw, filetype="pdf")
    try:
        return pymupdf4llm.to_markdown(doc, show_progress=False, write_images=False)
    finally:
        doc.close()


def download(catalog, artifact_dir, limit, fetcher, to_text=None, only_ids=None):
    out = []
    to_text = to_text or default_to_text
    def report(result):
        out.append(result)
        print(json.dumps(result, separators=(",", ":")), file=sys.stderr, flush=True)
    os.makedirs(artifact_dir, exist_ok=True)
    eligible = [x for x in catalog if x.get("id") and (only_ids is None or x["id"] in only_ids)][:min(limit, 6)]
    for x in eligible:
        if not x.get("id"):
            report({"id": None, "action": "skip_missing_original_id"}); continue
        stem = safe_id(x["id"])
        pdfp, mdp, mp = (os.path.join(artifact_dir, stem + ext) for ext in (".pdf", ".md", ".json"))
        if all(os.path.isfile(p) for p in (pdfp, mdp, mp)):
            report({"id": x["id"], "action": "already_staged"}); continue
        try:
            raw = fetcher.pdf(x["copy_url"])
            text = to_text(raw)
            head = io_.norm_title(validation_text(text[:4000]))
            surnames = [io_.norm_title(validation_text(n)).split()[-1] for n in (x.get("authors") or []) if io_.norm_title(validation_text(n))]
            author_ok = (not x.get("standalone") and not surnames) or any(re.search(r"\b" + re.escape(n) + r"\b", head) for n in surnames)
            if len(text or "") < 3000 or not title_opens(text, x["title"]) or not author_ok:
                report({"id": x["id"], "action": "reject_validation"}); continue
            digest = hashlib.sha256(raw).hexdigest()
            text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            manifest = {"url": x["copy_url"], "original_id": x["id"], "paper_title": x["title"], "length": len(raw), "sha256": digest, "text_sha256": text_digest, "pdf_magic": raw[:4].decode("ascii"), "first_page_title_valid": True, "first_page_author_valid": True, "license": x.get("license") or "unknown", "standalone": bool(x.get("standalone")), "doi": x.get("doi"), "year": x.get("year"), "authors": x.get("authors"), "identity_source": x.get("identity_source"), "copy_publication_year": x.get("copy_publication_year"), "copy_version": x.get("copy_version")}
            manifest.update(parser="pymupdf4llm-native-markdown", exact_latex=False)
            for path, data in ((pdfp, raw), (mdp, text.encode("utf-8")), (mp, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))):
                with open(path, "xb") as f: f.write(data)
            report({"id": x["id"], "action": "staged", "sha256": digest})
        except Exception as e:
            report({"id": x["id"], "action": "error", "error": type(e).__name__})
    return out


def apply_artifacts(conn, catalog, artifact_dir, data_dir, limit):
    result = []
    cache = os.path.join(data_dir, "fulltext_cache")
    os.makedirs(cache, exist_ok=True)
    for x in catalog:
        if not x.get("id"):
            result.append({"id": None, "action": "skip_missing_original_id"}); continue
        if len([r for r in result if r["action"] == "inserted"]) >= min(limit, 3):
            break
        pid = x["id"]
        standalone = x.get("standalone") is True
        if not pid.startswith(("iacr:", "doi:")) and not standalone:
            result.append({"id": pid, "action": "skip_invalid_standalone"}); continue
        original = conn.execute("SELECT * FROM papers WHERE arxiv_id=?", (pid,)).fetchone() if pid.startswith("iacr:") else None
        if not original and not standalone:
            result.append({"id": pid, "action": "skip_missing_original"}); continue
        if original and (original["status"] != "done" or original["fulltext_source"] != "iacr-abstract"):
            result.append({"id": pid, "action": "skip_not_abstract_only"}); continue
        if original and not catalog_title_matches(original["title"], x):
            result.append({"id": pid, "action": "skip_title_mismatch"}); continue
        stem = safe_id(pid)
        try:
            with open(os.path.join(artifact_dir, stem + ".json"), encoding="utf-8") as f: m = json.load(f)
            with open(os.path.join(artifact_dir, stem + ".pdf"), "rb") as f: pdf = f.read(MAX_PDF_BYTES + 1)
            with open(os.path.join(artifact_dir, stem + ".md"), encoding="utf-8") as f: text = f.read()
            surnames = [io_.norm_title(validation_text(n)).split()[-1] for n in (x.get("authors") or []) if io_.norm_title(validation_text(n))]
            head = io_.norm_title(validation_text(text[:4000]))
            author_ok = (not standalone and not surnames) or (bool(surnames) and any(re.search(r"\b" + re.escape(n) + r"\b", head) for n in surnames))
            valid = (len(pdf) <= MAX_PDF_BYTES and pdf.startswith(b"%PDF") and m["original_id"] == pid and io_.norm_title(validation_text(m["paper_title"])) == io_.norm_title(validation_text(x["title"])) and m["url"] == x["copy_url"] and m["length"] == len(pdf) and m["sha256"] == hashlib.sha256(pdf).hexdigest() and m.get("text_sha256") == hashlib.sha256(text.encode("utf-8")).hexdigest() and m["pdf_magic"] == "%PDF" and m["first_page_title_valid"] is True and m.get("first_page_author_valid") is True and (not standalone or (m.get("standalone") is True and m.get("doi") == x.get("doi") and m.get("year") == x["year"])) and len(text) >= 3000 and title_opens(text, x["title"]) and author_ok)
        except (OSError, ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            result.append({"id": pid, "action": "reject_artifact"}); continue
        if original:
            identity = pid.removeprefix("iacr:")
        elif x.get("originaliacr_id") and IACR_RE.fullmatch(str(x["originaliacr_id"])):
            identity = str(x["originaliacr_id"])
        else:
            identity = "doi:" + re.sub(r"[^a-z0-9]+", "_", x["doi"].lower()).strip("_")
        new_id = "author:" + identity + ":" + m["sha256"][:12]
        if conn.execute("SELECT 1 FROM papers WHERE arxiv_id=?", (new_id,)).fetchone():
            result.append({"id": pid, "action": "skip_known"}); continue
        cachep = os.path.join(cache, safe_id(new_id) + ".tex")
        try:
            with open(cachep, "xb") as f: f.write(text.encode("utf-8"))
        except FileExistsError:
            try:
                with open(cachep, "rb") as f: cached_digest = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                cached_digest = None
            if cached_digest != m["text_sha256"]:
                result.append({"id": pid, "action": "skip_cache_mismatch"}); continue
        try:
            conn.execute("INSERT INTO papers (arxiv_id,title,year,layers,status,abstract,niche_score,matched_terms,fulltext_source,source_url,twin_of,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                         (new_id, original["title"] if original else x["title"], original["year"] if original else x["year"], original["layers"] if original else "web3", "fulltext_fetched", original["abstract"] if original else None, original["niche_score"] if original else None, original["matched_terms"] if original else "[]", "oa-mirror-pdf", m["url"], pid if original else None))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        result.append({"id": pid, "action": "inserted", "new_id": new_id})
    return result


def main(argv=None):
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--audit", action="store_true"); mode.add_argument("--download", action="store_true"); mode.add_argument("--apply-artifacts", action="store_true")
    ap.add_argument("--db", default=os.path.join(DATA_DIR, "state.db")); ap.add_argument("--catalog", default=str(CATALOG)); ap.add_argument("--artifact-dir", default=os.path.join(DATA_DIR, "web3_author_artifacts")); ap.add_argument("--data-dir", default=DATA_DIR); ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--only", nargs="+", help="process only these catalog IDs")
    a = ap.parse_args(argv)
    if not 1 <= a.limit <= 6: ap.error("--limit must be 1..6")
    catalog = load_catalog(a.catalog)
    if a.audit:
        conn = open_db(a.db)
        try: output = audit(conn, catalog)
        finally: conn.close()
    elif a.download:
        import requests
        output = download(catalog, a.artifact_dir, a.limit, io_.Fetcher(requests.Session()), only_ids=set(a.only) if a.only else None)
    else:
        conn = open_db(a.db, True)
        try: output = apply_artifacts(conn, catalog, a.artifact_dir, a.data_dir, a.limit)
        finally: conn.close()
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__": main()
