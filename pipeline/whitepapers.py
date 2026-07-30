#!/usr/bin/env python3
"""Ingest the foundational documents of the blockchain industry.

These are the canon a practitioner actually cites -- Bitcoin, the Ethereum
Yellow Paper, the AMM papers -- and no automated harvester will ever find
them: they live on project websites, not on arXiv or IACR. So the list is
curated by hand and re-runnable; each document lands in the same pipeline as
everything else (id "wp:<slug>", layer web3, chunked and embedded normally).

Usage: python whitepapers.py [--dry-run]
"""
import io
import re
import sqlite3
import sys
import time
import urllib.request

sys.path.insert(0, "/opt/dtox-research")
import service  # noqa: E402

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# slug, title, year, url
WHITEPAPERS = [
    ("bitcoin", "Bitcoin: A Peer-to-Peer Electronic Cash System", 2008,
     "https://bitcoin.org/bitcoin.pdf"),
    ("ethereum-yellowpaper", "Ethereum: A Secure Decentralised Generalised Transaction Ledger (Yellow Paper)", 2014,
     "https://ethereum.github.io/yellowpaper/paper.pdf"),
    ("solana", "Solana: A new architecture for a high performance blockchain", 2017,
     "https://solana.com/solana-whitepaper.pdf"),
    ("lightning", "The Bitcoin Lightning Network: Scalable Off-Chain Instant Payments", 2016,
     "https://lightning.network/lightning-network-paper.pdf"),
    ("uniswap-v3", "Uniswap v3 Core", 2021,
     "https://uniswap.org/whitepaper-v3.pdf"),
    ("uniswap-v2", "Uniswap v2 Core", 2020,
     "https://uniswap.org/whitepaper.pdf"),
    ("chainlink-v2", "Chainlink 2.0: Next Steps in the Evolution of Decentralized Oracle Networks", 2021,
     "https://research.chain.link/whitepaper-v2.pdf"),
    ("cryptonote", "CryptoNote v2.0", 2013,
     "https://bytecoin.org/old/whitepaper.pdf"),
    ("zerocash", "Zerocash: Decentralized Anonymous Payments from Bitcoin", 2014,
     "http://zerocash-project.org/media/pdf/zerocash-extended-20140518.pdf"),
    ("mastercoin", "Tendermint: Consensus without Mining", 2014,
     "https://tendermint.com/static/docs/tendermint.pdf"),
    ("avalanche", "Avalanche: Scalable and Probabilistic Leaderless BFT Consensus", 2018,
     "https://raw.githubusercontent.com/ava-labs/avalanche-docs/master/static/papers/avalanche_consensus.pdf"),
    ("filecoin", "Filecoin: A Decentralized Storage Network", 2017,
     "https://filecoin.io/filecoin.pdf"),
    ("polkadot", "Polkadot: Vision for a Heterogeneous Multi-Chain Framework", 2016,
     "https://raw.githubusercontent.com/w3f/polkadot-white-paper/master/PolkaDotPaper.pdf"),
    ("maker", "The Maker Protocol: MakerDAO's Multi-Collateral Dai System", 2020,
     "https://makerdao.com/whitepaper/DaiDec17WP.pdf"),
    ("compound", "Compound: The Money Market Protocol", 2019,
     "https://compound.finance/documents/Compound.Whitepaper.pdf"),
    ("aave", "Aave Protocol Whitepaper", 2020,
     "https://github.com/aave/aave-protocol/raw/master/docs/Aave_Protocol_Whitepaper_v1_0.pdf"),
    ("curve", "StableSwap: Efficient Mechanism for Stablecoin Liquidity", 2019,
     "https://classic.curve.fi/files/stableswap-paper.pdf"),
    ("balancer", "Balancer: A Non-Custodial Portfolio Manager and Liquidity Provider", 2019,
     "https://balancer.fi/whitepaper.pdf"),
    ("flashbots-mev", "Flash Boys 2.0: Frontrunning, Transaction Reordering and Consensus Instability", 2019,
     "https://arxiv.org/pdf/1904.05234"),
    ("celestia", "LazyLedger: A Distributed Data Availability Ledger With Client-Side Smart Contracts", 2019,
     "https://arxiv.org/pdf/1905.09274"),
    ("eigenlayer", "EigenLayer: The Restaking Collective", 2023,
     "https://docs.eigenlayer.xyz/assets/files/whitepaper-88c5ba86e4b5cf0a2a3e4c6e0a3c9b5b.pdf"),
    ("arbitrum", "Arbitrum: Scalable, private smart contracts", 2018,
     "https://www.usenix.org/system/files/conference/usenixsecurity18/sec18-kalodner.pdf"),
    ("plasma", "Plasma: Scalable Autonomous Smart Contracts", 2017,
     "https://plasma.io/plasma.pdf"),
    ("monero-bulletproofs", "Bulletproofs: Short Proofs for Confidential Transactions and More", 2017,
     "https://eprint.iacr.org/2017/1066.pdf"),
    ("cosmos", "Cosmos: A Network of Distributed Ledgers", 2016,
     "https://raw.githubusercontent.com/cosmos/cosmos/master/WHITEPAPER.md"),
]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def normalize_pdf_text(text):
    """Undo PDF layout artefacts that wreck embedding quality.

    Column layout leaves hard line breaks mid-sentence and even mid-word
    ("proof-of-w\nork"), plus doubled spaces from justification. Left as is,
    the Bitcoin whitepaper scored 0.70 against its own wording and lost to
    survey papers that merely discuss it.
    """
    # word split by a hyphen across a line break
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    # blank lines stay as paragraph breaks; everything else joins up
    paragraphs = re.split(r"\n\s*\n", text)
    out = []
    for para in paragraphs:
        joined = re.sub(r"\s*\n\s*", " ", para)
        joined = re.sub(r"[ \t]{2,}", " ", joined).strip()
        if joined:
            out.append(joined)
    return "\n\n".join(out)


