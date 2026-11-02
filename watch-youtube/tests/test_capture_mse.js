// SPDX-License-Identifier: MIT
"use strict";
// Run with: node --test tests/test_capture_mse.js
// Synthetic container fragments exercise framing, not media decoder support.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const os = require("node:os");
const { spawnSync } = require("node:child_process");
const { webcrypto, createHash } = require("node:crypto");

const code = fs.readFileSync(path.join(__dirname, "../scripts/capture_mse.js"), "utf8");
const exportCode = fs.readFileSync(path.join(__dirname, "../scripts/export_capture.js"), "utf8");
const cat = (...parts) => Uint8Array.from(parts.flatMap(x => Array.from(x)));
const hex = s => Uint8Array.from(Buffer.from(s.replaceAll(" ", ""), "hex"));
function box(type, payload = new Uint8Array()) {
  const bytes = new Uint8Array(payload.length + 8);
  new DataView(bytes.buffer).setUint32(0, bytes.length);
  bytes.set(Buffer.from(type), 4); bytes.set(payload, 8);
  return bytes;
}
function ebml(id, payload = new Uint8Array()) {
  assert.ok(payload.length < 127);
  return cat(hex(id), [128 + payload.length], payload);
}
const webmInit = () => cat(ebml("1a45dfa3"), hex("18538067ff"), ebml("1654ae6b"));
const webmCluster = (payload = [1, 2, 3]) => ebml("1f43b675", ebml("a3", payload));
const mp4Init = () => cat(box("ftyp", [0, 0, 0, 0]), box("moov", [4, 5, 6]));
const mp4Media = payload => cat(box("moof"), box("mdat", payload || [1, 2, 3]));

function environment(options = {}) {
  class Events {
    constructor() { this.listeners = new Map(); }
    addEventListener(type, fn) { if (!this.listeners.has(type)) this.listeners.set(type, new Set()); this.listeners.get(type).add(fn); }
    removeEventListener(type, fn) { this.listeners.get(type)?.delete(fn); }
    emit(type, target = this) { for (const fn of this.listeners.get(type) || []) fn({ type, target }); }
  }
  class SourceBuffer extends Events {
    constructor() { super(); this.updating = false; this.buffered = { length: 0 }; this.values = { timestampOffset: 0, mode: "segments", appendWindowStart: 0, appendWindowEnd: Infinity }; }
    appendBuffer() { if (this.updating) throw new Error("updating"); this.updating = true; }
    finish() { this.updating = false; this.emit("updateend"); }
    changeType() { if (this.updating) throw new Error("updating"); }
    remove() { this.updating = true; }
    abort() { this.updating = false; this.emit("abort"); this.emit("updateend"); }
  }
  for (const name of ["timestampOffset", "mode", "appendWindowStart", "appendWindowEnd"]) {
    Object.defineProperty(SourceBuffer.prototype, name, { configurable: true,
      get() { return this.values[name]; }, set(value) { this.values[name] = value; } });
  }
  class MediaSource extends Events {
    addSourceBuffer() { return new SourceBuffer(); }
    removeSourceBuffer() {}
    endOfStream() {}
  }
  const nativeAppend = SourceBuffer.prototype.appendBuffer;
  const document = new Events();
  const anchors = [], blobs = new Map(), revoked = [];
  document.querySelectorAll = () => options.media || [];
  document.createElement = () => ({ style: {}, remove() { const i = anchors.indexOf(this); if (i >= 0) anchors.splice(i, 1); } });
  document.body = document.documentElement = { appendChild(node) { anchors.push(node); } };
  const window = { SourceBuffer, MediaSource, crypto: options.crypto || webcrypto, __ytCaptureOptions: options.capture || {} };
  const context = vm.createContext({ window, document, ArrayBuffer, Uint8Array, Blob,
    URL: { createObjectURL(blob) { const id = "blob:test-" + blobs.size; blobs.set(id, blob); return id; }, revokeObjectURL(id) { revoked.push(id); blobs.delete(id); } } });
  vm.runInContext(code, context);
  const api = window.__ytMediaCapture;
  const source = new MediaSource();
  return { api, window, source, context, document, blobs, anchors, revoked, SourceBuffer, nativeAppend };
}
function append(sb, bytes) { sb.appendBuffer(bytes); sb.finish(); }
function stream(env) { return env.api.status().streams[0]; }
const plain = x => JSON.parse(JSON.stringify(x));
const range = (start, end) => ({ length: 1, start: () => start, end: () => end });
const ranges = pairs => ({ length: pairs.length, start: i => pairs[i][0], end: i => pairs[i][1] });

