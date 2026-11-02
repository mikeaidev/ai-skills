# Browser adapter contract

## Contents

- [Required host operations](#required-host-operations)
- [Evaluation forms and return values](#evaluation-forms-and-return-values)
- [Portable capture evidence](#portable-capture-evidence)
- [Container framing and cleanup](#container-framing-and-cleanup)

Use this contract when a permitted browser can play the source but a direct
raw-file acquisition route is unavailable. Treat browser automation as a host
capability, not a bundled Python dependency. Do not assume a provider, tool name,
login session, model brand or credential exists in a new session.

## Required host operations

Discover the current tool descriptions and supported schemas. Prefer a service
connector or the host's browser skill where sufficient. Supply equivalent
operations with the available browser tool:

| Operation | Required result |
| --- | --- |
| Open or select a permitted tab | A usable page context; confirm source identity independently |
| Inspect page/player state | Actual decoded-frame advancement, quality, duration and controls |
| Evaluate page JavaScript | Access to the page's MediaSource/SourceBuffer realm |
| Click a visible download anchor | A raw binary download or receipt, not an inline base64 video |
| Transfer the download | A local file whose size and SHA-256 can be compared with export metadata |
| Inspect extracted images | Actual visual examination; extraction alone is not inspection |

In ChatGPT, discover the currently enabled browser/plugin tools and read their
browser-control instructions. Use their supported evaluation and download APIs.
Other hosts may implement the same operations through a different adapter.
Report an unavailable capability explicitly; do not invent a tool, promise a
login, bypass a restriction or assume that a prior session's receipt still works.

## Evaluation forms and return values

Install the complete `scripts/capture_mse.js` expression before a fresh source
buffer is initialized. It installs `window.__ytMediaCapture` and also evaluates
to status. Some evaluators discard that expression value while preserving its
side effects. Read state explicitly before deciding to retry:

```javascript
window.__ytMediaCapture && window.__ytMediaCapture.status()
```

If a host requires an explicit expression that returns an awaited value, use:

```javascript
(async () => {
  return await window.__ytMediaCapture.prepareExports();
})()
```

Use a bare `return` only when the tool explicitly accepts a function body. A
bare top-level `return await ...` is invalid JavaScript in expression/script
evaluators. The supplied export script is already an async IIFE expression.

After a discarded/null export result, inspect the existing exports without
creating Blobs, repeating hashes or clicking downloads again:

```javascript
window.__ytMediaCapture.exportMetadata()
```

`prepareExports` reuses unchanged exports for the same stream revision and
filename. It refuses pending appends and detects changes during hashing. Metadata
reads retain the original export snapshot; `stale: true` means capture or requested-coverage observations changed
after that file was exported. Use a stable newly prepared export when current
coverage matters. Do not combine the old hash with newer append metadata.

## Portable capture evidence

Read `window.__ytMediaCapture.manifest()` for schema-versioned capture evidence.
`prepareExports()` and `exportMetadata()` also return it as `captureEvidence`.

- `presentationObservations` contain each media element's observed duration,
  playback position, decoded-frame count and dimensions.
- `streamTimelines` retain per-generation browser buffered ranges, timestamp
  offset, append windows and mode. A closed generation retains its own final
  state. These values are browser timeline observations, not raw packet PTS.
- `sourceStates` record an observed `endOfStream` call separately. Even that
  event is not a certification of the hosted original's completeness.
- Each exported file retains bytes, SHA-256, MIME type, stream/generation IDs,
  an anchor ID, the export revision and captured append metadata. Append records
  include settled `stateAfterUpdate` where observed.

Preserve overall presentation duration and per-track endpoints separately.
Unequal audio/video endpoints alone do not establish missing media. Do not turn
captured buffered endpoints into independent expected durations automatically.
Use independently evidenced track expectations and a documented timestamp
mapping during local validation; keep unavailable expectations unknown.

The capture code never collects page URLs, titles or media request URLs.
Optionally pass a public, non-sensitive identifier/label before installation:

```javascript
window.__ytCaptureOptions = {
  sourceIdentity: { id: "public-source-id", label: "Public reference" }
};
```

Only `id` and `label` are retained, with bounded lengths and embedded URLs
redacted. Save an independently confirmed public source URL in the host's local
source record. Keep signed download receipts and credentials out of distributable
reports and the skill package.

## Container framing and cleanup

`containerTracking.mediaBoundaryComplete` describes the supported framing
guard's state at the captured boundary. `envelopeStatus` separately reports
`complete`, `unknown`, `needs_review`, `incomplete` or `failed`. A finite WebM
Segment may declare more bytes than captured even when all observed media
elements end cleanly; report `declaredEnvelopeBytesRemaining` and warnings for
external inspection. Do not classify or repair those absent bytes automatically.
Unknown-size envelopes likewise remain unknown. The legacy
`structurallyComplete` field is true only for complete supported framing and a
complete known envelope; it does not establish source completeness.

Normal export refuses partial headers/payloads, partial finite media containers,
unknown initialization, repeated initialization, overflow, asynchronous errors
and pending operations. A framing-complete export with an unresolved outer
envelope is eligible for external validation, not automatically validated.

Transfer and verify files before cleanup. Call
`window.__ytMediaCapture.restore()` to restore patched methods, remove listeners,
release captured bytes and revoke export URLs. Close only sessions opened for
the task. Worker-only MSE, unsupported container framing, page reloads and missing
page evaluation/download support may require another permitted acquisition
route; automatic capture stitching and recovery are not implemented.
