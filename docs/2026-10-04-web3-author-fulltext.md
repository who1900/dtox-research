# Web3 author-copy full text

## Purpose and scope

The author-copy path adds verified author, institutional, or publisher PDFs without replacing or deleting any existing paper record. Twin copies use a separate `author:` ID linked with `twin_of`; an existing matching original is required only for this twin path. A curated standalone entry may be admitted when `curated_standalone: true`, its identity is backed by a valid DOI or syntactically valid IACR ID, and required title/author/year/source metadata validates. Standalone entries do not require an existing original row and never get a fabricated original ID or `twin_of`. The stored copy URL is used as source URL. Unknown license information stays unknown; this flow does not imply a Creative Commons license.

The catalog contains nine PDF entries. FRI uses its verified DOI identity; KZG uses its valid IACR identifier despite lacking an original database row. Neither case requires inventing a database original or a twin link. All nine author rows are now confirmed `done`; the four second-batch records are MCP-verified below. PLONK remains the candidate without a verified copy.

## Safe acquisition and validation

Downloads use the existing shared fetcher, which honors blocked-host rules and each host's `robots.txt`, applies a per-host request pause, and uses the configured user agent and request timeouts. The downloader does not fetch from IACR hosts. Artifacts are staged locally; downloading does not write to the paper database.

Before staging, the script checks extracted text length, title at the start of the text, and an author surname. It records source and verified identity metadata, title, byte length, PDF magic, SHA-256 of the PDF and extracted text, validation flags, and license in a manifest. Applying artifacts repeats identity, title, author, PDF-size/magic, and checksum checks. Twin insertion additionally requires a matching existing original in `done` / `iacr-abstract` state; standalone insertion does not. Mismatches are rejected or skipped. New IDs include a stable DOI or IACR identity plus PDF digest prefix, preventing accidental collision with unrelated records.

The default native parser is PyMuPDF/PyMuPDF4LLM and produces Markdown from PDF text, not original LaTeX source. Manifests mark `exact_latex: false`. Mathematical equations or their structure may be missing or degraded in this representation; it must not be described as source-faithful LaTeX.

## Operator commands

The stable operator script and catalog are `/opt/dtox-research/web3_author_fulltext.py` and `/opt/dtox-research/web3_author_copies.json`. The second-batch persistent PDFs, Markdown, and manifests are under `/opt/dtox-research/web3_author_artifacts/20261004b`; extracted-text caches are under `/opt/dtox-research/fulltext_cache`. The CLI requires exactly one mode. The default `--limit` is 3; accepted values are 1 through 6. Database application is additionally capped at three inserted artifacts per run, regardless of a higher limit.

```sh
/opt/dtox-research/venv/bin/python /opt/dtox-research/web3_author_fulltext.py --audit
/opt/dtox-research/venv/bin/python /opt/dtox-research/web3_author_fulltext.py --download --artifact-dir /opt/dtox-research/web3_author_artifacts/20261004 --limit 5
/opt/dtox-research/venv/bin/python /opt/dtox-research/web3_author_fulltext.py --apply-artifacts --artifact-dir /opt/dtox-research/web3_author_artifacts/20261004 --limit 5
```

`--audit` is read-only and reports catalog/database matches and the proposed action. `--download` fetches and validates eligible catalog PDFs into the artifact directory; it does not touch the database. `--apply-artifacts` uses already-staged files and inserts verified copies. There is no HTTP download during apply. Review audit output and staged artifacts before application. Do not increase limits casually; the normal operational cap is three. If running from a different server, only fetch allowed author copies; do not use this workflow to evade IACR robots restrictions.

## First-batch ingestion record (2026-10-04)

At the first-batch checkpoint, five author copies were imported and fully indexed: Groth16 from UCL, Halo from Imperial, STARK from StarkWare, Bulletproofs from Stanford, and Nova from CMU. All five SQLite rows were confirmed `done`; MCP verification of Nova's paper body and read-section succeeded. This is historical status; KZG and FRI were subsequently imported and are included in the current nine-row confirmation above.

The first-batch global twins-builder attempt timed out at 90 seconds without staged output; the live `twins.json` remained untouched at that time. The subsequent targeted exact-copy publication is confirmed in the final QA below. Twin links are not claims of new paper merges. PDF parsing remains distinct from source-copy verification: mathematical structure completeness is unknown.

The native batch parsed three PDFs in 175 seconds, with 114.5 MB peak memory and 1 minute 21 seconds CPU time, within the 50% CPU quota. These are batch resource metrics, not production changes. A complete PDF does not guarantee complete parser output: exact mathematical structure is not recovered. For Groth, the structured specification currently has zero extracted elements, although reading the section itself works. Do not present parser output as complete equation/structure coverage.

The API author-copy label is deployed and health is OK. Registry counts remain unchanged at 54/16/2. No Oracle work or IP rotation was performed.

## Test snapshot

The latest reported API suite has 801 passed and 1 skipped; the MCP suite has 26 passed. The 21 loader/author-specific tests passed. These test results are separate from database import, indexing, and parser-structure completeness.

## Privacy

Do not include user-private IP addresses, keys, credentials, or other secrets in this document, logs, or operator notes.