test("MP4 exact-byte export and hashes; wrapper export works", async () => {
  const e = environment(), sb = e.source.addSourceBuffer('video/mp4; codecs="avc1"');
  const bytes = cat(mp4Init(), mp4Media()); append(sb, bytes);
  assert.deepEqual(plain(stream(e).exportIssues), []);
  const result = await vm.runInContext(exportCode, e.context);
  const captured = Buffer.from(await e.blobs.values().next().value.arrayBuffer());
  assert.deepEqual(captured, Buffer.from(bytes));
  assert.equal(result.files[0].sha256, createHash("sha256").update(bytes).digest("hex"));
  assert.equal(result.files[0].containerTracking.codecConfigurationCompared, false);
});

test("WebM headers split across one-byte appends remain eligible", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/webm");
  const bytes = cat(webmInit(), webmCluster());
  for (const byte of bytes) append(sb, Uint8Array.of(byte));
  assert.deepEqual(plain(stream(e).exportIssues), []);
  assert.equal((await e.api.prepareExports()).files[0].bytes, bytes.length);
});

test("extended MP4 headers split across appends are bounded", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  const large = cat(hex("00000001667479700000000000000010"), box("moov"), mp4Media());
  for (let i = 0; i < large.length; i += 3) append(sb, large.subarray(i, i + 3));
  assert.deepEqual(plain(stream(e).exportIssues), []);
  assert.equal((await e.api.prepareExports()).files[0].bytes, large.length);
});

test("append byte offsets and buffer reuse cannot change captured bytes", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4"), bytes = cat(mp4Init(), mp4Media());
  const buffer = cat([99, 98], bytes, [97, 96]);
  append(sb, new DataView(buffer.buffer, 2, bytes.length)); buffer.fill(0);
  await e.api.prepareExports();
  assert.deepEqual(Buffer.from(await e.blobs.values().next().value.arrayBuffer()), Buffer.from(bytes));
});

test("pending append refuses normal export", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  sb.appendBuffer(mp4Init());
  await assert.rejects(e.api.prepareExports(), /pending/);
  sb.finish(); assert.equal((await e.api.prepareExports()).files.length, 1);
});

test("asynchronous source-buffer error refuses normal export", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  sb.appendBuffer(mp4Init()); sb.emit("error"); sb.finish();
  await assert.rejects(e.api.prepareExports(), /stream-errors/);
  assert.equal((await e.api.prepareExports({ limitedDiagnostic: true })).files[0].diagnostic, true);
});

test("overflow refuses normal export and tracks dropped bytes", async () => {
  const e = environment({ capture: { maxBytes: 8 } }), sb = e.source.addSourceBuffer("video/mp4");
  append(sb, mp4Init()); assert.ok(e.api.status().droppedBytes > 0);
  await assert.rejects(e.api.prepareExports(), /overflow/);
});

test("explicit changeType creates a separate initialized generation", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  append(sb, mp4Init()); sb.changeType("video/webm"); append(sb, cat(webmInit(), webmCluster()));
  const streams = e.api.status().streams;
  assert.equal(streams.length, 2); assert.notEqual(streams[0].id, streams[1].id);
  assert.deepEqual(plain(streams.map(x => x.exportIssues)), [[], []]);
  assert.equal((await e.api.prepareExports()).files.length, 2);
});

test("later MP4 ftyp/moov initialization is refused without comparing codecs", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  append(sb, cat(mp4Init(), mp4Media(), mp4Init()));
  assert.equal(stream(e).containerTracking.repeatedInitialization, true);
  await assert.rejects(e.api.prepareExports(), /container-framing-review/);
});

test("later MP4 moov without ftyp is also refused", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  append(sb, mp4Init()); append(sb, box("moov", [8, 9]));
  assert.equal(stream(e).containerTracking.repeatedInitialization, true);
  await assert.rejects(e.api.prepareExports(), /container-framing-review/);
});

