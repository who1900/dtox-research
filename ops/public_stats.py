#!/usr/bin/env python3
"""Writes the public corpus counters for read.whoim.space/research/stats.json."""
import json, os, sqlite3, tempfile, urllib.request
from datetime import datetime, timezone
db = sqlite3.connect("file:/opt/dtox-research/state.db?mode=ro", uri=True, timeout=30)
docs = db.execute("select count(*) from papers where status='done'").fetchone()[0]
web3 = db.execute("select count(*) from papers where status='done' and layers like '%web3%'").fetchone()[0]
edges = db.execute("select count(*) from citations").fetchone()[0]
with urllib.request.urlopen("http://127.0.0.1:6333/collections/papers_fulltext", timeout=30) as r:
    chunks = json.load(r)["result"]["points_count"]
out = {"documents": docs, "chunks": chunks, "citation_edges": edges, "web3_documents": web3,
       "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")}
dst = "/var/www/dtox-research/stats.json"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst))
with os.fdopen(fd, "w") as f:
    json.dump(out, f)
os.chmod(tmp, 0o644)
os.replace(tmp, dst)
