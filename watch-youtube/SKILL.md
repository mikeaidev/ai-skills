---
name: watch-youtube
description: Retrieve, validate and inspect YouTube or local video using actual media, per-track timing and timestamped visual evidence. Use when asked to watch or summarize a video, study a tutorial or editing technique, locate scenes, compare motion or analyze reference footage. Includes bounded local workflows, review checkpoints and a provider-neutral browser MediaSource capture fallback. Keep acquisition, technical validation, sampled visual inspection, continuous viewing and audio audition distinct.
---

# Watch YouTube Videos

Resolve bundled paths against this skill directory. Keep media and generated results outside it. Start from the supplied URL/file and current runtime capabilities; do not depend on earlier chats, installed personal helpers, cached credentials or a specific model vendor. Use the portable Python/JavaScript source with the host's permitted tools. Treat `agents/openai.yaml` as optional ChatGPT UI metadata.

## Start in a fresh session

1. Resolve the requested source, question, quality and interval. Match review depth to the question; do not require a whole download for a narrow interval.
2. Discover the host's available file/execution, browser, download, image-inspection and audio-inspection capabilities. Prefer a sufficient service integration or an already-working raw-file route. Use [references/browser-adapter.md](references/browser-adapter.md) to map browser capabilities; do not invent tool names, sessions or access.
3. Run the dependency preflight when execution is available:

   ```bash
   python3 scripts/workflow.py doctor
   ```

   Require Python 3.10+, FFmpeg and ffprobe for local helpers. Use Pillow optionally for contact sheets and Node.js for JavaScript tests. Read [references/runtime-limitations.md](references/runtime-limitations.md) for missing-capability and resource handling. Do not claim an unavailable stage succeeded.
4. Record source identity and requested scope. Distinguish `retrieved`, `technically_checked`, `frames_extracted`, `frames_visually_inspected`, `continuous_viewing` and `audio_audition`; later stages never follow automatically from earlier ones.

## Acquire playable media

Reuse an existing usable source file when available and validate its actual streams. For downloads, verify media rather than HTML and preserve byte counts/hashes. Use captions and page metadata only as labeled supplementary evidence.

If direct acquisition is unavailable but ordinary playback works, read [references/browser-adapter.md](references/browser-adapter.md). Use [references/skyvern-mse.md](references/skyvern-mse.md) only when that provider is available. The capture/export JavaScript is provider-neutral; the host supplies evaluation, clicks and binary transfer.

1. Establish advancing decoded frames, intended source and actual selected dimensions.
2. Evaluate all of `scripts/capture_mse.js` before fresh main-thread MediaSource/SourceBuffer initialization. Restart through an observed supported player method while retaining the hooks. Seeking to zero alone may omit initialization.
3. Inspect `window.__ytMediaCapture.status()`. Keep MIME/init generations separate. Avoid seeks and quality changes during a continuous capture. Treat unsupported framing, pending/failed appends, repeated initialization, offset changes or overflow as explicit review/refusal conditions.
4. Pause once the requested material is buffered and appends settle. Supply explicit selected IDs and `requestedInterval` in SourceBuffer seconds when the task's bounded capture interval is known; use independently evidenced `streamIntervals` for differing track extents. Read [references/format-evidence.md](references/format-evidence.md) for coverage attribution and whole-video requests. Evaluate `scripts/export_capture.js`. Export binary files through visible anchors and host download receipts. Compare transferred bytes/SHA-256 against export records; do not transfer video as inline base64.
5. Save source identity, settings, capture evidence and export/receipt hashes together. Distinguish overall presentation duration, each SourceBuffer's timeline, container boundaries and native packet intervals. Browser ranges alone do not prove hosted-original completeness.
6. Transfer and verify files before calling `window.__ytMediaCapture.restore()` and closing sessions created for this task. Retrieve existing export metadata idempotently instead of recreating exports after an evaluator returns null.

Preserve original bytes. Do not substitute screen recording, frame interpolation, transcoding, generated timestamps, independent A/V rebasing or `-shortest` for missing source data. Honor host access and execution restrictions. Do not assume a temporary `blob:` URL is a complete original file.

## Run bounded local analysis

For acquired files, use the orchestrator to preflight, verify optional acquisition records, validate and sample manageable sections:

```bash
python3 scripts/workflow.py run --input VIDEO_FILE --audio AUDIO_FILE \
  --output-dir NEW_RUN_DIRECTORY --start 0 --end 30 --fps 2 \
  --source-url SOURCE_URL
```

