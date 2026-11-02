# Portable workflow and review continuation

## Local capabilities and planning

Run these commands from the package directory or use absolute script paths. The CLI uses only bundled scripts, standard-library Python and installed FFmpeg/ffprobe. It consumes already acquired local files. Browser operations and actual image/audio inspection remain host capabilities described in browser-adapter.md.

```bash
python3 scripts/workflow.py doctor
python3 scripts/workflow.py plan --start 0 --end 90 --section-seconds 30 --fps 2
```

The numeric planning example is illustrative. Use the actual requested scope. Preflight checks executable availability and FFmpeg's required output-frame-rate mode; optional contact-sheet/test tools are reported separately. It cannot discover the host's authenticated browser, image or audio tools from inside a local process.

## Acquisition record

Keep source identity, the unmodified export metadata, transferred local files and declared receipt hashes together. The optional acquisition record has schema version 1:

```json
{
  "schema_version": 1,
  "source": {"url": "PUBLIC_SOURCE_URL", "title": "Observed public title"},
  "exports": {"files": []},
  "tracks": [
    {
      "role": "video",
      "id": "EXPORTED_FILE_ID",
      "receipt": {"bytes": 0, "sha256": "ACTUAL_TRANSFERRED_FILE_SHA256"}
    }
  ]
}
```

Replace all symbolic values. Populate `exports` with the complete object returned by `prepareExports()` or `exportMetadata()`, without rewriting its file records. Add the audio track record when a separate audio file is supplied. Role IDs refer to `exports.files[].id`, not filenames or provider receipt identifiers. Preserve any independent provider byte/hash evidence; if a receipt lacks a hash, explicitly record that the receipt hash was computed from the transferred file. Host-declared receipt values are not authenticated independently by the CLI.

For a direct/raw-file route, omit `exports` and each track's `id`; supply `export: {"bytes": ACTUAL_SIZE, "sha256": "ACTUAL_HASH"}` on each track instead. Export/receipt/local-file agreement establishes declared transfer consistency, not byte identity with every byte of an original hosted container. Do not place signed URLs or credentials in this record. A combined audiovisual file is the `video` input role; its actual stream presence is determined by validation.

## Analyze in bounded sections

```bash
python3 scripts/workflow.py run --input VIDEO_FILE --audio AUDIO_FILE \
  --output-dir NEW_RUN_DIRECTORY --start 0 --end 90 --section-seconds 30 \
  --fps 2 --source-url PUBLIC_SOURCE_URL --acquisition-record ACQUISITION_JSON
```

Omit unavailable optional arguments. Keep output outside the skill and choose a nonexistent destination. The workflow hashes inputs, preflights dependencies, verifies the optional acquisition record, invokes native validation, then extracts sections with checkpoints after completed stages. Confirmed validation failure stops extraction; a review-required validation can continue extraction while retaining the review state. Completed extraction remains unreviewed.

Treat `--start`/`--end` as sampling bounds. They do not automatically declare every native track must cover those bounds. Add `--validate-interval` only for an explicit interval-coverage check. Add `--expected-duration` for independently observed overall player metadata, `--track-expectations FILE` for independently evidenced track extents/mappings, and `--decoded-equivalence` for optional original/remux comparison. Read timing-and-validation.md before interpreting these results.

Add `--analysis-copy` to request a separately verified clean audio container when the supported raw capture has complete observed media but container/index warnings. Read [format-evidence.md](format-evidence.md) for MP4 duration evidence, AV1 framing correspondence and derivative verification. Raw diagnostics and strict byte-identity outcomes remain visible; a verified derivative does not certify complete hosted-source retrieval. Acquisition records retain the browser's requested-coverage observation separately from transfer hash agreement and native interval proof.

Configure section size, `--max-sections`, `--max-frames`, `--max-probe-frames`, `--max-probe-bytes`, `--max-log-bytes`, `--max-output-bytes` and `--timeout` for the source/runtime. Frame/log/output extraction budgets are per section; the timeout applies per helper operation, not to an entire multi-section review. Resource exhaustion does not authorize a whole-file pass. Additional decoded-comparison limits are described by the validator's help.

Read `workflow.json`, `checkpoint.json`, validation reports, section manifests and process logs. Keep process exit status separate from reported completeness and inspection. A failed run retains earlier section files. Plan the next extraction in a fresh directory; automatic extraction/capture recovery is not implemented.

## Record actual inspection

Open the actual images/contact sheets using the host's image tool. Then initialize a separate ledger and append an explicit review assertion:

```bash
python3 scripts/review_ledger.py init \
  --manifest SECTION_DIRECTORY/frames_manifest.json --ledger NEW_LEDGER_JSON
python3 scripts/review_ledger.py record \
  --manifest SECTION_DIRECTORY/frames_manifest.json --ledger LEDGER_JSON \
  --frame-index 0 --frame-index 1 --reviewer REVIEWER_LABEL \
  --observation OBSERVED_CONTENT --interpretation LABELED_INTERPRETATION \
  --next-start NEXT_START_SECONDS --next-end NEXT_END_SECONDS
```

Frame indices are zero-based positions in that manifest's `frames` array. Repeat `--frame-index` only for actual examined images. The ledger verifies original source, manifest and selected image hashes, rational PTS and history integrity. It retains earlier entries through atomic append updates under an exclusive sidecar lock. Hash chaining detects accidental history modification; it is not a signature or proof that perception occurred. Keep observations separate from interpretations and describe contact-sheet resolution limits.

The sampler records the rational timestamp as `source_time_exact`. The ledger also accepts the legacy manifest spelling `source_time_seconds_exact`, validates every supplied exact value against native PTS and time base, and refuses contradictory or malformed metadata. Existing ledger attestations retain their `source_time_seconds_exact` output spelling. Neither exact field is required for older manifests whose native PTS, time base and numeric seconds already agree.

Use `--source RELOCATED_ORIGINAL` only when its hash still matches the manifest. Additional sections from the same source can be recorded using their own manifest and frame indices. Do not edit extraction manifests to manufacture review status. For relocated images, retain their original manifest references or create separately documented relocation evidence; the helper does not silently remap image paths.

After actual sound audition, use `--audio-audition-status performed --audio-observation DESCRIPTION`, including the auditioned source/interval and method in the description. Without audition, retain `not_performed` or `unsupported`. The CLI records an assertion; successful decode, sample hashes or text transcripts do not count as listening.

A stale `.lock` after an interrupted write requires explicit inspection of the ledger and running processes before removing that lock. Do not remove another active writer's lock. A refused rerun must leave prior entries and evidence untouched.
