#!/usr/bin/env python3
"""Full text for IACR ePrint papers from their legal open copies elsewhere.

About 4.9k iacr:YYYY/N papers sit in the corpus as an abstract only
(fulltext_source='iacr-abstract'): eprint.iacr.org bans our crawler and
forbids PDFs in robots.txt, so nothing here ever contacts *.iacr.org. Many of
these papers have other open copies, found through OpenAlex.

  --probe   ask OpenAlex (exact title + year window) for the open locations of
            each candidate and write a plan JSON. No PDF is downloaded and no
            state is written. Candidates are papers without an arXiv twin
            (twin_of / twins.json): canon first, then niche_score, then year.
  (default) read the plan and act on it. Dry run unless --apply.
  --apply   per paper, in its own commit (sqlite timeout 30):
            * the best copy is on arXiv and the id is not in the corpus: insert
              it as 'discovered' with twin_of set, so the normal pipeline fetches
              the LaTeX; no PDF is downloaded. If the id is already in the
              corpus only twin_of is filled in.
            * otherwise download the PDF of the best repository / author copy
              (robots.txt honoured per host, >= 3 s between requests to a host,
              20 MB cap, redirects followed by hand and re-checked), convert it
              with whitepapers.pdf_to_text, check that the text opens with the
              paper's own title, then write fulltext_cache, drop the old Qdrant
              points / fts rows / chunk caches (as reprocess_lost_body does) and
              set status='fulltext_fetched', fulltext_source='oa-mirror-pdf',
              source_url=<copy url>. chunk_step then re-chunks it as markdown
              (needs the service.py that knows 'oa-mirror-pdf'; the script
              refuses PDF items while the deployed service.py lacks it).
  --feed    with --apply: only as many PDF papers as the pipeline queue has
            room for (--target minus quality_checked+fulltext_fetched+chunked,
            at most --batch), so a timer can trickle the work in.

OpenAlex: the pipeline's persisted 429 pause (scheduler_state
'openalex:paused_until') is honoured and a 429 stops the probe. The key is read
from OPENALEX_API_KEY in the environment and never printed. A lock file keeps
two runs from overlapping.

  iacr_oa_fulltext.py --probe --limit 120
  iacr_oa_fulltext.py --limit 3                  # dry run
  iacr_oa_fulltext.py --apply --limit 3
  iacr_oa_fulltext.py --apply --feed --batch 20
"""
import argparse
import contextlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from urllib import robotparser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reprocess_lost_body as rlb  # noqa: E402
import requests  # noqa: E402

DATA_DIR = os.getenv("DTOX_DATA_DIR", "/opt/dtox-research")
UA = "dtox-research/1.1 (research pipeline; mailto:danik1900@gmail.com)"
ROBOTS_TOKEN = "dtox-research"
OPENALEX_URL = "https://api.openalex.org/works"
OPENALEX_PAUSE_KEY = "openalex:paused_until"
OPENALEX_INTERVAL = 1.0
HOST_PAUSE = 3.0
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 4
MIN_TEXT_CHARS = 3000
FULLTEXT_SOURCE = "oa-mirror-pdf"

# never contacted, at all
BLOCKED_SUFFIXES = ("iacr.org",)
# login walls / scraping-hostile, useless for an unattended fetch
SKIP_HOSTS = ("researchgate.net", "academia.edu", "sci-hub", "libgen")

# Canon ids checked against state.db. Note iacr:2017/620 is the Algebraic Group
# Model, not FRI (FRI/Aurora-era FRI paper has no ePrint id of its own).
CANON_IDS = (
    "iacr:2016/260", "iacr:2019/953", "iacr:2018/046", "iacr:2021/370", "iacr:2023/573",
    "iacr:2023/1217", "iacr:2023/1216", "iacr:2017/1066", "iacr:2019/1047", "iacr:2019/1021",
    "iacr:2019/550", "iacr:2021/529", "iacr:2018/828", "iacr:2017/620",
)
CANON_TITLE_PATTERNS = ("plonky2", "constant-size commitments to polynomials",
                        "fast reed-solomon interactive oracle")
