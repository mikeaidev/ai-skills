# Container timing, framing and analysis copies

Read this reference when a playable capture has missing probe durations, a container-envelope warning or an encoded remux mismatch. These checks describe captured local bytes. They do not authenticate the complete hosted original or establish visual/audio inspection.

## Verified fragmented-MP4 timing

Some demuxer/probe outputs omit packet durations that are explicitly declared by the container. The bounded MP4 reader resolves supported fragmented layouts using the track time base, fragment decode times, sample tables and duration defaults. It checks sample byte ranges against captured media-data boxes and matches ordered sample payload hashes and rational PTS/DTS to the probe records before returning duration evidence.

Only missing durations with verified correspondence can use this evidence. Keep the raw probe JSON, the source hash and the duration's provenance. Contradictory hashes, timestamps, sizes or sample tables cannot be corrected by fitting an offset or typical frame rate. Unsupported edits/layouts and exhausted budgets retain an explicit unsupported/review outcome.

A complete set of captured fragments may still be only part of a video. The last verified sample establishes the captured extent, not an independently expected source endpoint. Container duration and player presentation duration remain separate metadata.

Decoded-frame timing can use sample-duration evidence only when the verified sample timeline aligns unambiguously with the decoded frame sequence. Packet duration and displayed-frame duration must not be equated for an unsupported or ambiguous mapping.

Decoded hashes and timing probes use the same single-threaded decoder setting so that their returned frame sequences can be compared consistently. A missing timing row or picture is a real comparison failure until the full sequence is obtained and aligned; container evidence cannot manufacture decoded frames.

## AV1 framing equivalence

Strict encoded-packet identity still reports whether packet bytes are identical. A separate narrow AV1 check recognizes a validated empty temporal-delimiter OBU at the beginning of a temporal-unit packet. It can establish correspondence when this delimiter is the only change and every remaining packet byte agrees in order.

The comparison must parse the OBU framing and verify packet counts, sizes/hashes and codec compatibility. It must not drop arbitrary headers, padding, tile data or malformed units merely to obtain equality. Missing/reordered packets and any other payload change remain unexplained by this rule.

Once this correspondence is established, compare native PTS/DTS independently using rational clock bounds. Report the normalization rule and measured deltas. Do not change the strict byte-identity result, fit a timestamp shift or broaden an allowance enough to conceal a missing frame. Decoded picture/sample equality remains another independent check.

## WebM/Matroska structure

Keep the following outcomes separate:

- Whether observed media elements and Blocks are structurally complete.
- Whether the outer Segment envelope reaches its declared end or uses an unknown size.
- Whether SeekHead/Cues entries point to the declared elements.
- Whether the captured interval or entire hosted source is complete.

A stale index target or short outer envelope does not identify the absent bytes. Complete observed Clusters/Blocks do not establish that another complete Cluster was never omitted. Unsupported lacing/layouts and resource limits must not produce a structural pass.

## Optional analysis copies

Use `--analysis-copy` with the validator or workflow when a supported Opus WebM/Matroska capture needs a clean container for local analysis. The helper creates a new stream-copy derivative in the owned run directory. It preserves the raw input and its diagnostics. Other codecs/layouts retain an explicit unsupported or not-applicable result.

A verified analysis copy requires a clean output structure and successful decode, unchanged ordered audio payloads, preserved timestamps and codec-delay/pre-skip/discard information, and equal retained decoded PCM. A clean wrapper alone is insufficient. A copy cannot supply missing samples or convert unknown original-source completeness into a pass.

Example using separately acquired tracks:

```bash
python3 scripts/workflow.py run \
  --input ../media/video.mp4 --audio ../media/audio.webm \
  --output-dir ../runs/format-check-001 --start 0 --end 10 --fps 2 \
  --validate-interval --decoded-equivalence --analysis-copy
```

Choose real bounds and a previously absent output directory. Inspect the per-stage reports and copy-verification result. Raw-container review and strict encoded changes can remain visible even when a derivative is suitable for the selected analysis interval. The process exit code must not replace those distinctions.

## Capture coverage before export

For a bounded request, give the browser helper the requested SourceBuffer interval:

```javascript
window.__ytExportOptions = {
  ids: ["SELECTED_VIDEO_ID", "SELECTED_AUDIO_ID"],
  requestedInterval: {start: 0, end: 10}
};
```

Replace IDs and bounds with actual observed values, then evaluate the packaged export helper. An interval request gates the export; it does not trim media or reset its timestamps. Use explicit per-stream intervals only when independently evidenced track bounds or coordinates require them. Never substitute a common player duration for every track's endpoint.

SourceBuffer ranges can combine data from earlier codec generations. A later generation must not inherit coverage merely because the shared browser buffer spans the request. Initialization, offset/window changes, removal, seeking and codec adaptation can defeat attribution; unestablished generation coverage remains review-required. Exports made without requested bounds retain unknown request completeness.

Select a stable quality through the host's observed controls before capture, install hooks before fresh initialization, and monitor changes while buffering. If no eligible generation covers a whole-video request, use a fresh bounded capture or another permitted native-file route. Do not concatenate incompatible generations or transcode them silently. Repeated attempts should stop at the task's explicit time/byte/service budget, retaining the last completed evidence.

## Primary format references

- ISO BMFF MediaSource fragment/sample requirements: https://www.w3.org/TR/mse-byte-stream-format-isobmff/
- AV1 low-overhead OBU syntax and empty temporal delimiters: https://aomediacodec.github.io/av1-spec/av1-spec.pdf
- EBML element framing: https://www.rfc-editor.org/rfc/rfc8794.html
- Matroska elements, seek structures and audio timing metadata: https://www.rfc-editor.org/rfc/rfc9559.html
- FFmpeg stream-copy operation: https://ffmpeg.org/ffmpeg.html#Streamcopy

These references describe formats and operations. The helper's supported subset, checked byte ranges, actual comparison results and resource limits define what was established in a particular run.