test("later WebM Tracks within the same Segment is refused", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/webm");
  append(sb, cat(webmInit(), webmCluster(), ebml("1654ae6b", [1])));
  assert.equal(stream(e).containerTracking.repeatedInitialization, true);
  await assert.rejects(e.api.prepareExports(), /container-framing-review/);
});

test("new WebM initialization following an unknown-size Cluster is refused", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/webm");
  append(sb, cat(webmInit(), hex("1f43b675ff"), ebml("a3", [1]), webmInit()));
  assert.equal(stream(e).containerTracking.repeatedInitialization, true);
  await assert.rejects(e.api.prepareExports(), /container-framing-review/);
});

test("successive unknown-size WebM Clusters close at structural boundaries", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/webm");
  append(sb, cat(webmInit(), hex("1f43b675ff"), ebml("a3", [1]), hex("1f43b675ff"), ebml("a3", [2])));
  assert.deepEqual(plain(stream(e).exportIssues), []);
  assert.equal((await e.api.prepareExports()).files.length, 1);
});

test("initialization magic in MP4 mdat or WebM block payload is not scanned", async () => {
  const payload = cat(mp4Init(), webmInit());
  const a = environment(), mp4 = a.source.addSourceBuffer("video/mp4");
  append(mp4, cat(mp4Init(), mp4Media(payload)));
  assert.equal(stream(a).containerTracking.repeatedInitialization, false);
  await a.api.prepareExports();
  const b = environment(), webm = b.source.addSourceBuffer("video/webm");
  append(webm, cat(webmInit(), webmCluster(payload)));
  assert.equal(stream(b).containerTracking.repeatedInitialization, false);
  await b.api.prepareExports();
});

test("partial container headers and media payloads require review", async () => {
  for (const bytes of [cat(mp4Init(), mp4Media().subarray(0, 4)), cat(mp4Init(), mp4Media().subarray(0, 17))]) {
    const e = environment(), sb = e.source.addSourceBuffer("video/mp4"); append(sb, bytes);
    await assert.rejects(e.api.prepareExports(), /container-boundary-incomplete/);
  }
});

test("unsupported unknown-size EBML child is refused", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/webm");
  append(sb, cat(webmInit(), hex("1549a966ff")));
  await assert.rejects(e.api.prepareExports(), /container-framing-review/);
});

test("late installation leaves discovered-existing buffers ineligible", async () => {
  const e = environment(), sb = new e.SourceBuffer(); append(sb, mp4Init());
  assert.equal(stream(e).discoveredExisting, true);
  await assert.rejects(e.api.prepareExports(), /init-unknown|unknown-mime/);
});

test("restore releases bytes, listeners, anchors, URLs and original method", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4"); append(sb, mp4Init()); await e.api.prepareExports();
  const result = e.api.restore();
  assert.equal(result.active, false); assert.equal(e.api.status().totalBytes, 0);
  assert.equal(e.SourceBuffer.prototype.appendBuffer, e.nativeAppend);
  assert.equal(e.anchors.length, 0); assert.equal(e.blobs.size, 0); assert.equal(e.revoked.length, 1);
  await assert.rejects(e.api.prepareExports(), /restored/);
});

test("capture mutation while hashing cancels export without anchors", async () => {
  let resume;
  const crypto = { subtle: { async digest(...args) { await new Promise(resolve => { resume = resolve; }); return webcrypto.subtle.digest(...args); } } };
  const e = environment({ crypto }), sb = e.source.addSourceBuffer("video/mp4"); append(sb, mp4Init());
  const pending = e.api.prepareExports();
  while (!resume) await new Promise(resolve => setImmediate(resolve));
  append(sb, mp4Media()); resume();
  await assert.rejects(pending, /capture changed/); assert.equal(e.anchors.length, 0);
});

test("invalid MP4 lengths and MIME mismatches cannot become normal exports", async () => {
  const a = environment(), sb = a.source.addSourceBuffer("video/mp4"); append(sb, hex("0000000466747970"));
  await assert.rejects(a.api.prepareExports(), /container-framing-review/);
  const b = environment(), wb = b.source.addSourceBuffer("video/webm"); append(wb, mp4Init());
  await assert.rejects(b.api.prepareExports(), /signature-mime-mismatch|container-framing-review/);
});