ARXIV_NEW = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
ARXIV_DOI = re.compile(r"10\.48550/arxiv\.(\d{4}\.\d{4,5})", re.I)


def host_of(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_blocked_host(url):
    """True for any iacr.org host, and for hosts that are useless to fetch."""
    h = host_of(url)
    if not h:
        return True
    if any(h == s or h.endswith("." + s) for s in BLOCKED_SUFFIXES):
        return True
    return any(s in h for s in SKIP_HOSTS)


def norm_title(t):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).split())


def title_match(ours, theirs):
    a, b = norm_title(ours), norm_title(theirs)
    if not a or not b:
        return False
    if a == b:
        return True
    # arXiv versions often drop or add a subtitle: accept a long common prefix
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short.split()) >= 5 and long_.startswith(short + " ")


def arxiv_id_of(*urls):
    for u in urls:
        if not u:
            continue
        m = ARXIV_NEW.search(u) or ARXIV_DOI.search(u)
        if m:
            return m.group(1)
    return None


def tier_of(loc):
    """0 arXiv, 1 repository (HAL, Zenodo, universities), 2 anything else."""
    src = loc.get("source") or {}
    urls = (loc.get("pdf_url"), loc.get("landing_page_url"))
    if arxiv_id_of(*urls):
        return 0
    h = host_of(loc.get("pdf_url") or loc.get("landing_page_url") or "")
    if src.get("type") == "repository" or h.endswith((".edu", ".ac.uk")) or ".ac." in h \
            or h in ("hal.science", "zenodo.org", "hal.archives-ouvertes.fr"):
        return 1
    return 2


def _sort_key(c):
    return (c["tier"], not c["pdf"], not c["license"], c["url"])


def copies_from_work(work):
    """Open, non-iacr locations of one OpenAlex work."""
    locs = list(work.get("locations") or [])
    for key in ("best_oa_location", "primary_location"):
        if work.get(key):
            locs.append(work[key])
    out, seen = [], set()
    for loc in locs:
        if not loc or not loc.get("is_oa"):
            continue
        pdf, landing = loc.get("pdf_url"), loc.get("landing_page_url")
        primary = pdf or landing
        if not primary or is_blocked_host(primary) or primary in seen:
            continue
        seen.add(primary)
        aid = arxiv_id_of(pdf, landing)
        src = loc.get("source") or {}
        out.append({
            "tier": tier_of(loc),
            "kind": "arxiv" if aid else "pdf",
            "arxiv_id": aid,
            "url": primary,
            "landing": landing,
            "pdf": bool(pdf),
            "host": host_of(primary),
            "license": loc.get("license"),
            "source": src.get("display_name"),
        })
    return out


def build_entry(row, works, canon):
    """Plan entry for one paper from the OpenAlex works that matched its title."""
    copies = []
    for w in works:
        for c in copies_from_work(w):
            if not any(c["url"] == o["url"] for o in copies):
                copies.append(c)
        aid = arxiv_id_of(w.get("doi") or "")
        if aid and not any(c["arxiv_id"] == aid for c in copies):
            copies.append({"tier": 0, "kind": "arxiv", "arxiv_id": aid,
                           "url": f"https://arxiv.org/abs/{aid}", "landing": None, "pdf": False,
                           "host": "arxiv.org", "license": None, "source": "arXiv (DOI)"})
    copies.sort(key=_sort_key)
    # a landing-only non-arXiv copy cannot be downloaded unattended
    usable = [c for c in copies if c["kind"] == "arxiv" or c["pdf"]]
    entry = {"id": row["arxiv_id"], "title": row["title"], "year": row["year"],
             "niche_score": row["niche_score"], "canon": canon, "action": "none",
             "arxiv_id": None, "source_url": None, "host": None, "license": None,
             "copies": copies}
    if usable:
        best = usable[0]
        entry.update(action="queue_arxiv" if best["kind"] == "arxiv" else "pdf",
                     arxiv_id=best["arxiv_id"], source_url=best["url"], host=best["host"],
                     license=best["license"])
    return entry


