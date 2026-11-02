# Runtime capabilities and limitations

## Portable core and host capabilities

Use Python 3.10+ and FFmpeg/ffprobe supporting the helper commands, including `-fps_mode passthrough`. Run `python3 scripts/workflow.py doctor` in a fresh environment. Use Pillow optionally for contact sheets. Use Node.js for JavaScript regression tests. The runtime scripts use Python's standard library and do not require a model-provider SDK, private account, stored session or prior workspace.

An AI host must separately provide execution/file access, a route to media bytes, actual image inspection and, when requested, actual audio audition. A skill is instructions and source code; it cannot add capabilities absent from the host. A text-only session can discuss supplied text but cannot truthfully report new visual/audio inspection. Missing capability must remain unavailable, not simulated success.

Browser capture requires ordinary playable media, supported page-context evaluation, main-thread MediaSource/SourceBuffer access, binary download registration and raw-file transfer. Map these through the generic browser-adapter contract and use the appropriate provider reference when available. Discover current schemas, accounts and resource limits. Worker-only pipelines, protected media, access barriers, policy restrictions or missing downloads can make that route unavailable. Do not enable a forbidden operation or bypass a rejected evaluation route.

ChatGPT UI metadata is optional for other hosts. Other models can follow `SKILL.md` and run the same CLI/JavaScript through equivalent permitted tools. Do not claim a particular model, account tier, browser provider or deployment was tested unless actually exercised.

## Safe outputs and continuation

Require a previously absent directory for every validation, extraction and workflow run. Existing directories, reports and symbolic-link aliases are refused before checkpoint writes. Inputs are hashed before claiming output ownership. Preserve original media and earlier review annotations.

Resume review through the explicit review ledger and saved next interval. Create fresh directories for additional extraction. Do not describe a new extraction as an automatic reconstruction/resume of a previous failed one. The ledger records explicit inspection assertions and verifies provenance; it cannot verify that a human/model really opened an image or listened to sound.

## Bounded resources

Use manageable review sections and configurable frame, output, log, probe and timeout limits. Spool large subprocess results to disk and bound parsing; a spooled/bounded JSON probe is not an unlimited streaming parser. A limit stops or marks the affected stage; it does not permit a whole-file pass from a prefix. Keep diagnostic files and the last completed checkpoint. Plan storage for source media, remuxes, stills and decoded comparisons as separate costs.

Browser capture has configured byte/event limits. Export Blobs and hashing may require additional memory. Read the actual status and choose a smaller interval, working direct route or explicit new budget when needed. Worker recovery, page-reload recovery, automatic interrupted-capture stitching and codec/init-change stitching are not implemented.

## Timing and validation boundaries

Read [timing-and-validation.md](timing-and-validation.md). Keep presentation duration separate from per-track extents. Unknown source expectations remain unknown. Decoder success, container headers, browser buffered ranges and hash matches answer different questions; none independently proves all original content was retrieved.

Treat encoded-payload identity, timestamp preservation, auxiliary durations/padding, decoded content and decoded timeline as independent outcomes. Optional decoded comparison can explain a framing-only mismatch but does not erase a strict encoded-preservation failure. Account for documented timestamp quantization without excusing a whole missing frame. Retain genuine uncertainties and container diagnostics.

Frame sampling records actual native PTS, boundary context and display-interval evidence when ascertainable. Samples do not establish continuous viewing or motion between sampled frames. A displayed hold does not mean its whole duration was watched. Record the exact reviewed images and actual audio-audition scope separately.

The structural capture tracker follows recognizable MP4/WebM boundaries across appends and refuses repeated initialization. It is not a full codec parser. Complete observed fragments/Clusters do not necessarily mean the outer container envelope or all source media is complete. Preserve raw bytes and investigate warnings independently.

## Primary technical documentation

Consult current official documentation when a runtime differs:

- SourceBuffer: https://developer.mozilla.org/en-US/docs/Web/API/SourceBuffer/appendBuffer
- Media Source Extensions: https://w3c.github.io/media-source/
- FFmpeg timing and stream copy: https://ffmpeg.org/ffmpeg.html
- ffprobe: https://ffmpeg.org/ffprobe.html
- FFmpeg bitstream filters: https://ffmpeg.org/ffmpeg-bitstream-filters.html
- AV1 bitstream specification: https://aomediacodec.github.io/av1-spec/
- Matroska codec mappings: https://www.matroska.org/technical/codec_specs.html
- Opus encapsulation: https://www.rfc-editor.org/rfc/rfc7845

Use live tool schemas for the host's enabled operations. Public documentation does not guarantee a hosted deployment exposes the same capabilities or limits.