test("finite WebM envelope shortage stays separate from complete media elements", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("audio/webm");
  const payload = cat(ebml("1654ae6b"), webmCluster());
  const remaining = 9;
  const bytes = cat(ebml("1a45dfa3"), hex("18538067"), [128 + payload.length + remaining], payload);
  append(sb, bytes);
  const framing = stream(e).containerTracking;
  assert.equal(framing.mediaBoundaryComplete, true);
  assert.equal(framing.structurallyComplete, false);
  assert.equal(framing.envelopeStatus, "needs_review");
  assert.equal(framing.declaredEnvelopeBytesRemaining, remaining);
  assert.equal(framing.warnings.length, 1);
  assert.deepEqual(plain(stream(e).exportIssues), []);
  const exported = await e.api.prepareExports();
  assert.equal(exported.files[0].eligibleForValidation, true);
  assert.equal(exported.files[0].containerTracking.envelopeStatus, "needs_review");
  assert.equal(exported.captureEvidence.completeness, "not_established_by_append_capture");
});

test("unknown envelope and incomplete Cluster do not share a completeness verdict", async () => {
  const complete = environment(), a = complete.source.addSourceBuffer("video/webm");
  append(a, cat(webmInit(), webmCluster()));
  assert.equal(stream(complete).containerTracking.envelopeStatus, "unknown");
  assert.equal(stream(complete).containerTracking.mediaBoundaryComplete, true);
  await complete.api.prepareExports();
  const incomplete = environment(), b = incomplete.source.addSourceBuffer("video/webm");
  append(b, cat(webmInit(), webmCluster().subarray(0, -1)));
  assert.equal(stream(incomplete).containerTracking.mediaBoundaryComplete, false);
  assert.equal(stream(incomplete).containerTracking.envelopeStatus, "incomplete");
  await assert.rejects(incomplete.api.prepareExports(), /container-boundary-incomplete/);
});

test("manifest separates overall presentation observations and each browser timeline", () => {
  const video = { tagName: "VIDEO", currentTime: 2, duration: 6.03, paused: true,
    readyState: 4, networkState: 1, playbackRate: 1, videoWidth: 64, videoHeight: 64 };
  const e = environment({ media: [video] });
  const a = e.source.addSourceBuffer("audio/webm"), v = e.source.addSourceBuffer("video/mp4");
  append(a, cat(webmInit(), webmCluster())); append(v, cat(mp4Init(), mp4Media()));
  a.buffered = range(0, 6.03); v.buffered = range(0, 6);
  const result = e.api.manifest();
  assert.equal(result.schema_version, 1);
  assert.equal(result.presentationObservations[0].duration, 6.03);
  assert.deepEqual(plain(result.streamTimelines.map(x => x.bufferedRanges)), [[[0, 6.03]], [[0, 6]]]);
  assert.equal(result.streamTimelines[0].timestampOffset, 0);
  assert.match(result.streamTimelines[0].observationBasis, /not raw packet PTS/);
  assert.equal("expectedDuration" in result.streamTimelines[0], false);
});

test("only explicit bounded source identifiers are included; page URLs are not inspected", () => {
  const e = environment({ capture: { sourceIdentity: { id: "public-id", label: "Example https://private.invalid/token", url: "https://private.invalid/" } } });
  Object.defineProperty(e.window, "location", { get() { throw new Error("must not inspect location"); } });
  assert.deepEqual(plain(e.api.manifest().sourceIdentity), { id: "public-id", label: "Example <redacted-url>" });
  assert.equal("url" in e.api.manifest().sourceIdentity, false);
  assert.deepEqual(plain(environment().api.manifest().sourceIdentity), {});
});

test("ended-source evidence is explicit and does not assert completeness", () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4"); append(sb, mp4Init());
  assert.equal(e.api.manifest().sourceStates[0].endOfStream, null);
  e.source.endOfStream();
  assert.equal(typeof e.api.manifest().sourceStates[0].endOfStream.elapsedMs, "number");
  assert.equal(e.api.manifest().completeness, "not_established_by_append_capture");
});

test("closed generations retain their own final buffered timeline", () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  append(sb, mp4Init()); sb.buffered = range(0, 3); sb.changeType("video/webm");
  append(sb, cat(webmInit(), webmCluster())); sb.buffered = range(0, 5);
  assert.deepEqual(plain(e.api.manifest().streamTimelines.map(x => x.bufferedRanges)), [[[0, 3]], [[0, 5]]]);
});

