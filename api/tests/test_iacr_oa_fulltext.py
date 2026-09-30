import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock

_OPS_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "ops")
if _OPS_DIR not in sys.path:
    sys.path.insert(0, _OPS_DIR)
import iacr_oa_fulltext as io_  # noqa: E402

BODY = "Fake Paper Title On Something\n\n" + ("lorem ipsum dolor sit amet " * 200)


def loc(pdf=None, landing=None, oa=True, typ="repository", lic=None, name="Repo"):
    return {"is_oa": oa, "pdf_url": pdf, "landing_page_url": landing, "license": lic,
            "source": {"type": typ, "display_name": name}}


def work(title, locs, doi=None):
    return {"title": title, "doi": doi, "locations": locs}


class Resp:
    def __init__(self, status=200, text="", headers=None, content=b"", json_data=None):
        self.status_code, self.text, self.headers = status, text, headers or {}
        self._content, self._json = content, json_data

    def iter_content(self, n):
        for i in range(0, len(self._content), n):
            yield self._content[i:i + n]

    def json(self):
        return self._json

    def close(self):
        pass


class FakeSession:
    """url -> Resp or list of Resp; records every requested URL."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, **kw):
        self.calls.append(url)
        r = self.routes.get(url)
        if r is None:
            return Resp(404)
        return r.pop(0) if isinstance(r, list) else r


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.db = os.path.join(self.dir, "state.db")
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE papers (arxiv_id TEXT PRIMARY KEY, title TEXT, year INTEGER, layers TEXT, "
                  "status TEXT, abstract TEXT, niche_score REAL, matched_terms TEXT, source_url TEXT, "
                  "fulltext_source TEXT, extractor_version INTEGER, twin_of TEXT, updated_at TEXT)")
        c.execute("CREATE TABLE scheduler_state (key TEXT PRIMARY KEY, value TEXT)")
        c.commit()
        c.close()
        self.plan = os.path.join(self.dir, "plan.json")
        self.progress = os.path.join(self.dir, "progress.jsonl")
        self.twins = os.path.join(self.dir, "twins.json")

    def conn(self):
        c = io_.rlb.connect(self.db)
        self.addCleanup(c.close)
        return c

    def paper(self, pid, title="Fake Paper Title On Something", year=2020, score=5, status="done",
              src="iacr-abstract", twin_of=None, layers="web3"):
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO papers (arxiv_id,title,year,layers,status,abstract,niche_score,fulltext_source,twin_of)"
                  " VALUES (?,?,?,?,?,?,?,?,?)", (pid, title, year, layers, status, "abs", score, src, twin_of))
        c.commit()
        c.close()

    def row(self, pid):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM papers WHERE arxiv_id=?", (pid,)).fetchone()
        c.close()
        return r

    def args(self, *extra):
        return io_.parse_args(["--db", self.db, "--fts", os.path.join(self.dir, "fts.db"),
                               "--data-dir", self.dir, "--twins", self.twins, "--plan", self.plan,
                               "--progress", self.progress, "--lock", os.path.join(self.dir, "x.lock"),
                               "--service-file", os.path.join(self.dir, "nosuch.py"), *extra])


class HostFilter(unittest.TestCase):
    def test_iacr_hosts_blocked(self):
        for u in ("https://eprint.iacr.org/2019/953.pdf", "http://iacr.org/x", "https://www.iacr.org/a",
                  "https://EPRINT.IACR.ORG/1", "/relative", ""):
            self.assertTrue(io_.is_blocked_host(u), u)
        for u in ("https://hal.science/x.pdf", "https://notiacr.org/a", "https://iacr.org.evil.com/a",
                  "https://zenodo.org/r.pdf"):
            self.assertEqual(io_.is_blocked_host(u), u == "", u)

    def test_iacr_copies_dropped_from_work(self):
        w = work("T", [loc(pdf="https://eprint.iacr.org/2016/260.pdf"),
                       loc(landing="https://eprint.iacr.org/2016/260"),
                       loc(pdf="https://hal.science/a.pdf")])
        self.assertEqual([c["host"] for c in io_.copies_from_work(w)], ["hal.science"])

    def test_non_oa_and_bad_hosts_dropped(self):
        w = work("T", [loc(pdf="https://x.org/a.pdf", oa=False),
                       loc(pdf="https://www.researchgate.net/a.pdf")])
        self.assertEqual(io_.copies_from_work(w), [])


class Priority(Env):
    def test_arxiv_then_repo_then_other_and_pdf_before_landing(self):
        w = work("Fake Paper Title On Something", [
            loc(pdf="https://someone.example/p.pdf", typ="other"),
            loc(pdf="https://hal.science/p.pdf", lic="cc-by"),
            loc(landing="https://zenodo.org/record/1"),
            loc(landing="https://arxiv.org/abs/2001.01234v2", pdf="https://arxiv.org/pdf/2001.01234v2"),
        ])
        e = io_.build_entry({"arxiv_id": "iacr:2020/1", "title": w["title"], "year": 2020, "niche_score": 1},
                            [w], False)
        self.assertEqual([c["tier"] for c in e["copies"]], [0, 1, 1, 2])
        self.assertEqual((e["action"], e["arxiv_id"]), ("queue_arxiv", "2001.01234"))

    def test_repository_before_author_site_and_license_kept(self):
        w = work("T", [loc(pdf="https://me.example/p.pdf", typ="other"),
                       loc(pdf="https://hal.science/p.pdf", lic="cc-by")])
        e = io_.build_entry({"arxiv_id": "iacr:2020/1", "title": "T", "year": 2020, "niche_score": 1}, [w], False)
        self.assertEqual((e["action"], e["host"], e["license"]), ("pdf", "hal.science", "cc-by"))

    def test_landing_only_is_not_downloadable(self):
        w = work("T", [loc(landing="https://zenodo.org/record/1")])
        e = io_.build_entry({"arxiv_id": "iacr:2020/1", "title": "T", "year": 2020, "niche_score": 1}, [w], False)
        self.assertEqual(e["action"], "none")
        self.assertEqual(len(e["copies"]), 1)

    def test_arxiv_doi_gives_arxiv_copy(self):
        w = work("T", [], doi="https://doi.org/10.48550/arXiv.2102.03333")
        e = io_.build_entry({"arxiv_id": "iacr:2020/1", "title": "T", "year": 2020, "niche_score": 1}, [w], False)
        self.assertEqual((e["action"], e["arxiv_id"]), ("queue_arxiv", "2102.03333"))

    def test_title_match_rules(self):
        self.assertTrue(io_.title_match("Nova: Recursive Zero-Knowledge Arguments", "nova recursive zero knowledge arguments"))
        self.assertTrue(io_.title_match("Spartan: Efficient and general-purpose zkSNARKs",
                                        "Spartan: Efficient and general-purpose zkSNARKs without trusted setup"))
        self.assertFalse(io_.title_match("Short title", "Short title of a different paper entirely"))

    def test_candidate_order_canon_then_score_then_year(self):
        self.paper("iacr:2019/953", title="PLONK: something", score=1)
        self.paper("iacr:2020/1", score=9, year=2020)
        self.paper("iacr:2021/1", score=9, year=2021)
        self.paper("iacr:2018/1", score=3)
        got = [r["arxiv_id"] for _, r in io_.candidates(self.conn(), self.twins)]
        self.assertEqual(got, ["iacr:2019/953", "iacr:2021/1", "iacr:2020/1", "iacr:2018/1"])
        self.assertEqual(len(io_.candidates(self.conn(), self.twins, limit=2)), 2)
        self.assertEqual(len(io_.candidates(self.conn(), self.twins, canon_only=True)), 1)


class Twins(Env):
    def test_twinned_papers_skipped(self):
        self.paper("iacr:2020/1")            # twin_of on the arXiv row
        self.paper("2001.00001", src="latex", twin_of="iacr:2020/1")
        self.paper("iacr:2020/2")            # twins.json says so
        self.paper("iacr:2020/3")            # free
        self.paper("iacr:2020/4", src="oa-mirror-pdf")  # not abstract-only
        with open(self.twins, "w") as f:
            json.dump({"canonical": {"iacr:2020/2": "2001.00002", "2001.00002": "2001.00002",
                                     "iacr:2020/3": "iacr:2020/3"}}, f)
        got = [r["arxiv_id"] for _, r in io_.candidates(self.conn(), self.twins)]
        self.assertEqual(got, ["iacr:2020/3"])


class Probe(Env):
    def test_probe_builds_plan_and_never_writes_state(self):
        self.paper("iacr:2020/1")
        self.paper("iacr:2020/2", title="Another Long Unmatched Title Here", score=1)
        results = [work("Fake Paper Title On Something", [loc(pdf="https://hal.science/a.pdf"),
                                                          loc(pdf="https://eprint.iacr.org/2020/1.pdf")]),
                   work("Totally different work", [loc(pdf="https://zenodo.org/b.pdf")])]
        sess = FakeSession({io_.OPENALEX_URL: Resp(json_data={"results": results})})
        oa = io_.OpenAlex(sess, sleep=lambda s: None)
        before = open(self.db, "rb").read()
        plan = io_.probe(self.conn(), oa, self.args(), log=lambda *a: None)
        self.assertEqual(open(self.db, "rb").read(), before)
        by = {i["id"]: i for i in plan["items"]}
        self.assertEqual(by["iacr:2020/1"]["host"], "hal.science")
        self.assertEqual(by["iacr:2020/2"]["action"], "none")
        self.assertTrue(all("iacr.org" not in c["url"] for i in plan["items"] for c in i["copies"]))
        self.assertTrue(all(u.startswith("https://api.openalex.org") for u in sess.calls))
        self.assertTrue(os.path.exists(self.plan))

    def test_429_stops_and_keeps_partial_plan(self):
        self.paper("iacr:2020/1")
        self.paper("iacr:2020/2")
        sess = FakeSession({io_.OPENALEX_URL: [Resp(json_data={"results": []}), Resp(429, headers={"Retry-After": "60"})]})
        plan = io_.probe(self.conn(), io_.OpenAlex(sess, sleep=lambda s: None), self.args(), log=lambda *a: None)
        self.assertEqual(len(plan["items"]), 1)
        self.assertEqual(len(sess.calls), 2)

    def test_pause_left_reads_pipeline_key(self):
        c = self.conn()
        self.assertEqual(io_.pause_left(c), 0.0)
        c.execute("INSERT INTO scheduler_state VALUES (?,?)", (io_.OPENALEX_PAUSE_KEY, str(time.time() + 100)))
        c.commit()
        self.assertGreater(io_.pause_left(c), 90)

    def test_api_key_goes_only_to_params(self):
        seen = {}

        class S:
            def get(self, url, params=None, **kw):
                seen.update(params)
                return Resp(json_data={"results": []})
        io_.OpenAlex(S(), api_key="SECRET", sleep=lambda s: None).find("Some Title Words", 2020)
        self.assertEqual(seen["api_key"], "SECRET")
        self.assertIn("publication_year:2019|2020", seen["filter"])


class RobotsAndFetch(unittest.TestCase):
    def fetcher(self, routes):
        sess = FakeSession(routes)
        return io_.Fetcher(sess, sleep=lambda s: None), sess

    def test_disallowed_by_robots_not_fetched(self):
        f, s = self.fetcher({"https://h.example/robots.txt": Resp(200, text="User-agent: *\nDisallow: /pdf/"),
                             "https://h.example/pdf/a.pdf": Resp(200, content=b"%PDF-1")})
        with self.assertRaises(ValueError):
            f.pdf("https://h.example/pdf/a.pdf")
        self.assertNotIn("https://h.example/pdf/a.pdf", s.calls)

    def test_missing_robots_allows_and_is_cached(self):
        f, s = self.fetcher({"https://h.example/a.pdf": Resp(200, content=b"%PDF-1 data"),
                             "https://h.example/b.pdf": Resp(200, content=b"%PDF-1 data")})
        self.assertEqual(f.pdf("https://h.example/a.pdf")[:4], b"%PDF")
        f.pdf("https://h.example/b.pdf")
        self.assertEqual(s.calls.count("https://h.example/robots.txt"), 1)

    def test_robots_forbidden_or_error_means_no(self):
        f, s = self.fetcher({"https://h.example/robots.txt": Resp(403),
                             "https://h.example/a.pdf": Resp(200, content=b"%PDF-1")})
        with self.assertRaises(ValueError):
            f.pdf("https://h.example/a.pdf")

    def test_own_user_agent_token_rule_and_redirect_to_iacr_blocked(self):
        f, s = self.fetcher({"https://h.example/robots.txt": Resp(404),
                             "https://h.example/a.pdf": Resp(302, headers={"Location": "https://eprint.iacr.org/1.pdf"})})
        with self.assertRaises(ValueError):
            f.pdf("https://h.example/a.pdf")
        self.assertFalse(any("iacr.org" in u for u in s.calls))

    def test_size_cap_and_not_pdf(self):
        f, _ = self.fetcher({"https://h.example/robots.txt": Resp(404),
                             "https://h.example/big.pdf": Resp(200, headers={"Content-Length": str(io_.MAX_PDF_BYTES + 1)}),
                             "https://h.example/html.pdf": Resp(200, content=b"<html>")})
        with self.assertRaises(ValueError):
            f.pdf("https://h.example/big.pdf")
        with self.assertRaises(ValueError):
            f.pdf("https://h.example/html.pdf")

    def test_three_second_pause_per_host(self):
        sleeps, now = [], [100.0]
        sess = FakeSession({"https://h.example/robots.txt": Resp(404),
                            "https://h.example/a.pdf": Resp(200, content=b"%PDF-1")})
        f = io_.Fetcher(sess, sleep=lambda s: sleeps.append(s), clock=lambda: now[0])
        f.pdf("https://h.example/a.pdf")
        self.assertTrue(sleeps and sleeps[0] >= 2.9)


class Apply(Env):
    def plan_with(self, items):
        with open(self.plan, "w") as f:
            json.dump({"items": items}, f)

    def item(self, pid, action="pdf", url="https://h.example/a.pdf", arxiv=None, title="Fake Paper Title On Something"):
        return {"id": pid, "title": title, "year": 2020, "niche_score": 1, "canon": False, "action": action,
                "arxiv_id": arxiv, "source_url": url, "host": "h.example", "license": None,
                "copies": [{"tier": 1, "kind": "pdf", "arxiv_id": None, "url": url, "landing": None, "pdf": True,
                            "host": "h.example", "license": None, "source": "R"}]}

    def sess(self):
        return FakeSession({"https://h.example/robots.txt": Resp(404),
                            "https://h.example/a.pdf": Resp(200, content=b"%PDF-1 x"),
                            "http://qdrant/collections/c/points/delete": Resp(200)})

    def run_apply(self, *extra, to_text=None, sess=None):
        args = self.args("--qdrant", "http://qdrant", "--collection", "c", "--skip-fts", *extra)
        s = sess or self.sess()
        s.post = MagicMock()
        fetcher = io_.Fetcher(s, sleep=lambda x: None)
        n = io_.apply_plan(self.conn(), s, args, to_text=to_text or (lambda raw: BODY), fetcher=fetcher,
                           log=lambda *a: None)
        return n, s

    def test_dry_run_changes_nothing(self):
        self.paper("iacr:2020/1")
        self.plan_with([self.item("iacr:2020/1")])
        n, s = self.run_apply()
        self.assertEqual(n, 1)
        self.assertEqual(self.row("iacr:2020/1")["fulltext_source"], "iacr-abstract")
        self.assertEqual(s.calls, [])
        self.assertFalse(os.path.exists(self.progress))

    def test_apply_pdf_sets_fulltext_and_resets(self):
        self.paper("iacr:2020/1")
        os.makedirs(os.path.join(self.dir, "chunk_cache"))
        old = os.path.join(self.dir, "chunk_cache", "iacr_2020_1.json")
        open(old, "w").write("[]")
        self.plan_with([self.item("iacr:2020/1")])
        n, s = self.run_apply("--apply")
        r = self.row("iacr:2020/1")
        self.assertEqual((n, r["status"], r["fulltext_source"], r["source_url"]),
                         (1, "fulltext_fetched", "oa-mirror-pdf", "https://h.example/a.pdf"))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "fulltext_cache", "iacr_2020_1.tex")))
        self.assertFalse(os.path.exists(old))
        self.assertEqual(s.post.call_args[1]["json"]["filter"]["must"][0]["match"]["value"], "iacr:2020/1")
        self.assertIn("iacr:2020/1", io_.rlb.load_progress(self.progress))

    def test_wrong_paper_text_rejected_and_nothing_changed(self):
        self.paper("iacr:2020/1")
        self.plan_with([self.item("iacr:2020/1")])
        n, s = self.run_apply("--apply", to_text=lambda raw: "Some Other Paper\n\n" + "word " * 1000)
        self.assertEqual(n, 0)
        self.assertEqual(self.row("iacr:2020/1")["status"], "done")
        s.post.assert_not_called()

    def test_skip_when_twin_appeared_or_not_abstract_only(self):
        self.paper("iacr:2020/1")
        self.paper("2001.00001", src="latex", twin_of="iacr:2020/1")
        self.paper("iacr:2020/2", src="oa-mirror-pdf")
        self.plan_with([self.item("iacr:2020/1"), self.item("iacr:2020/2")])
        n, s = self.run_apply("--apply")
        self.assertEqual(n, 0)
        self.assertEqual(s.calls, [])
        self.assertEqual(self.row("iacr:2020/1")["fulltext_source"], "iacr-abstract")

    def test_arxiv_copy_queues_discovered_row_without_download(self):
        self.paper("iacr:2020/1", layers="web3,llm-slm")
        self.plan_with([self.item("iacr:2020/1", action="queue_arxiv", url="https://arxiv.org/abs/2001.09999",
                                  arxiv="2001.09999")])
        n, s = self.run_apply("--apply")
        r = self.row("2001.09999")
        self.assertEqual((n, r["status"], r["twin_of"], r["layers"]), (1, "discovered", "iacr:2020/1", "web3,llm-slm"))
        self.assertEqual(s.calls, [])
        self.assertEqual(self.row("iacr:2020/1")["fulltext_source"], "iacr-abstract")

    def test_arxiv_id_already_in_corpus_only_links_twin(self):
        self.paper("iacr:2020/1")
        self.paper("2001.09999", src="latex", status="done")
        self.plan_with([self.item("iacr:2020/1", action="queue_arxiv", url="https://arxiv.org/abs/2001.09999",
                                  arxiv="2001.09999")])
        self.run_apply("--apply")
        r = self.row("2001.09999")
        self.assertEqual((r["status"], r["twin_of"]), ("done", "iacr:2020/1"))

    def test_feed_limits_pdf_by_queue_room_and_batch(self):
        for i in range(1, 6):
            self.paper(f"iacr:2020/{i}")
        for i in range(3):
            self.paper(f"2002.0000{i}", status="chunked", src="latex")
        self.plan_with([self.item(f"iacr:2020/{i}") for i in range(1, 6)])
        n, _ = self.run_apply("--apply", "--feed", "--target", "5", "--batch", "10")
        self.assertEqual(n, 2)  # target 5 - queue 3
        n, _ = self.run_apply("--apply", "--feed", "--target", "3")
        self.assertEqual(n, 0)

    def test_pdf_items_refused_when_service_lacks_support(self):
        self.paper("iacr:2020/1")
        old_service = os.path.join(self.dir, "service.py")
        open(old_service, "w").write("# old service\n")
        self.plan_with([self.item("iacr:2020/1")])
        n, s = self.run_apply("--apply", "--service-file", old_service)
        self.assertEqual(n, 0)
        self.assertEqual(self.row("iacr:2020/1")["status"], "done")
        open(old_service, "w").write("if src == 'oa-mirror-pdf': pass\n")
        n, s = self.run_apply("--apply", "--service-file", old_service)
        self.assertEqual(n, 1)

    def test_limit(self):
        for i in range(1, 4):
            self.paper(f"iacr:2020/{i}")
        self.plan_with([self.item(f"iacr:2020/{i}") for i in range(1, 4)])
        n, _ = self.run_apply("--apply", "--limit", "2")
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main()