## Second batch: candidates and bounded math review (2026-10-04)

The candidate set was FRI, KZG, Pietrzak, and Wesolowski. PLONK has no verified copy and remains the outstanding candidate. Acquisition, import, indexing, and twins-publication results are recorded below.

For curated standalone copies, preserve the verified copy identity and do not invent an original IACR record or `twin_of` target when none exists. In particular, publication year and copy-version metadata must remain distinct: an original work dated 2018 may have a publisher copy from 2019 or 2020. Preserve `copy_publication_year`, `copy_version`, and `identity_source` in the artifact manifest so a later copy year is not misreported as the original year.

A bounded visual check of Groth PDF page 14 against the main paper found that the old Markdown verification equation is missing after “test” and the `C_sim` expression is garbled; denominators and summation indices are also not faithfully represented. The artifact's `exact_latex: false` flag remains appropriate. This is a scoped observation about that page, not a complete PDF/parser audit; no parser behavior was changed.

Application is capped at a maximum of three new inserts per run; the four verified imports were applied across sequential runs.

## Second-batch acquisition result (2026-10-04)

All four candidate PDFs downloaded and passed staging validation into `/tmp/dtox-web3-author2-20261004.IP6iEx5z/artifacts`. Their SHA-256 values:

| Candidate | PDF SHA-256 |
|---|---|
| FRI | `e244896fb6e7fcab7fe4de00e31a36003b941b6550e062fdb5ee66d78641498d` |
| KZG | `c24aa91367299d29eec69557ca90c451e45747b91f3838868f6f797147e9e811` |
| Pietrzak | `91415004afd0a96ce0c147ef6a1eba8eb96d7e3608ed9c02e4236301e3ed4893` |
| Wesolowski | `d381c40a2f0aec6365db8b42c3c0dcf8c2cf982f80e74783da42566cabbfed35` |

The acquisition took 83.6 seconds wall time and 36.7 seconds CPU time. HAL/Wesolowski (2020) already had full text, so these four copies represent only three works that were previously missing.

## Second-batch import result (2026-10-05)

All four copies were imported. PDFs, extracted Markdown, and manifests are persistent under `/opt/dtox-research/web3_author_artifacts/20261004b`; extracted-text caches are under `/opt/dtox-research/fulltext_cache`. The four PDFs total 85 pages (FRI 17, KZG 18, Pietrzak 15, Wesolowski 35). Imported IDs:

| Work | New author ID |
|---|---|
| FRI | `author:doi:10_4230_lipics_icalp_2018_14:e244896fb6e7` |
| KZG | `author:2010/009:c24aa9136729` |
| Pietrzak | `author:2018/627:91415004afd0` |
| Wesolowski | `author:2018/623:d381c40a2f0a` |

Database import is confirmed and all four records have completed MCP full-text/index verification as reported below. Changes were limited to the ops loader and catalog; no API restart was performed. Backup: `/opt/dtox-research/release-backups/web3-author2-20261004T190123Z-3251270`.

## Second-batch final QA (2026-10-05)

MCP verification confirms all four imported papers have `fulltext: true`:

| Work | Indexed chunks |
|---|---:|
| FRI | 57 |
| KZG | 56 |
| Pietrzak | 53 |
| Wesolowski | 105 |
| **Total** | **271** |

For each paper, `read_paper_section` returned the exact requested outline title and offset 6500 with a maximum of 600 characters, and the returned body was verified at 600 characters. This confirms retrieval of those sections, not structured-math extraction: section classification is mostly `other`, and no structured math is available. This remains an explicit limitation and has not been fixed.

The catalog contains nine PDFs. Across the first five and these four files, the cumulative set is nine PDFs / 302 pages (217 + 85). HAL/Wesolowski is an already-existing full-text copy, so the four recent files add only three distinct scientific works. Persistent second-batch PDFs, Markdown, and manifests are under `/opt/dtox-research/web3_author_artifacts/20261004b`; extracted-text caches are under `/opt/dtox-research/fulltext_cache`.

The targeted twins publication added exactly four mappings, preserving every prior mapping: two original-ID self-mappings and two author-copy IDs mapped to their IACR originals. Total mappings increased from 37,250 to 37,254. Both VDF rows were validated as `done`, with PDF/cache hashes checked. Their 2019 and 2020 editions are verified related-version links; exact revision equivalence is not claimed. Backup: `/opt/dtox-research/release-backups/web3-author2-twins-20261004T190817Z-3252557/twins.json`.

An exact-title KZG search with `limit=10` returns the new KZG record at rank 3. The FRI query with `limit=10` and `layer=web3` ranks `1903.12243` first, `iacr:2024/390` second, and `iacr:2016/116` third; the new FRI author record is fourth. Acquisition and body retrieval are verified, but retrieval ordering remains a limitation. No API change was made for these results.

Final remote confirmation is received: all nine author rows are `done`, artifact hashes were verified, and all three services are active. The main API remains at `f9c08`; bundle `4c737` is unchanged. Registry counts remain 54/16/2. PLONK is the remaining candidate without a verified copy. MCP full-text, indexed-chunk, and section-read checks for all four second-batch papers are complete.
