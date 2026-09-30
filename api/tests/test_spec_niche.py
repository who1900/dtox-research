import pathlib
import sqlite3
import sys
import types
import unittest
from unittest.mock import patch

# pipeline/service.py imports its sibling modules (extractor, niche_filter) as
# bare top-level names, so it only imports cleanly when pipeline/ itself is on
# sys.path (how the deployed service is run) -- pipeline.service is not enough
# on its own. Test-only path fixup; nothing under pipeline/ is changed.
_PIPELINE_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "pipeline")
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

# pipeline/service.py does `import fcntl` at module scope for its single-instance
# lock (used only in main(), never in the code paths under test here). fcntl is
# POSIX-only, so importing pipeline.service on a Windows dev box fails before
# any of our code even runs. This installs a no-op stub so the module can be
# imported for testing on any platform; on a real POSIX host the real fcntl is
# already present and this branch is skipped entirely.
if "fcntl" not in sys.modules:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        stub = types.ModuleType("fcntl")
        stub.flock = lambda *a, **kw: None
        stub.LOCK_EX = 2
        stub.LOCK_NB = 4
        sys.modules["fcntl"] = stub

from pipeline import service  # noqa: E402


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    service.init_db(conn)
    return conn


class SpecsAreAlwaysWeb3Tests(unittest.TestCase):
    TITLE = "Fee market change for ETH 1.0 chain"
    ABSTRACT = "A transaction pricing mechanism with a fixed per-block fee that is burned."

    def _row(self, conn, pid):
        return conn.execute("SELECT status, layers FROM papers WHERE arxiv_id=?", (pid,)).fetchone()

    def test_eip_without_niche_words_is_admitted_as_web3(self):
        conn = make_conn()
        service.upsert_discovered(conn, "eip:1559", self.TITLE, 2019, "web3", abstract=self.ABSTRACT)
        row = self._row(conn, "eip:1559")
        self.assertEqual(row["status"], "discovered")
        self.assertIn("web3", row["layers"].split(","))

    def test_off_niche_eip_is_resurrected_on_reread(self):
        conn = make_conn()
        conn.execute("INSERT INTO papers (arxiv_id, title, layers, status) VALUES ('simd:0085','x','','off_niche')")
        service.upsert_discovered(conn, "simd:0085", "Fee collector constraints", 2023, "web3",
                                  abstract="Validators must keep the collector account rent exempt.")
        self.assertEqual(self._row(conn, "simd:0085")["status"], "discovered")

    def test_arxiv_paper_still_goes_through_the_gate(self):
        conn = make_conn()
        service.upsert_discovered(conn, "2401.99999", "Cooking pasta at altitude", 2024, "web3",
                                  abstract="We measure boiling points of water in mountain kitchens.")
        self.assertEqual(self._row(conn, "2401.99999")["status"], "off_niche")