test("export metadata reads are idempotent and preserve the exported revision", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4"); append(sb, mp4Init());
  const first = await e.api.prepareExports();
  assert.equal(e.blobs.size, 1);
  const read = e.api.exportMetadata();
  assert.deepEqual(plain(read.files), plain(first.files));
  read.files[0].appendMetadata.length = 0;
  assert.equal(e.api.exportMetadata().files[0].appendMetadata.length, 1);
  assert.equal(e.blobs.size, 1);
  append(sb, mp4Media());
  const stale = e.api.exportMetadata().files[0];
  assert.equal(stale.stale, true);
  assert.equal(stale.bytes, first.files[0].bytes);
  assert.equal(stale.appendMetadata.length, 1);
  assert.equal(stale.sha256, first.files[0].sha256);
  assert.equal(stale.eligibleForValidation, true);
  const second = await e.api.prepareExports();
  assert.equal(second.files.length, 1); assert.equal(second.files[0].stale, false);
  assert.equal(second.files[0].appendMetadata.length, 2);
  assert.equal(e.api.exportMetadata().files.length, 2);
});

test("append metadata includes settled post-update state and supports explicit IIFE evaluation", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  sb.appendBuffer(mp4Init()); sb.buffered = range(0, 1); sb.finish();
  const result = await vm.runInContext("(async () => { return await window.__ytMediaCapture.prepareExports(); })()", e.context);
  assert.deepEqual(plain(result.files[0].appendMetadata[0].stateAfterUpdate.buffered), [[0, 1]]);
  const metadata = vm.runInContext("window.__ytMediaCapture.exportMetadata()", e.context);
  assert.equal(metadata.files[0].anchorId, result.files[0].anchorId);
  assert.equal(e.anchors.length, 1);
});

test("an unspecified export interval never becomes a completeness claim", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  append(sb, cat(mp4Init(), mp4Media())); sb.buffered = range(0, 5);
  const result = await e.api.prepareExports();
  assert.equal(result.files[0].requestedCoverage.status, "not_requested");
  assert.equal(result.files[0].requestedCoverage.requestedInterval, null);
  assert.equal(result.captureEvidence.completeness, "not_established_by_append_capture");
  assert.match(result.files[0].requestedCoverage.scope, /Preliminary export gate/);
});

test("explicit requested interval is recorded with fresh-buffer attribution", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  append(sb, cat(mp4Init(), mp4Media())); sb.buffered = ranges([[0, 2], [2, 5]]);
  const result = await e.api.prepareExports({ requestedInterval: { start: 0, end: 5 } });
  const checked = result.files[0].requestedCoverage;
  assert.equal(checked.status, "covered");
  assert.deepEqual(plain(checked.requestedInterval), { start: 0, end: 5, coordinate: "sourcebuffer" });
  assert.equal(checked.observed.status, "observed");
  assert.equal(checked.toleranceSeconds, 0);
  assert.equal(result.files[0].eligibleForValidation, true);
  assert.equal(result.files[0].stale, false);
  assert.equal(result.captureEvidence.requestedCoverage[0].status, "covered");
  assert.match(checked.observed.scope, /not native-packet coverage/);
});

test("partial range, internal gap and an uncovered start refuse normal exports", async () => {
  for (const buffered of [[[0, 4]], [[0, 2], [2.01, 5]], [[1, 5]]]) {
    const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
    append(sb, cat(mp4Init(), mp4Media())); sb.buffered = ranges(buffered);
    await assert.rejects(e.api.prepareExports({ requestedInterval: { start: 0, end: 5 } }), /requested-coverage-uncovered/);
    assert.equal(e.anchors.length, 0); assert.equal(e.blobs.size, 0);
    const result = await e.api.prepareExports({ requestedInterval: { start: 0, end: 5 }, limitedDiagnostic: true });
    assert.equal(result.files[0].requestedCoverage.status, "uncovered");
    assert.equal(result.files[0].diagnostic, true);
    assert.equal(result.files[0].eligibleForValidation, false);
    assert.ok(result.files[0].diagnosticReasons.includes("requested-coverage-uncovered"));
  }
});