# --------------------------------------------------------------------------
# candidate selection
# --------------------------------------------------------------------------

def load_twinned(twins_path):
    """iacr ids that twins.json already groups under a non-iacr canonical id."""
    twinned = set()
    if twins_path and os.path.exists(twins_path):
        with open(twins_path) as f:
            canon = json.load(f).get("canonical", {})
        for member, head in canon.items():
            if member.startswith("iacr:") and head != member and not head.startswith("iacr:"):
                twinned.add(member)
    return twinned


def is_canon(row):
    if row["arxiv_id"] in CANON_IDS:
        return True
    t = norm_title(row["title"])
    return any(p in t for p in CANON_TITLE_PATTERNS)


def candidates(conn, twins_path, limit=0, canon_only=False):
    rows = conn.execute(
        "SELECT p.arxiv_id, p.title, p.year, COALESCE(p.niche_score, 0) AS niche_score FROM papers p "
        "WHERE p.arxiv_id LIKE 'iacr:%' AND p.status='done' AND p.fulltext_source='iacr-abstract'").fetchall()
    # twin_of has no index: one scan for all ids, not one per candidate
    twinned = load_twinned(twins_path)
    twinned.update(r[0] for r in conn.execute("SELECT twin_of FROM papers WHERE twin_of LIKE 'iacr:%'"))
    tagged = [(is_canon(r), r) for r in rows if r["arxiv_id"] not in twinned]
    if canon_only:
        tagged = [t for t in tagged if t[0]]
    tagged.sort(key=lambda t: (not t[0], -(t[1]["niche_score"] or 0), -(t[1]["year"] or 0), t[1]["arxiv_id"]))
    return tagged[:limit] if limit else tagged


# --------------------------------------------------------------------------
# OpenAlex
# --------------------------------------------------------------------------

class OpenAlexPaused(Exception):
    pass


class OpenAlex:
    def __init__(self, session, api_key="", sleep=time.sleep, clock=time.monotonic):
        self.session, self.key = session, api_key
        self._sleep, self._clock, self._last = sleep, clock, None

    def _wait(self):
        if self._last is not None:
            delta = OPENALEX_INTERVAL - (self._clock() - self._last)
            if delta > 0:
                self._sleep(delta)
        self._last = self._clock()

    def find(self, title, year):
        words = norm_title(title)
        if not words:
            return []
        params = {"filter": f"title.search:{words}",
                  "per-page": 8,
                  "select": "id,doi,title,publication_year,locations,best_oa_location,primary_location"}
        if year:
            params["filter"] += ",publication_year:" + "|".join(str(y) for y in range(year - 1, year + 4))
        if self.key:
            params["api_key"] = self.key
        self._wait()
        resp = self.session.get(OPENALEX_URL, params=params, timeout=60, headers={"User-Agent": UA})
        if resp.status_code == 429:
            raise OpenAlexPaused(resp.headers.get("Retry-After", ""))
        if resp.status_code != 200:
            raise requests.RequestException(f"openalex status {resp.status_code}")
        works = resp.json().get("results") or []
        return [w for w in works if title_match(title, w.get("title"))]


def pause_left(conn):
    """Seconds left of the pipeline's persisted OpenAlex 429 pause."""
    try:
        row = conn.execute("SELECT value FROM scheduler_state WHERE key=?", (OPENALEX_PAUSE_KEY,)).fetchone()
        return max(0.0, float(row[0]) - time.time()) if row else 0.0
    except (sqlite3.Error, TypeError, ValueError):
        return 0.0


def save_plan(path, plan):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(plan, f, indent=1)
    os.replace(tmp, path)


