# Diagnose by stage

| Observation | Action |
| --- | --- |
| Output directory already exists | Choose a new directory. Preserve old media, manifests and annotations. |
| Dependency/capability preflight fails | Use the available permitted route or identify the missing capability. Do not claim an unrun stage passed. |
| Downloader returns HTML, tiny body or probe failure | Retain the exact error. Try browser capture only when ordinary playback and the required browser operations work. |
| Page lacks advancing decoded frames or expected dimensions | Establish intended playback/quality before capture. |
| Evaluator returns null after a script | Query the installed helper's status/export metadata through an expression. Null does not prove side effects failed. Read browser-adapter.md before retrying. |
| Evaluation is disallowed | Use supported acquisition/inspection operations or report this route unavailable; do not evade the restriction. |
| Playback works but no usable fresh captured stream appears | Check new initialization, main-thread versus worker MSE and source identity. Do not accept unknown-init data. |
| Multiple candidates, quality change or timestamp offset change | Keep generations separate and verify source/settings. Do not merge them blindly. |
| Overflow, pending/failed appends or repeated initialization | Stop and replan a bounded capture. Retain only explicitly labeled diagnostic exports when needed. |
| Video and audio have slightly different endpoints | Compare per-track extents and coordinate mappings. The longer audio track can determine player duration. Do not add its padding to video tolerance. |
| Native audio end differs from header/player end | Inspect codec delay, pre-skip, discard padding and raw-versus-demux coordinates. Do not infer a missing packet from different clocks alone. |
| Last video keyframe is several seconds before the end | Inspect all frames and declared sample durations in that final GOP/fragment. A final keyframe at EOF is not required. |
| Packet end is short of an independently evidenced source-track end | Preserve the source expectation; inspect real missing coverage. Do not fit the expectation or enlarge tolerances to force a pass. |
| Source-track end/mapping is unknown | Report unknown/review required with measured native timing. Do not call it proven truncation or proven completeness. |
| Gap is outside requested interval | Keep it in whole-file integrity diagnostics; evaluate selected coverage separately. |
| Raw container warns about premature EOF | Inspect outer envelope and observed fragments/Clusters separately. Complete observed media boundaries do not identify any absent bytes. |
| Remux payload hashes differ | Retain strict mismatch; optionally compare decoded content/timing. Do not infer damaged pictures or mark unperformed timestamp checks failed. |
| Decoded pictures match but timestamps differ | Report content and timeline results independently. Do not rebase streams silently. |
| Frame samples stop before the requested end | Check evidenced media extent, final display interval, sample spacing and resource limits separately. Extraction success is not complete review. |
| Requested start lies inside a long hold | Include an evidenced preceding displayed frame as boundary context; retain its original PTS and interval evidence. |
| File receipts are missing | Query the same session after asynchronous completion and inspect current provider errors. Keep export object URLs alive; do not dump media through text. |
| Long media exceeds a resource limit | Retain the checkpoint, choose bounded sections or a working direct route, and use fresh output directories. Do not label prefix processing a whole-file pass. |

## Raw container and source completeness

SourceBuffer bytes may retain outer Segment lengths or index offsets that do not match the concatenated append stream. A premature-end warning can indicate missing media or omitted/nonmatching metadata. Preserve it until actual source/packet evidence supports an interpretation. Do not repair lengths, fabricate missing bytes or suppress warnings solely because decoding succeeds.

Keep raw originals even when a remux is usable. A reconstructed container needs its own decode, timing, coverage and preservation results. Source identity, transfer integrity, container structure and completeness are separate claims.
