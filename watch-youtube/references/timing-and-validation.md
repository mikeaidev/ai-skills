# Track timing and validation

## Coordinates and evidence

Keep overall presentation duration, source-track presentation intervals, demuxed packet PTS/DTS, decoded sample counts and sampled-image PTS distinct. A player may report the longer track. Video and audio may legitimately start/end at different times. A last keyframe starts a decoding group; it does not define the end of that group's final displayed frame.

Use rational native ticks for timing comparisons. Preserve codec delay, pre-skip, discard padding, packet/frame duration, time-base precision and the evidence supporting any coordinate mapping. Do not add initial and final audio padding to video coverage tolerance. Do not count the same codec delay twice from different representations of it.

A captured final fragment with its declared sample count, continuous timestamps and complete payload is evidence of a consistent captured tail. It alone cannot show that a later source fragment was never omitted. A copied header, complete observed boxes, full decode, or exported hash match alone also cannot establish complete hosted-original retrieval.

## CLI expectations

- `--expected-duration`: independently observed overall player/presentation duration. Retain it as metadata; do not demand every track match it.
- `--expected-start` / `--expected-end`: an explicit selected validation interval. Keep it separate from review sampling bounds. Where an independently evidenced source track has a shorter extent, validate the intersection with that track.
- `--track-expectations FILE`: per-kind source extents and optional native-to-source offsets with provenance. Use actual source information when available. Leave unknown source extents unknown.

Use this JSON shape, replacing the symbolic strings with independently obtained finite decimal/rational values:

```json
{
  "video": {
    "start": "SOURCE_TRACK_START",
    "end": "SOURCE_TRACK_END",
    "timestamp_offset": "NATIVE_TO_SOURCE_OFFSET",
    "source": "Description of the source evidence and timestamp coordinate"
  },
  "audio": {
    "start": "SOURCE_TRACK_START",
    "end": "SOURCE_TRACK_END",
    "timestamp_offset": "NATIVE_TO_SOURCE_OFFSET",
    "source": "Description of the source evidence and timestamp coordinate"
  }
}
```

Treat these strings as schema placeholders, not executable values. Define `source_time = demux_time + timestamp_offset`; use zero only when the coordinates coincide. Do not fit an offset to align observed endpoints, and do not replace an independent expectation with the last captured packet. User-supplied provenance is an assertion recorded by the helper, not independently authenticated evidence. Keep the original supporting source metadata with the run.

Distinguish Opus encoded Block timestamps, delay-adjusted demux timestamps and retained decoded sample duration. A decoded PCM duration is a sample count divided by sample rate, not automatically an absolute endpoint. Timestamp quantization can prevent an exact mapping; retain uncertainty rather than broadening tolerances to a full frame or packet.

Padding summaries preserve each Skip Samples record's zero-based `packet_index` in demuxed stream order, raw `pts` and rational `pts_seconds_exact` when available. Initial skip samples come only from the actual first packet; a later skip record is not stream priming. Stream-declared initial padding and first-packet skip metadata can describe the same delay, so the summary uses their maximum rather than adding them. These metadata summaries do not extend video coverage or change native packet durations.

## Independent outcomes

Read the validator's per-stage results and diagnostics, not just its process exit code. Treat `passed`, `failed`, `needs_review`, `unknown`, `not_tested`, `not_compared` and `unsupported` according to the reported stage. Exit 0 indicates no confirmed blocking technical failure; completeness may remain unknown. Exit 1 identifies a confirmed failure or refused/invalid run. Exit 2 identifies review required. Preserve the reasons and any underlying strict failure.

Keep these questions separate:

1. Did the exported/transferred bytes match the recorded source file?
2. Does the container have consistent observed media boundaries and a complete outer envelope?
3. Can all mapped streams be decoded?
4. Does the requested interval have native media coverage, under the stated evidence and coordinate mapping?
5. Did a remux preserve encoded payloads and mapped PTS/DTS?
6. Do decoded pictures/audio samples match, and does their timeline match?
7. Which actual images/audio were inspected?

Retain whole-file diagnostics independently of selected-interval coverage. A gap outside the interval should not declare that interval missing. Uncertain packet durations, VFR holds and B-frame presentation ordering require interpretation; do not treat every reordering or repeated payload as missing/corrupt media.

## Remux and decoded comparison

Retain originals. Stream copy without independent A/V rebasing, interpolation, generated timestamps or shortest-track truncation. Container/codec framing can change packet bytes even when decoded pictures are identical. Report strict encoded preservation as its own result.

When payload alignment is unavailable, label dependent timestamp comparison `not_compared`; do not imply measured timestamp corruption. Use the optional decoded comparator for ordered native-picture checks, canonical retained audio samples and separate rational timing/count results. Do not infer content identity from packet counts alone or certify timing from content hashes alone. Record unsupported formats, resource limits and missing evidence explicitly. Decoded equality does not establish the identity/completeness of the hosted original or perceptual review.