test("per-stream interval declarations preserve legitimately different endpoints", async () => {
  const e = environment(), v = e.source.addSourceBuffer("video/mp4"), a = e.source.addSourceBuffer("audio/webm");
  append(v, cat(mp4Init(), mp4Media())); append(a, cat(webmInit(), webmCluster()));
  v.buffered = range(0, 6); a.buffered = range(0, 6.03);
  await assert.rejects(e.api.prepareExports({ requestedInterval: { start: 0, end: 6.03 } }), /requested-coverage-uncovered/);
  assert.equal(e.anchors.length, 0);
  const [video, audio] = e.api.status().streams;
  const result = await e.api.prepareExports({ streamIntervals: {
    [video.id]: { start: 0, end: 6, coordinate: "sourcebuffer" },
    [audio.id]: { start: 0, end: 6.03 }
  } });
  assert.deepEqual(plain(result.files.map(f => f.requestedCoverage.requestedInterval.end)), [6, 6.03]);
  assert.ok(result.files.every(f => f.requestedCoverage.status === "covered"));
});

test("interval options are validated before any export side effects", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  append(sb, cat(mp4Init(), mp4Media())); sb.buffered = range(0, 5);
  for (const interval of [null, [], {}, { start: "0", end: 5 }, { start: 0, end: Infinity },
    { start: -1, end: 5 }, { start: 1, end: 1 }, { start: false, end: 5 },
    { start: 0, end: 5, coordinate: "packet_pts" }, { start: 0, end: 5, unknown: true }]) {
    await assert.rejects(e.api.prepareExports({ requestedInterval: interval }), /requires finite/);
  }
  for (const streamIntervals of [null, [], {}, { unknown: { start: 0, end: 5 } }]) {
    await assert.rejects(e.api.prepareExports({ streamIntervals }), /streamIntervals/);
  }
  assert.equal(e.anchors.length, 0); assert.equal(e.blobs.size, 0);
});

test("one common interval can have explicit selected-stream overrides", async () => {
  const e = environment(), a = e.source.addSourceBuffer("audio/webm"), v = e.source.addSourceBuffer("video/mp4");
  append(a, cat(webmInit(), webmCluster())); append(v, cat(mp4Init(), mp4Media()));
  a.buffered = range(0, 6.03); v.buffered = range(0, 6);
  const video = e.api.status().streams[1];
  const result = await e.api.prepareExports({ requestedInterval: { start: 0, end: 6.03 },
    streamIntervals: { [video.id]: { start: 0, end: 6 } } });
  assert.deepEqual(plain(result.files.map(f => f.requestedCoverage.requestedInterval.end)), [6.03, 6]);
});

test("a later codec generation cannot inherit shared-buffer coverage", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  append(sb, cat(mp4Init(), mp4Media())); sb.buffered = range(0, 3);
  const first = e.api.status().streams[0].id;
  sb.changeType("video/webm"); append(sb, cat(webmInit(), webmCluster())); sb.buffered = range(0, 5);
  const second = e.api.status().streams[1].id;
  await assert.rejects(e.api.prepareExports({ ids: [second], requestedInterval: { start: 0, end: 5 } }), /requested-coverage-unestablished/);
  const diagnostic = await e.api.prepareExports({ ids: [second], requestedInterval: { start: 0, end: 5 }, limitedDiagnostic: true });
  assert.equal(diagnostic.files[0].requestedCoverage.status, "unestablished");
  assert.ok(diagnostic.files[0].requestedCoverage.observed.reasons.includes("shared-buffer-after-generation-change"));
  assert.equal(diagnostic.files[0].eligibleForValidation, false);
  const closed = await e.api.prepareExports({ ids: [first], requestedInterval: { start: 0, end: 3 } });
  assert.equal(closed.files[0].requestedCoverage.status, "covered");
  assert.deepEqual(plain(closed.files[0].requestedCoverage.observed.ranges), [[0, 3]]);
  await assert.rejects(e.api.prepareExports({ ids: [first], requestedInterval: { start: 0, end: 4 } }), /requested-coverage-uncovered/);
});