class GithubDocsNicheTests(unittest.TestCase):
    ABSTRACT = "How durable nonces work for offline signing and delayed transaction submission."

    def _row(self, conn, pid):
        return conn.execute("SELECT status, layers FROM papers WHERE arxiv_id=?", (pid,)).fetchone()

    def test_solana_gh_doc_is_web3_without_niche_words(self):
        conn = make_conn()
        pid = "gh:solana-docs:122a8c29499d39a9"
        service.upsert_discovered(conn, pid, "Durable Nonces", None, "web3", abstract=self.ABSTRACT)
        row = self._row(conn, pid)
        self.assertEqual(row["status"], "discovered")
        self.assertIn("web3", row["layers"].split(","))

    def test_unflagged_gh_source_still_goes_through_gate(self):
        conn = make_conn()
        pid = "gh:nitro:0123456789abcdef"
        service.upsert_discovered(conn, pid, "Cooking pasta at altitude", None, "web3",
                                  abstract="We measure boiling points of water in mountain kitchens.")
        self.assertEqual(self._row(conn, pid)["status"], "off_niche")

    def test_forced_web3(self):
        self.assertTrue(service.forced_web3("gh:jupiter:abc123"))
        self.assertTrue(service.forced_web3("wp:aave-v3"))
        self.assertFalse(service.forced_web3("gh:nitro:abc123"))
        self.assertFalse(service.forced_web3("gh:unknown-source:abc123"))
        self.assertFalse(service.forced_web3("2401.00001"))

    def test_manifest_entries(self):
        for key in ("solana-docs", "jupiter", "meteora", "uniswap", "compound-comet", "anchor",
                    "anza-docs", "yellowstone-grpc"):
            cfg = service.GITHUB_DOC_SOURCES[key]
            self.assertTrue(cfg.get("web3"), key)
            self.assertTrue(cfg["repo"] and cfg["paths"], key)
        # solana-com ships 19 machine translations beside en/: only en/ may be listed
        for path in service.GITHUB_DOC_SOURCES["solana-docs"]["paths"]:
            self.assertTrue(path.startswith(("apps/docs/content/docs/en/", "apps/docs/content/cookbook/")), path)

    def test_geyser_and_yellowstone_sources(self):
        anza = service.GITHUB_DOC_SOURCES["anza-docs"]
        self.assertEqual(anza["repo"], "anza-xyz/docs.anza.xyz")
        self.assertIn("src/validator/", anza["paths"])  # holds geyser.md
        ys = service.GITHUB_DOC_SOURCES["yellowstone-grpc"]
        self.assertEqual(ys["repo"], "rpcpool/yellowstone-grpc")
        self.assertIn("README.md", ys["paths"])
        self.assertTrue(service.forced_web3("gh:anza-docs:0123456789abcdef"))
        self.assertTrue(service.forced_web3("gh:yellowstone-grpc:0123456789abcdef"))

    NEW_SOURCES = ("flashbots-docs", "builder-specs", "mev-share", "suave-docs", "foundry-book",
                   "openzeppelin-docs", "solidity-docs", "slither", "chainlink-docs", "aave-v3-origin",
                   "morpho-blue", "morpho-vault-v2", "metamorpho", "cow-protocol", "zksync-docs",
                   "starknet-docs", "starkex-resources")

    def test_builder_defi_tooling_sources(self):
        for key in self.NEW_SOURCES:
            cfg = service.GITHUB_DOC_SOURCES[key]
            self.assertTrue(cfg.get("web3"), key)
            self.assertRegex(cfg["repo"], r"^[\w.-]+/[\w.-]+$")
            self.assertTrue(cfg["paths"] and cfg["branch"], key)
            self.assertTrue(service.forced_web3(f"gh:{key}:0123456789abcdef"), key)
            # no translated trees: only English content is listed
            for path in cfg["paths"]:
                self.assertNotRegex(path, r"/(fr|fil|pt|ja|zh|zh_t|th|de|vi|tr|kr|id|es|ru)/", (key, path))
        self.assertEqual(service.GITHUB_DOC_SOURCES["solidity-docs"]["exts"], (".rst",))
        self.assertIn("src/pages/forge/linting/", service.GITHUB_DOC_SOURCES["foundry-book"]["exclude"])
        self.assertIn("src/operations/", service.GITHUB_DOC_SOURCES["anza-docs"]["paths"])
        self.assertIn("docs/", service.GITHUB_DOC_SOURCES["compound-comet"]["paths"])

    def test_tree_filter_extensions_and_exclude(self):
        import io
        import json
        tree = {"tree": [{"type": "blob", "path": p} for p in (
            "docs/a.rst", "docs/b.md", "docs/brand-guide.rst", "docs/sub/c.rst", "test/x.rst", "docs/conf.py")]}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        service._github_tree_cache.pop("solidity-docs", None)
        with patch.object(service.urllib.request, "urlopen", return_value=Resp(json.dumps(tree).encode())):
            paths = service.github_doc_files("solidity-docs")
        service._github_tree_cache.pop("solidity-docs", None)
        self.assertEqual(paths, ["docs/a.rst", "docs/sub/c.rst"])

    def test_rst_to_markdown(self):
        rst = "\n".join([
            ".. index:: ! visibility", "", ".. _label:", "", "*****", "Title", "*****", "",
            "A ``public`` var, see :ref:`the guide <x>`.", "", "Example::", "", "    uint x;", "",
            "Sub", "===", "", ".. code-block:: solidity", "    :force:", "", "    uint y;", ""])
        md = service.rst_to_markdown(rst)
        self.assertIn("# Title", md)
        self.assertIn("## Sub", md)
        self.assertIn("`public`", md)
        self.assertIn("`the guide`", md)
        self.assertIn("```solidity\nuint y;\n```", md)
        self.assertIn("```\nuint x;\n```", md)
        self.assertNotIn("index::", md)
        self.assertNotIn(":force:", md)
        self.assertEqual(service._github_doc_title(md, "docs/x.rst"), "Title")

    def test_title_prefers_front_matter(self):
        text = "---\ntitle: Durable Nonces\n---\n\n```bash\n# not a title\n```\n"
        self.assertEqual(service._github_doc_title(text, "x/durable-nonces.mdx"), "Durable Nonces")
        self.assertEqual(service._github_doc_title("intro\n# Real Title\n", "a.md"), "Real Title")

    def test_whitepapers_skip_semantic_scholar_gate(self):
        self.assertTrue(service.is_non_arxiv("wp:aave"))


class WhitepaperListTests(unittest.TestCase):
    def test_slugs_unique_and_urls_https(self):
        import whitepapers
        slugs = [w[0] for w in whitepapers.WHITEPAPERS]
        self.assertEqual(len(slugs), len(set(slugs)))
        for slug in ("uniswap-v4", "curve-cryptoswap", "aave-v2", "aave-v3", "chainlink-v1", "pyth", "risc0-proof-system"):
            self.assertIn(slug, slugs)
        for _slug, _title, year, url in whitepapers.WHITEPAPERS:
            self.assertTrue(1990 < year < 2030)
            self.assertTrue(url.startswith(("https://", "http://")))


if __name__ == "__main__":
    unittest.main()
