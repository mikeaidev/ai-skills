# Skyvern native-media capture

## Entry points and limits

Discover the current schemas for `skyvern_browser_session_create`, `skyvern_navigate`, `skyvern_observe`, `skyvern_click`, `skyvern_evaluate`, `skyvern_browser_session_get` and `skyvern_browser_session_close`. Names may have provider prefixes. Prefer deterministic observed selectors with direct selector mode; ordinal/ref identifiers may expire across stateless requests. Read live errors and do not invent unsupported launch or download parameters.

Use a cloud session by default for this fallback. Select a timeout covering buffering, export and transfer. Leave proxy/profile/security settings at defaults unless the user has specified otherwise. Reuse a requested active session where supported; do not silently replace it with a signed-out session or export cookies. Start public-video tasks without importing credentials unless authentication is independently required and authorized.

If evaluation returns `OP_NOT_ALLOWED`, do not enable User Scripts or route around the restriction through another injection API. Use supported inspection/download methods or report that this capture route is unavailable. Stop at independently observed access barriers and use the applicable authentication/handoff flow. This method assumes ordinary playable media; it does not implement protected-media decryption.

## Establish playback before capture

Inspect a suitable visible `video` element rather than assuming the first element is the intended content. Record:

```javascript
(() => {
  const videos = [...document.querySelectorAll('video')];
  return videos.map(v => ({
    currentTime: v.currentTime, duration: v.duration,
    width: v.videoWidth, height: v.videoHeight,
    paused: v.paused, readyState: v.readyState,
    decodedFrames: v.getVideoPlaybackQuality?.().totalVideoFrames,
    buffered: Array.from({length: v.buffered.length}, (_, i) =>
      [v.buffered.start(i), v.buffered.end(i)])
  }));
})()
```

Compare observations over time. Select requested quality through observed player controls and verify dimensions again. A page title, thumbnail, successful metadata request or blob source URL does not establish playable media. Check that an ad or another autoplay video has not replaced the requested source.

## Install, restart, collect

1. Optionally set `window.__ytCaptureOptions = {maxBytes: 64 * 1024 * 1024}` before installation. The helper defaults to 64MiB and caps the configured byte limit at 256MiB. Plan memory against actual task/session limits; this is an explicit bound, not a source-size prediction. Export/hashing can require additional memory.
2. Read `scripts/capture_mse.js` from the skill directory and pass its complete text as the evaluation expression. Keep its returned API in the page, not in a separate tool/runtime scope. Use `window.__ytMediaCapture.status()` for compact metadata; never serialize its media arrays into tool output.
3. Create fresh player buffers while retaining hooks. If the current YouTube player exposes `document.getElementById('movie_player')?.loadVideoById`, use the observed source identity when calling it:

```javascript
document.getElementById('movie_player').loadVideoById({
  videoId: 'OBSERVED_VIDEO_ID', startSeconds: 0
});
```

This is a site-specific method, not a guaranteed permanent API. Confirm it exists before use. A selected-interval attempt may use its requested start where supported; verify the resulting native coverage independently. Do not substitute a seek and assume init bytes were captured. A full navigation erases page hooks unless installed again.

4. Confirm fresh buffers, correct MIME, recognizable initialization and expected source/quality. Keep distinct buffers/generations separate. Structural header tracking is a preliminary check, not a media-integrity verdict. It follows container boundaries across appends and refuses repeated initialization in a single buffer, even if that configuration might be equivalent. Stop at an ambiguous or unsupported structure; do not concatenate changing streams or override a normal-export refusal.
5. Let the interval buffer without seeks or quality switches. The page status and later packet scan serve different purposes. The browser may evict already-buffered media, so the current buffered range alone does not describe all captured bytes.
6. Pause playback and wait for pending appends to settle. A temporary playback speed change can accelerate buffering while retaining native encoded timestamps, but record it and do not claim normal-speed audiovisual review. Restore normal speed before any actual listening/viewing assessment.

Main-thread hooks cannot capture every possible worker/alternate-player pipeline. If playback advances but no usable fresh buffers appear, identify that stage as unsupported instead of exporting an empty or unknown-init file.

## Export and transfer

Evaluate `scripts/export_capture.js`. Use `window.__ytExportOptions` to select a filename prefix and explicit IDs if needed; read the script for its exact options. Treat every buffer generation as a separate candidate. Do not concatenate codec/init changes just because their filenames look similar.

Select the current intended streams from `status()` when old/unknown buffers also exist. For a task requesting the first ten seconds, for example, set `window.__ytExportOptions = {prefix: 'reference', ids: ['ID_FROM_STATUS', 'OTHER_ID_FROM_STATUS'], requestedInterval: {start: 0, end: 10}}` before evaluating the export script. Replace both IDs and bounds with actual task values. Explicit `streamIntervals` can supply independently evidenced per-track bounds. IDs are runtime-specific. Capture eligibility only permits external validation; it is not proof of completeness or perceptual inspection.

Normal requested exports require observed coverage attributable to that captured generation. Shared SourceBuffer ranges after a codec change cannot establish a later generation's coverage. Harmless initial configuration before media and no-op assignments remain supported; material timeline changes after capture require review. Without supplied bounds, request completeness remains unestablished. Read [format-evidence.md](format-evidence.md) for stable-quality selection, whole-video coverage and bounded recapture.

Normal export refuses unsafe candidates. Use `limitedDiagnostic: true` only to preserve explicitly labeled failure evidence; never treat a diagnostic export as accepted complete media or a substitute for a fresh valid capture.

The export result supplies download selectors, true MIME/container type, byte counts and SHA-256. Click one observed anchor at a time. Keep its object URL alive until registration and transfer finish. Query the same session with `skyvern_browser_session_get`; read its returned `downloaded_files` metadata. Download the artifact URL directly as binary, then verify count/hash. Do not echo signed URLs or inline video base64 into chat or checkpoints intended for sharing.

Session file receipts may require a short asynchronous completion check. A successful click alone does not establish a finished file. If receipts do not appear, inspect the exact error/current provider documentation; do not fall back to dumping the entire file through text.

If a metadata response is capped, use documented response paging or retrieve a compact summary from saved page state. Keep byte arrays out of tool output; metadata pagination is not an inline-video transfer route.

Before cleanup, save source identity, requested/actual interval, capture status, selected IDs, export hashes and transfer results. After verified transfer, call `window.__ytMediaCapture.restore()` to release hooks/listeners/object URLs and close sessions created for the task. Preserve already-downloaded originals even if later validation fails.