Omit `--audio` for combined or intentionally video-only files. Supply only evidence actually obtained. Read `--help` and [references/workflow.md](references/workflow.md) for acquisition records, per-track expectations, limits, result states and review continuation. The local command consumes acquired files; it does not secretly control a host browser or provide a downloader.

Require a previously absent output directory for each run/extraction. Existing destinations are refused without modifying earlier media, manifests or annotations. Use bounded sections and configurable byte/frame/log budgets. Preserve a completed review ledger and create fresh extraction directories for additional material.

## Validate timing and preservation

For fragmented-MP4 duration evidence, AV1 framing equivalence, container/index diagnostics, optional analysis copies or requested capture coverage, read [references/format-evidence.md](references/format-evidence.md). Keep each evidence result separate from strict byte identity, original-source completeness and actual perception.

Use the validator directly for independent technical checks:

```bash
python3 scripts/validate_media.py --input VIDEO_FILE --audio AUDIO_FILE \
  --output-dir NEW_QA_DIRECTORY --expected-duration PLAYER_DURATION_SECONDS
```

Treat `--expected-duration` as overall presentation metadata, not an instruction that every track must have that endpoint. Provide independently evidenced track expectations and coordinate mappings through `--track-expectations` when available. Keep their provenance. Read [references/timing-and-validation.md](references/timing-and-validation.md) before supplying them or interpreting a tail mismatch. Do not fit an expected endpoint or timestamp offset to the final observed packet merely to obtain a pass.

- Inspect codecs, dimensions, rate, sample properties, full decode results, native timing and container warnings. Report absent audio explicitly.
- Evaluate each track's native intervals and source-to-demux mapping. Account for codec delay, pre-skip, discard padding, frame durations, legitimate holds and clock quantization. Never transfer audio padding into video tolerance.
- Separate requested-interval coverage from whole-file integrity. Retain problems outside the selected interval without mislabeling that interval uncovered.
- Read per-stage results. Keep confirmed failure, review required, unknown, unsupported, not tested and not compared distinct. Decoder success or a copied duration alone never establishes complete source retrieval.
- Preserve raw files when stream-copying tracks into MKV. Report encoded payload identity, PTS/DTS preservation, auxiliary duration/padding changes and container structure separately. Do not imply measured timestamp damage when no timestamps were compared.
- Add `--decoded-equivalence` when an original/remux comparison is useful. Read picture/sample equality and timeline results separately. Decoded equivalence does not establish encoded identity, source completeness or audio audition.

Investigate warnings using [references/troubleshooting.md](references/troubleshooting.md). Keep an incomplete outer container envelope separate from complete observed media fragments; do not automatically dismiss an EOF warning or treat it as proof of lost pictures.

## Inspect actual images and record review

Read [references/review-protocol.md](references/review-protocol.md). Open generated images/contact sheets before recording observations. Start with distributed overview samples, then inspect every section needed for the question. For rapid motion, cuts, acting, contact or gaze, inspect short consecutive native-frame bursts or supported playback. Coarse samples cannot establish subtle animation or audiovisual synchronization.

Use `scripts/sample_frames.py --help` for direct extraction. Check native PTS, evidenced display intervals, boundary-context frames, resource-limit warnings and requested-versus-actual coverage. Keep intentional holds. Use `--max-width 0` when source-resolution text/details matter. Never infer continuous viewing from a sample span or from a frame's displayed duration.

Use `scripts/review_ledger.py` to initialize a ledger from a manifest and explicitly record only the frames actually examined, observations, interpretations and next section. The ledger validates source/frame identities; it does not perform visual inspection. Record audition only after using an actual audio-capable route. Captions, waveform metrics and decoded samples do not establish what was heard.

## Report and continue

State retrieval/transfer results, per-stage technical outcomes, unresolved warnings, exact visually inspected timestamps/method, audio-audition status and unreviewed portions. Answer the user's question using observations and label interpretations/applications. Save the checkpoint and next interval. Avoid saying the whole video was watched after sampled-image or caption-only inspection.

## Redistribute and verify

Read [references/distribution.md](references/distribution.md). Keep the MIT license with the source. Build public ZIPs through `scripts/build_release.py` and the explicit `release-files.json` allowlist. Exclude retrieved media, generated reports, runtime checkpoints, secrets and private identities. Test from a clean extraction and verify live acquisition separately in each deployment; mocks and a prior session do not establish current browser support.