test("timeline-affecting operations invalidate requested attribution but preserve legacy export", async () => {
  const operations = [
    (e, sb) => { sb.timestampOffset = 1; },
    (e, sb) => { sb.mode = "sequence"; },
    (e, sb) => { sb.appendWindowEnd = 4; },
    (e, sb) => { sb.remove(0, 1); sb.finish(); },
    (e, sb) => sb.abort(),
    (e, sb) => e.source.removeSourceBuffer(sb),
    e => e.document.emit("seeking", { tagName: "VIDEO" }),
    e => e.document.emit("emptied", { tagName: "AUDIO" })
  ];
  for (const operation of operations) {
    const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
    append(sb, cat(mp4Init(), mp4Media())); sb.buffered = range(0, 5);
    operation(e, sb);
    await assert.rejects(e.api.prepareExports({ requestedInterval: { start: 0, end: 5 } }), /requested-coverage-unestablished/);
    assert.equal((await e.api.prepareExports()).files[0].requestedCoverage.status, "not_requested");
  }
});

test("fresh empty-buffer startup configuration does not defeat coverage attribution", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  sb.timestampOffset = 2; sb.mode = "segments"; sb.appendWindowStart = 0; sb.appendWindowEnd = 8;
  e.document.emit("emptied", { tagName: "VIDEO" });
  e.document.emit("seeking", { tagName: "VIDEO" });
  sb.remove(0, 1); sb.finish(); sb.abort();
  append(sb, cat(mp4Init(), mp4Media())); sb.buffered = range(2, 7);
  const result = await e.api.prepareExports({ requestedInterval: { start: 2, end: 7 } });
  assert.equal(result.files[0].requestedCoverage.status, "covered");
  assert.deepEqual(plain(result.files[0].requestedCoverage.observed.reasons), []);
  assert.equal(result.files[0].state.timestampOffset, 2);
});

test("no-op setters after captured appends record events without invalidating attribution", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  append(sb, cat(mp4Init(), mp4Media())); sb.buffered = range(0, 5);
  sb.timestampOffset = 0; sb.mode = "segments"; sb.appendWindowStart = 0; sb.appendWindowEnd = Infinity;
  const result = await e.api.prepareExports({ requestedInterval: { start: 0, end: 5 } });
  assert.equal(result.files[0].requestedCoverage.status, "covered");
  assert.equal(result.timelineEvents.filter(event => event.type === "property-change").length, 4);
  assert.deepEqual(plain(result.files[0].requestedCoverage.observed.reasons), []);
});

test("native coverage mutation during hashing cancels export even without new append bytes", async () => {
  for (const end of [2, 6]) {
    let resume;
    const crypto = { subtle: { async digest(...args) { await new Promise(resolve => { resume = resolve; }); return webcrypto.subtle.digest(...args); } } };
    const e = environment({ crypto }), sb = e.source.addSourceBuffer("video/mp4");
    append(sb, cat(mp4Init(), mp4Media())); sb.buffered = range(0, 5);
    const pending = e.api.prepareExports({ requestedInterval: { start: 0, end: 5 } });
    while (!resume) await new Promise(resolve => setImmediate(resolve));
    sb.buffered = range(0, end); resume();
    await assert.rejects(pending, /capture changed/);
    assert.equal(e.anchors.length, 0); assert.equal(e.blobs.size, 0);
  }
});

test("no-interval raw exports remain compatible with browser-only range eviction during hashing", async () => {
  let resume;
  const crypto = { subtle: { async digest(...args) { await new Promise(resolve => { resume = resolve; }); return webcrypto.subtle.digest(...args); } } };
  const e = environment({ crypto }), sb = e.source.addSourceBuffer("video/mp4");
  const bytes = cat(mp4Init(), mp4Media()); append(sb, bytes); sb.buffered = range(0, 5);
  const pending = e.api.prepareExports();
  while (!resume) await new Promise(resolve => setImmediate(resolve));
  sb.buffered = range(1, 5); resume();
  const result = await pending;
  assert.equal(result.files[0].requestedCoverage.status, "not_requested");
  assert.deepEqual(plain(result.files[0].requestedCoverage.observed.ranges), [[1, 5]]);
  assert.equal(result.files[0].sha256, createHash("sha256").update(bytes).digest("hex"));
});