def probe(conn, oa, args, log=print):
    tagged = candidates(conn, args.twins, args.limit, args.canon_only)
    plan = {"items": []}
    if args.resume and os.path.exists(args.plan):
        with open(args.plan) as f:
            plan = json.load(f)
    have = {i["id"] for i in plan["items"]}
    todo = [t for t in tagged if t[1]["arxiv_id"] not in have]
    log(f"{len(tagged)} candidates, {len(todo)} to probe")
    stopped = None
    for n, (canon, row) in enumerate(todo, 1):
        try:
            works = oa.find(row["title"], row["year"])
        except OpenAlexPaused as e:
            stopped = f"429 (Retry-After {e}); stopping, rerun with --resume later"
            break
        except (requests.RequestException, ValueError) as e:
            log(f"error {row['arxiv_id']}: {type(e).__name__}")
            continue
        entry = build_entry(row, works, canon)
        plan["items"].append(entry)
        log(f"{row['arxiv_id']} {entry['action']} {entry['host'] or '-'}")
        if n % 25 == 0:
            save_plan(args.plan, plan)
    plan["built"] = rlb.now_iso()
    save_plan(args.plan, plan)
    log(f"plan: {len(plan['items'])} items -> {args.plan}" + (f"; {stopped}" if stopped else ""))
    return plan


# --------------------------------------------------------------------------
# polite downloading
# --------------------------------------------------------------------------

class Fetcher:
    def __init__(self, session, sleep=time.sleep, clock=time.monotonic):
        self.session, self._sleep, self._clock = session, sleep, clock
        self._last = {}
        self._robots = {}

    def _throttle(self, host):
        last = self._last.get(host)
        if last is not None:
            delta = HOST_PAUSE - (self._clock() - last)
            if delta > 0:
                self._sleep(delta)
        self._last[host] = self._clock()

    def _get(self, url, **kw):
        self._throttle(host_of(url))
        return self.session.get(url, timeout=30, headers={"User-Agent": UA},
                                allow_redirects=False, **kw)

    def allowed(self, url):
        if is_blocked_host(url):
            return False
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if host not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                resp = self._get(f"{parts.scheme}://{parts.netloc}/robots.txt")
                if resp.status_code in (404, 410):
                    rp.parse([])
                elif resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:  # 3xx, 401/403, 5xx: unknown, so no
                    rp = None
            except requests.RequestException:
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        return rp is not None and rp.can_fetch(ROBOTS_TOKEN, url)

    def pdf(self, url):
        """Return PDF bytes or raise ValueError with the reason."""
        for _ in range(MAX_REDIRECTS + 1):
            if not self.allowed(url):
                raise ValueError(f"blocked by host rule or robots: {host_of(url)}")
            resp = self._get(url, stream=True)
            try:
                if resp.status_code in (301, 302, 303, 307, 308):
                    url = urllib.parse.urljoin(url, resp.headers.get("Location", ""))
                    continue
                if resp.status_code != 200:
                    raise ValueError(f"status {resp.status_code}")
                if int(resp.headers.get("Content-Length") or 0) > MAX_PDF_BYTES:
                    raise ValueError("pdf too large")
                buf = bytearray()
                for chunk in resp.iter_content(65536):
                    buf += chunk
                    if len(buf) > MAX_PDF_BYTES:
                        raise ValueError("pdf too large")
                if bytes(buf[:4]) != b"%PDF":
                    raise ValueError("not a pdf")
                return bytes(buf)
            finally:
                with contextlib.suppress(Exception):
                    resp.close()
        raise ValueError("too many redirects")


def text_opens_with_title(text, title):
    """Guard against a mirror serving another paper: the first words of the
    title must occur on the first page of the text."""
    head = norm_title(text[:4000])
    words = norm_title(title).split()[:6]
    return bool(words) and " ".join(words) in head


# --------------------------------------------------------------------------
# applying the plan
# --------------------------------------------------------------------------