def pdf_to_text(raw):
    # pymupdf4llm keeps the document's structure. Measured on the Bitcoin
    # whitepaper: 14 markdown headings against pypdf's none, and no broken
    # hyphenation against three. Headings matter because inside a PDF the
    # section classifier has nothing else to work with. It is about ten times
    # slower, which is irrelevant for a shelf of two dozen documents. pypdf
    # stays as the fallback, since a whitepaper parsed plainly beats one that
    # fails to parse at all.
    raw_text = ""
    try:
        import tempfile
        import pymupdf4llm
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(raw)
            tmp.flush()
            raw_text = pymupdf4llm.to_markdown(tmp.name, show_progress=False)
    except Exception:
        raw_text = ""
    if len(raw_text.strip()) < 500:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        raw_text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    return normalize_pdf_text(raw_text)


def html_to_text(raw):
    html = raw.decode("utf-8", errors="replace")
    html = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;?", " ", text)
    return re.sub(r"[ \t]+", " ", text)


def main():
    dry = "--dry-run" in sys.argv
    conn = sqlite3.connect(service.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    service.FULLTEXT_DIR.mkdir(exist_ok=True)

    ok = failed = 0
    for slug, title, year, url in WHITEPAPERS:
        pid = f"wp:{slug}"
        try:
            raw, ctype = fetch(url)
        except Exception as e:
            print(f"  FAIL {slug}: {type(e).__name__} {str(e)[:60]}", flush=True)
            failed += 1
            continue
        try:
            if raw[:5] == b"%PDF-" or "pdf" in ctype.lower():
                text = pdf_to_text(raw)
            else:
                text = html_to_text(raw)
        except Exception as e:
            print(f"  FAIL {slug}: parse {type(e).__name__}", flush=True)
            failed += 1
            continue

        if len(text.strip()) < 2000:
            print(f"  SKIP {slug}: only {len(text.strip())} chars extracted", flush=True)
            failed += 1
            continue

        print(f"  OK   {slug}: {len(text)} chars", flush=True)
        ok += 1
        if dry:
            continue

        service.write_latex_cache(pid, text)
        (service.FULLTEXT_DIR / f"{service.safe_id(pid)}.tex").write_text(
            text, encoding="utf-8", errors="ignore")
        abstract = " ".join(text.split())[:1200]
        conn.execute(
            """INSERT INTO papers (arxiv_id, title, year, layers, status, passed,
                                   abstract, fulltext_source, updated_at, niche_score)
               VALUES (?,?,?,?,'fulltext_fetched',1,?, 'whitepaper-pdf', ?, 20)
               ON CONFLICT(arxiv_id) DO UPDATE SET
                   title=excluded.title, year=excluded.year, layers=excluded.layers,
                   status='fulltext_fetched', abstract=excluded.abstract,
                   fulltext_source=excluded.fulltext_source, updated_at=excluded.updated_at""",
            (pid, title, year, "web3", abstract, service.now_iso()),
        )
        conn.commit()
        time.sleep(1)

    print(f"итого: получено {ok}, не удалось {failed}")
    conn.close()


if __name__ == "__main__":
    main()
