# Local package QA: 2026-10-05

Final October 5 media: pitch **168.479667 seconds** (9 scenes, 292 spoken words); demo **171.229667 seconds** (10 scenes, 322 spoken words). Both are separate English H.264 1920x1080/24 fps MP4s with AAC 48 kHz mono, embedded English subtitles and matching external SRT/VTT. Local synthetic Microsoft Zira Desktop uses normal rate 0; no external paid service. UTF-8 was made explicit for the pitch's apostrophes before the final render.

## Completed checks

- Documentation consistency: 13 public tools, free read beta, proposed paid intensive usage, three unconfirmed pilots, per-record provenance, observation-time inventory, wallet attribution and dated minimal devnet proof.
- Six docs tests passed under Node 22.15.1. Exactly three cases were adapted to the moved technical-reference sections; environment setup, service isolation, payment/quorum boundaries, restore safety and README discoverability remain tested. The other three cases are unchanged. No current full product-suite run is claimed.
- Presentation belongs to the existing completed DTOX editor: 9 main slides plus 2 appendices. Eleven PDF pages were visually checked. Deck, speaker notes and evidence source files remain byte-identical to the frozen completion hashes.
- Every one of 19 extracted movie scene frames was reviewed. Source math/EIP excerpt, extraction limitations, captured-response dates and separate October 3 proof remain readable. Captions use measured per-phrase audio anchors rather than proportional estimates.
- Video source, manifest and narration TXT agree. SRT cue text/IDs match each phrase; rounding is within 0.51 ms, no overlap, at most two lines. MP4 duration is within each official limit; audio/video timing differs by less than one 24-fps frame. Final VTT is checked against SRT.
- Full decoding reports no errors. Synthetic speech source uses explicit UTF-8 and normal rate 0. Waveforms are non-silent and below clipping. Local independent ASR was unavailable; specialist pronunciation remains an owner listening decision.
- Read captures are existing response files with recorded hashes. Rendering is local/offline: no new MCP/RPC writes, payments, signatures or registry changes.

## Final artifact evidence

| File | Measured duration | SHA-256 |
| --- | --- | --- |
| presentation-preview.mp4 | 168.479667 s | `88055576544a1fa2cf75a232a4582184fadf2c6ded1503effa973564a954af05` |
| presentation.srt | 30 cues | `9f44fb80baf06082165d51e8ae248a5ee53ff7ccca1a2f226c157dad7b76bd02` |
| presentation audio levels | mean -20.4 dB; peak -2.8 dB | No clipping |
| demo-preview.mp4 | 171.229667 s | `e43de1b8dc9861088c8d1bbcdf51c7694f93febed7d1ed025f4385904783ea22` |
| demo.srt | 33 cues | `c4670b25c294c1e891321a6fdf6976b6324ca4a5d8a0ce57df8d14ea2d333e63` |
| demo audio levels | mean -20.6 dB; peak -2.3 dB | No clipping |

[Detailed media metadata](../presentation/video-2026-10-05/qa-report.json), [independent checks](../presentation/video-2026-10-05/validation-report.json), [build sources](video-2026-10-05/README.md).

## Historical results retained

- October 2: original preview lengths 160.104667/168.896333 s; prior 47 attestor and 19 targeted API checks remain dated evidence.
- October 3: prior gateway 38 and attestor 47 checks, and the authorized one-payment/one-signed-reading devnet proof remain dated evidence. Helper 3 overlaps gateway 38 and is not additive.
- The October 3 reading is `does_not_assert`, `read_once`, one reader, no independent scientific quorum. Production/mainnet gates are still NO-GO; EVM settlement is unverified.

## Remaining owner decisions

Confirm registration/eligibility, roster/location/track, founder facts, prior-work/funding/IP disclosures, specialist pronunciation, final video hosting URLs and selected public repository revision. Public release/upload/submission requires separate authorization. No such action occurred during preparation.

Deadline: October 12, 23:59 PDT = October 13, 06:59 UTC / 11:59 Almaty. See [official-source checklist](2026-10-05-submission-checklist.md).