test("caller mutation during hashing cannot upgrade the copied requested interval", async () => {
  let resume;
  const crypto = { subtle: { async digest(...args) { await new Promise(resolve => { resume = resolve; }); return webcrypto.subtle.digest(...args); } } };
  const e = environment({ crypto }), sb = e.source.addSourceBuffer("video/mp4");
  append(sb, cat(mp4Init(), mp4Media())); sb.buffered = range(0, 5);
  const interval = { start: 0, end: 5 };
  const pending = e.api.prepareExports({ requestedInterval: interval });
  while (!resume) await new Promise(resolve => setImmediate(resolve));
  interval.end = 50; resume();
  const result = await pending;
  assert.equal(result.files[0].requestedCoverage.requestedInterval.end, 5);
});

test("idempotent exports revalidate each new interval without reusing the old snapshot", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  append(sb, cat(mp4Init(), mp4Media())); sb.buffered = range(0, 5);
  const first = await e.api.prepareExports({ requestedInterval: { start: 0, end: 3 } });
  const second = await e.api.prepareExports({ requestedInterval: { start: 0, end: 5 } });
  assert.equal(e.anchors.length, 1); assert.equal(e.blobs.size, 1);
  assert.equal(first.files[0].requestedCoverage.requestedInterval.end, 3);
  assert.equal(second.files[0].requestedCoverage.requestedInterval.end, 5);
  assert.equal(second.files[0].anchorId, first.files[0].anchorId);
  await assert.rejects(e.api.prepareExports({ requestedInterval: { start: 0, end: 6 } }), /requested-coverage-uncovered/);
  assert.equal(e.api.exportMetadata().files[0].requestedCoverage.requestedInterval.end, 5);
});

test("later range changes mark requested exports stale and cannot pass from a saved snapshot", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  append(sb, cat(mp4Init(), mp4Media())); sb.buffered = range(0, 5);
  await e.api.prepareExports({ requestedInterval: { start: 0, end: 5 } });
  sb.buffered = range(0, 2);
  assert.equal(e.api.exportMetadata().files[0].stale, true);
  assert.equal(e.api.manifest().requestedCoverage[0].stale, true);
  await assert.rejects(e.api.prepareExports({ requestedInterval: { start: 0, end: 5 } }), /requested-coverage-uncovered/);
  assert.equal(e.anchors.length, 1);
});

test("interval properties are snapshotted once before validation", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  append(sb, cat(mp4Init(), mp4Media())); sb.buffered = range(0, 5);
  let reads = 0;
  const interval = { start: 0, get end() { reads++; return reads === 1 ? 5 : 50; } };
  const result = await e.api.prepareExports({ requestedInterval: interval });
  assert.equal(reads, 1);
  assert.equal(result.files[0].requestedCoverage.requestedInterval.end, 5);
});

test("nonfinite browser ranges cannot establish requested coverage", async () => {
  const e = environment(), sb = e.source.addSourceBuffer("video/mp4");
  append(sb, cat(mp4Init(), mp4Media())); sb.buffered = range(0, Infinity);
  await assert.rejects(e.api.prepareExports({ requestedInterval: { start: 0, end: 5 } }), /requested-coverage-unestablished/);
});

const ffmpegAvailable = spawnSync("ffmpeg", ["-version"], { stdio: "ignore" }).status === 0;
for (const container of ["webm", "mp4"]) {
  test(`generated ${container} fixture passes framing and exact-byte export`, { skip: !ffmpegAvailable }, async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "capture-framing-"));
    try {
      const file = path.join(dir, "fixture." + container);
      const codec = container === "webm" ? ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8"]
        : ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-movflags", "empty_moov+frag_keyframe+default_base_moof"];
      const result = spawnSync("ffmpeg", ["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=5:duration=0.6", "-an", ...codec, file], { encoding: "utf8" });
      assert.equal(result.status, 0, result.stderr);
      const bytes = fs.readFileSync(file), e = environment(), sb = e.source.addSourceBuffer("video/" + container);
      for (let i = 0; i < bytes.length; i += 17) append(sb, bytes.subarray(i, i + 17));
      assert.deepEqual(plain(stream(e).exportIssues), []);
      const exported = await e.api.prepareExports();
      assert.equal(exported.files[0].sha256, createHash("sha256").update(bytes).digest("hex"));
    } finally { fs.rmSync(dir, { recursive: true, force: true }); }
  });
}