def queue_arxiv(conn, entry, now=None):
    """Put the arXiv twin into the normal pipeline. No download here."""
    aid = entry["arxiv_id"]
    have = conn.execute("SELECT twin_of FROM papers WHERE arxiv_id=?", (aid,)).fetchone()
    if have is None:
        src = conn.execute("SELECT layers, abstract, niche_score, matched_terms FROM papers WHERE arxiv_id=?",
                           (entry["id"],)).fetchone()
        conn.execute(
            "INSERT INTO papers (arxiv_id, title, year, layers, status, abstract, niche_score, "
            "matched_terms, twin_of, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, entry["title"], entry["year"], src["layers"] or "web3", "discovered",
             src["abstract"], src["niche_score"], src["matched_terms"], entry["id"], now or rlb.now_iso()))
        outcome = "queued"
    else:
        if have["twin_of"] is None:
            conn.execute("UPDATE papers SET twin_of=? WHERE arxiv_id=?", (entry["id"], aid))
        outcome = "twin_in_corpus"
    conn.commit()
    return outcome


def write_text(data_dir, paper_id, text):
    d = os.path.join(data_dir, "fulltext_cache")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{rlb.safe_id(paper_id)}.tex")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def apply_pdf(conn, session, fetcher, args, entry, fts_ids, to_text, log=print):
    """Returns (outcome, url). Nothing is changed unless a copy yields good text."""
    pid = entry["id"]
    text, url = None, None
    tries = [c for c in entry["copies"] if c["kind"] == "pdf" and c["pdf"]][:args.max_copies]
    for c in tries:
        try:
            raw = fetcher.pdf(c["url"])
            cand = to_text(raw)
        except (ValueError, requests.RequestException) as e:
            log(f"  {pid} copy {c['host']}: {e}")
            continue
        except Exception as e:  # pdf parser failures
            log(f"  {pid} copy {c['host']}: parse failed {type(e).__name__}")
            continue
        if len(cand or "") < MIN_TEXT_CHARS:
            log(f"  {pid} copy {c['host']}: text too short")
            continue
        if not text_opens_with_title(cand, entry["title"]):
            log(f"  {pid} copy {c['host']}: title not on first page, rejected")
            continue
        text, url = cand, c["url"]
        break
    if text is None:
        return "no_usable_copy", None
    write_text(args.data_dir, pid, text)
    rlb.qdrant_delete(session, args.qdrant, args.collection, pid)
    rlb.fts_delete(args.fts, fts_ids)
    sid = rlb.safe_id(pid)
    with contextlib.suppress(FileNotFoundError):
        os.remove(os.path.join(args.data_dir, "chunk_cache", f"{sid}.json"))
    with contextlib.suppress(OSError):
        os.remove(os.path.join(args.data_dir, "latex_cache", sid, "source.tex"))
        os.rmdir(os.path.join(args.data_dir, "latex_cache", sid))
    conn.execute("UPDATE papers SET status='fulltext_fetched', fulltext_source=?, source_url=?, "
                 "extractor_version=NULL, updated_at=? WHERE arxiv_id=? AND status='done' "
                 "AND fulltext_source='iacr-abstract'",
                 (FULLTEXT_SOURCE, url, rlb.now_iso(), pid))
    conn.commit()
    return "ok", url


def service_supports_mirror(path):
    """The deployed service.py must chunk oa-mirror-pdf as markdown."""
    if not path or not os.path.exists(path):
        return True
    with open(path, encoding="utf-8", errors="ignore") as f:
        return FULLTEXT_SOURCE in f.read()


def default_to_text(raw):
    sys.path.insert(0, DATA_DIR)
    from whitepapers import pdf_to_text  # same converter the whitepaper shelf uses
    return pdf_to_text(raw)


def apply_plan(conn, session, args, to_text=None, fetcher=None, log=print):
    with open(args.plan) as f:
        items = json.load(f)["items"]
    done = rlb.load_progress(args.progress)
    items = [i for i in items if i["id"] not in done and i["action"] in ("pdf", "queue_arxiv")]
    pdf_ok = service_supports_mirror(args.service_file)
    if not pdf_ok:
        log(f"{args.service_file} lacks '{FULLTEXT_SOURCE}' support: PDF items skipped, deploy service.py first")
    room_pdf = args.limit or 10**9
    if args.feed:
        queue = rlb.queue_size(conn)
        room_pdf = min(args.target - queue, args.batch, room_pdf)
        log(f"queue {queue}, target {args.target}: room for {max(room_pdf, 0)} PDF papers")
    room_arxiv = min(args.arxiv_batch, args.limit or 10**9)
    chosen, np_, na = [], 0, 0
    for it in items:
        if it["action"] == "pdf":
            if not pdf_ok or np_ >= room_pdf:
                continue
            np_ += 1
        else:
            if na >= room_arxiv:
                continue
            na += 1
        chosen.append(it)
    verb = "apply" if args.apply else "would apply"
    if not args.apply:
        for it in chosen:
            log(f"{verb} {it['id']} {it['action']} {it['host']} {it['source_url']}")
        log(f"{verb} {len(chosen)}")
        return len(chosen)
    fetcher = fetcher or Fetcher(session)
    to_text = to_text or default_to_text
    pdf_ids = [i["id"] for i in chosen if i["action"] == "pdf"]
    rowids = {} if args.skip_fts else rlb.fts_rowids(args.fts, pdf_ids)
    n = 0
    for it in chosen:
        pid = it["id"]
        row = conn.execute("SELECT status, fulltext_source FROM papers WHERE arxiv_id=?", (pid,)).fetchone()
        if row is None or row["status"] != "done" or row["fulltext_source"] != "iacr-abstract":
            rlb.mark_progress(args.progress, pid)
            log(f"skipped {pid}: not an abstract-only 'done' paper any more")
            continue
        if conn.execute("SELECT 1 FROM papers WHERE twin_of=?", (pid,)).fetchone():
            rlb.mark_progress(args.progress, pid)
            log(f"skipped {pid}: has an arXiv twin")
            continue
        try:
            if it["action"] == "queue_arxiv":
                outcome = queue_arxiv(conn, it)
            else:
                outcome, _ = apply_pdf(conn, session, fetcher, args, it, rowids.get(pid, []), to_text, log)
        except (requests.RequestException, sqlite3.Error, OSError) as e:
            log(f"failed {pid}: {type(e).__name__}: {e}")  # not marked: retried next run
            continue
        rlb.mark_progress(args.progress, pid)
        n += outcome in ("ok", "queued", "twin_in_corpus")
        log(f"{outcome} {pid} {it['source_url'] or ''}")
    log(f"{verb} {n} of {len(chosen)}")
    return n


def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--feed", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--canon-only", action="store_true")
    ap.add_argument("--db", default=os.path.join(DATA_DIR, "state.db"))
    ap.add_argument("--fts", default=os.getenv("FTS_DB_PATH", os.path.join(DATA_DIR, "fts.db")))
    ap.add_argument("--data-dir", default=DATA_DIR)
    ap.add_argument("--twins", default=os.path.join(DATA_DIR, "twins.json"))
    ap.add_argument("--qdrant", default=os.getenv("QDRANT_URL", "http://127.0.0.1:16335"))
    ap.add_argument("--collection", default=os.getenv("QDRANT_COLLECTION", "papers_fulltext"))
    ap.add_argument("--plan", default=os.path.join(DATA_DIR, "iacr_oa_plan.json"))
    ap.add_argument("--progress", default=os.path.join(DATA_DIR, "iacr_oa_progress.jsonl"))
    ap.add_argument("--lock", default=os.path.join(DATA_DIR, "iacr_oa_fulltext.lock"))
    ap.add_argument("--service-file", default=os.path.join(DATA_DIR, "service.py"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--target", type=int, default=300)
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--arxiv-batch", type=int, default=50)
    ap.add_argument("--max-copies", type=int, default=3)
    ap.add_argument("--skip-fts", action="store_true")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    with rlb.single_instance(args.lock) as got:
        if not got:
            print("another run holds the lock, exiting", flush=True)
            return 0
        conn = rlb.connect(args.db)
        session = requests.Session()
        try:
            if args.probe:
                left = pause_left(conn)
                if left > 0:
                    print(f"OpenAlex is paused by the pipeline for another {int(left)} s, exiting", flush=True)
                    return 0
                probe(conn, OpenAlex(session, os.getenv("OPENALEX_API_KEY", "")), args)
            else:
                apply_plan(conn, session, args)
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
