// SPDX-License-Identifier: MIT
(() => {
  "use strict";
  if (window.__ytMediaCapture && window.__ytMediaCapture.active) {
    return window.__ytMediaCapture.status();
  }
  const options = window.__ytCaptureOptions || {};
  const requested = Number(options.maxBytes);
  const maxBytes = Number.isSafeInteger(requested) && requested > 0
    ? Math.min(requested, 256 * 1024 * 1024) : 64 * 1024 * 1024;
  const maxEvents = 20000;
  const startedAt = Date.now();
  const buffers = new WeakMap(), sources = new WeakMap(), records = new Map();
  const bufferList = [], sourceList = [], events = [], patches = [], listeners = [], exports = [];
  let active = true, totalBytes = 0, droppedBytes = 0, order = 0, metadataOverflow = false;
  let nextBuffer = 1, nextSource = 1, exporting = false, exportBytes = 0;
  const finite = n => Number.isFinite(n) ? n : null;
  const redactUrls = value => String(value).replace(/\b(?:https?|blob|data):\S+/gi, "<redacted-url>");
  const plain = value => JSON.parse(JSON.stringify(value));
  // Source identity is opt-in. Never inspect location, page title or media URLs:
  // a page can contain account details, signed URLs or unrelated private media.
  const sourceIdentity = {};
  if (options.sourceIdentity && typeof options.sourceIdentity === "object") {
    for (const key of ["id", "label"]) if (typeof options.sourceIdentity[key] === "string") {
      sourceIdentity[key] = redactUrls(options.sourceIdentity[key]).slice(0, 500);
    }
  }
  const now = () => Date.now() - startedAt;
  const safely = (fn, fallback = null) => { try { return fn(); } catch (_) { return fallback; } };
  const mediaState = () => safely(() => Array.from(document.querySelectorAll("video,audio")).map((m, i) => ({
    index: i, kind: String(m.tagName || "").toLowerCase(), currentTime: finite(m.currentTime),
    duration: finite(m.duration), paused: m.paused, readyState: m.readyState,
    networkState: m.networkState, playbackRate: m.playbackRate,
    width: m.videoWidth || 0, height: m.videoHeight || 0,
    decodedFrames: safely(() => m.getVideoPlaybackQuality().totalVideoFrames)
  })), []);
  const properties = sb => safely(() => ({
    timestampOffset: finite(sb.timestampOffset), mode: sb.mode,
    appendWindowStart: finite(sb.appendWindowStart), appendWindowEnd: finite(sb.appendWindowEnd),
    updating: !!sb.updating, buffered: safely(() => Array.from({ length: sb.buffered.length }, (_, i) =>
      [finite(sb.buffered.start(i)), finite(sb.buffered.end(i))]), [])
  }), {});
  function log(type, detail = {}) {
    if (events.length >= maxEvents) { metadataOverflow = true; return null; }
    const event = { order: ++order, elapsedMs: now(), type, ...detail };
    events.push(event);
    return event;
  }
  function listen(target, type, fn, capture = false) {
    target.addEventListener(type, fn, capture);
    listeners.push([target, type, fn, capture]);
  }
  function problem(record, code, detail) {
    record.incomplete = true;
    record.revision++;
    if (record.errors.length < maxEvents) record.errors.push({ code, elapsedMs: now(), detail: redactUrls(detail || "").slice(0, 500) });
    else metadataOverflow = true;
  }
  function prefixSignature(record) {
    const b = record.firstBytes;
    if (b.length >= 4 && b[0] === 0x1a && b[1] === 0x45 && b[2] === 0xdf && b[3] === 0xa3) return "webm-ebml";
    if (b.length >= 8) {
      const box = String.fromCharCode(...b.slice(4, 8));
      const size = b[0] * 16777216 + b[1] * 65536 + b[2] * 256 + b[3];
      if ((box === "ftyp" || box === "moov") && (size === 1 || size >= 8)) return `mp4-${box}`;
    }
    return null;
  }
  function extension(mime) {
    if (/^(video|audio)\/webm(?:\s*;|$)/i.test(mime)) return "webm";
    if (/^(video|audio|application)\/mp4(?:\s*;|$)/i.test(mime)) return "mp4";
    return null;
  }
  // Walk container boundaries, never search compressed payloads for magic bytes.
  // This is a bounded framing guard, not a codec-configuration parser. Repeated
  // initialization is conservatively refused, including identical init data.
  function containerTracker(mime) {
    const kind = extension(mime), header = [], stack = [];
    let position = 0, remaining = 0, completion = null, error = null;
    let ebml = 0, segments = 0, tracks = 0, ftyp = 0, moov = 0, initialized = false;
    let repeated = false, parsedElements = 0;
    const levelOne = new Set([0x114d9b74, 0x1549a966, 0x1654ae6b, 0x1f43b675,
      0x1c53bb6b, 0x1941a469, 0x1043a770, 0x1254c367]);
    const fail = message => { if (!error) error = message; };
    const duplicate = message => { repeated = true; fail(message); };
    const finished = () => {
      if (completion === "tracks") initialized = ebml === 1 && segments === 1 && tracks === 1;
      if (completion === "moov") initialized = true;
      completion = null;
    };
    const vintLength = (byte, max) => {
      for (let n = 1, mask = 128; n <= max; n++, mask >>= 1) if (byte & mask) return n;
      return 0;
    };
    function mp4Header() {
      if (header.length < 8) return false;
      let size = header[0] * 16777216 + header[1] * 65536 + header[2] * 256 + header[3];
      const type = String.fromCharCode(...header.slice(4, 8));
      let width = 8;
      if (size === 1) {
        if (header.length < 16) return false;
        let large = 0n;
        for (let i = 8; i < 16; i++) large = large * 256n + BigInt(header[i]);
        if (large > BigInt(Number.MAX_SAFE_INTEGER)) { fail("MP4 box size is not safely representable"); return false; }
        size = Number(large); width = 16;
      }
      if (size === 0) { fail("Open-ended MP4 box needs external review"); return false; }
      if (size < width) { fail("Invalid MP4 box length"); return false; }
      if (!parsedElements && type !== "ftyp" && type !== "moov") { fail("MP4 initialization does not start with ftyp/moov"); return false; }
      if (type === "ftyp" && ++ftyp > 1) duplicate("Repeated MP4 initialization (ftyp); codec configuration was not compared");
      if (type === "moov" && ++moov > 1) duplicate("Repeated MP4 initialization (moov); codec configuration was not compared");
      if (type === "ftyp" && initialized) duplicate("MP4 initialization restarted after moov");
      if ((type === "moof" || type === "mdat") && !initialized) fail("MP4 media precedes complete initialization");
      remaining = size - width; completion = type === "moov" ? "moov" : null;
      parsedElements++; header.length = 0;
      if (!remaining) finished();
      return !error;
    }
    function closeContainers(elementId) {
      while (stack.length) {
        const top = stack[stack.length - 1];
        if (top.end !== null) {
          if (position > top.end) { fail("EBML element exceeds its parent boundary"); return; }
          if (position === top.end) { stack.pop(); continue; }
        }
        if (top.id === 0x1f43b675 && levelOne.has(elementId)) {
          if (top.end === null) { stack.pop(); continue; }
          fail("Segment-level EBML element inside a finite Cluster");
        }
        break;
      }
    }
    function webmHeader() {
      const idWidth = vintLength(header[0], 4);
      if (!idWidth) { fail("Invalid EBML element ID"); return false; }
      if (header.length < idWidth + 1) return false;
      const sizeWidth = vintLength(header[idWidth], 8);
      if (!sizeWidth) { fail("Invalid EBML element size"); return false; }
      if (header.length < idWidth + sizeWidth) return false;
      let id = 0;
      for (let i = 0; i < idWidth; i++) id = id * 256 + header[i];
      let length = BigInt(header[idWidth] & (255 >> sizeWidth));
      for (let i = 1; i < sizeWidth; i++) length = length * 256n + BigInt(header[idWidth + i]);
      const unknown = length === (1n << BigInt(7 * sizeWidth)) - 1n;
      if (!unknown && length > BigInt(Number.MAX_SAFE_INTEGER)) { fail("EBML element size is not safely representable"); return false; }
      const width = idWidth + sizeWidth, start = position - width;
      // Header consumption advanced position; close containers at its start.
      position = start; closeContainers(id); position += width;
      if (error) return false;
      const parent = stack[stack.length - 1];
      if (parent && parent.end !== null && (position > parent.end || (!unknown && position + Number(length) > parent.end))) {
        fail("EBML child exceeds its finite parent"); return false;
      }
      if (!parsedElements && id !== 0x1a45dfa3) { fail("WebM does not start with an EBML header"); return false; }
      if (id === 0x1a45dfa3) {
        if (++ebml > 1) duplicate("Repeated WebM initialization (EBML header); codec configuration was not compared");
        if (unknown) fail("Unknown-size EBML header is unsupported");
      } else if (id === 0x18538067) {
        if (++segments > 1) duplicate("Repeated WebM Segment; codec configuration was not compared");
        if (ebml !== 1 || parent) fail("WebM Segment is not at the expected root boundary");
      } else if (id === 0x1654ae6b) {
        if (++tracks > 1) duplicate("Repeated WebM Tracks initialization; codec configuration was not compared");
        if (!parent || parent.id !== 0x18538067) fail("WebM Tracks is outside Segment");
      } else if (id === 0x1f43b675 && !initialized) fail("WebM media precedes complete Tracks initialization");
      if (error) return false;
      header.length = 0; parsedElements++;
      if (id === 0x18538067 || id === 0x1f43b675) {
        stack.push({ id, end: unknown ? null : position + Number(length) });
      } else {
        if (unknown) { fail("Unknown-size EBML element is unsupported at this level"); return false; }
        remaining = Number(length); completion = id === 0x1654ae6b ? "tracks" : null;
        if (!remaining) finished();
      }
      return true;
    }
    return {
      feed(view) {
        if (error) return;
        if (!kind) { fail("Unsupported MIME/container framing"); return; }
        for (let offset = 0; offset < view.length && !error;) {
          if (remaining) {
            const take = Math.min(remaining, view.length - offset);
            offset += take; position += take; remaining -= take;
            if (!remaining) finished();
          } else {
            header.push(view[offset++]); position++;
            if (kind === "mp4") mp4Header(); else webmHeader();
            if (header.length > 16) fail("Container header exceeds supported framing bounds");
          }
        }
      },
      summary() {
        const finiteOpen = stack.some(item => item.end !== null && position !== item.end);
        // A captured WebM Segment can retain the host file's declared length
        // while omitting index metadata. Defer that outer-envelope question to
        // packet/decode validation; never excuse a partial Cluster or element.
        const mediaContainerOpen = stack.some(item => item.id !== 0x18538067 && item.end !== null && position !== item.end);
        const mediaBoundaryComplete = !error && !header.length && !remaining && !mediaContainerOpen;
        const segment = stack.find(item => item.id === 0x18538067);
        const declaredEnvelopeBytesRemaining = segment && segment.end !== null
          ? Math.max(0, segment.end - position) : null;
        const envelopeStatus = error ? "failed" : !mediaBoundaryComplete ? "incomplete"
          : declaredEnvelopeBytesRemaining ? "needs_review"
          : stack.some(item => item.end === null) ? "unknown" : "complete";
        const warnings = [];
        if (declaredEnvelopeBytesRemaining) warnings.push("Finite WebM Segment extends beyond captured bytes; its missing bytes are not classified or repaired.");
        if (envelopeStatus === "unknown") warnings.push("Unknown-size container does not establish a complete original envelope.");
        return { container: kind, initializationComplete: initialized,
          repeatedInitialization: repeated, codecConfigurationCompared: false,
          policy: "Structural framing only; refuse any repeated initialization rather than concatenate it",
          parsedElements, bytesParsed: position, headerBytesPending: header.length,
          payloadBytesPending: remaining, finiteContainerPending: finiteOpen,
          mediaBoundaryComplete, envelopeStatus, declaredEnvelopeBytesRemaining, warnings,
          error, structurallyComplete: mediaBoundaryComplete && envelopeStatus === "complete" };
      }
    };
  }
  function newGeneration(state, mime, discoveredExisting, reason) {
    const initialState = properties(state.sb);
    const attributionReasons = [];
    if (discoveredExisting) attributionReasons.push("discovered-existing-buffer");
    if (state.generations.length) attributionReasons.push("shared-buffer-after-generation-change");
    if (!Array.isArray(initialState.buffered) || initialState.buffered.length) attributionReasons.push("buffer-not-observed-empty-at-generation-start");
    const record = {
      id: `${state.id}-g${state.generations.length}`, bufferId: state.id, sourceId: state.sourceId,
      generation: state.generations.length, mime: String(mime || ""), discoveredExisting,
      initUnknown: true, signature: null, firstBytes: [], bytes: 0, successfulAppends: 0,
      chunks: [], appendMetadata: [], errors: [], incomplete: false, overflow: false,
      droppedBytes: 0, revision: 0, createdMs: now(), reason, closed: false,
      lastState: initialState,
      coverageAttributionReasons: attributionReasons,
      containerTracker: containerTracker(mime)
    };
    state.generations.push(record); state.current = record; records.set(record.id, record);
    log("generation", { id: record.id, mime: record.mime, discoveredExisting, reason });
    return record;
  }
  function invalidateCoverage(record, reason) {
    if (!record.coverageAttributionReasons.includes(reason)) {
      record.coverageAttributionReasons.push(reason);
      record.revision++;
    }
  }
  function ensureSource(source) {
    if (sources.has(source)) return sources.get(source);
    const state = { id: `m${nextSource++}`, source, buffers: new Set(), endOfStream: null };
    sources.set(source, state); sourceList.push(state);
    for (const type of ["sourceopen", "sourceended", "sourceclose"]) {
      listen(source, type, () => {
        log(type, { sourceId: state.id });
        if (type === "sourceclose") for (const b of state.buffers) {
          if (b.pending.length) problem(b.current, "source-closed-pending");
        }
      });
    }
    return state;
  }
  function ensureBuffer(sb, mime = "", source = null, discoveredExisting = true) {
    if (buffers.has(sb)) return buffers.get(sb);
    const ms = source ? ensureSource(source) : null;
    const state = { id: `b${nextBuffer++}`, sb, sourceId: ms ? ms.id : null, generations: [], pending: [], removed: false };
    buffers.set(sb, state); bufferList.push(state); if (ms) ms.buffers.add(state);
    newGeneration(state, mime, discoveredExisting, discoveredExisting ? "discovered-existing-buffer" : "addSourceBuffer");
    for (const type of ["updatestart", "update", "updateend", "error", "abort"]) {
      listen(sb, type, () => safely(() => {
        const operation = state.pending[0];
        const record = operation ? operation.record : state.current;
        log(type, { id: record.id, operation: operation ? operation.kind : null, state: properties(sb) });
        if (type === "error") problem(record, "asynchronous-sourcebuffer-error");
        if (type === "abort" && operation) problem(record, "asynchronous-operation-aborted");
        if (type === "updateend") {
          const ended = state.pending.shift();
          record.lastState = properties(sb);
          if (ended && ended.metadata) {
            ended.metadata.updateendMs = now();
            ended.metadata.stateAfterUpdate = plain(record.lastState);
          }
        }
      }));
    }
    return state;
  }
  function patchMethod(proto, name, factory) {
    if (!proto) return;
    const descriptor = Object.getOwnPropertyDescriptor(proto, name);
    if (!descriptor || typeof descriptor.value !== "function") return;
    const replacement = factory(descriptor.value);
    Object.defineProperty(proto, name, { ...descriptor, value: replacement });
    patches.push({ proto, name, descriptor, replacement });
  }
  function patchSetter(proto, name) {
    if (!proto) return;
    const descriptor = Object.getOwnPropertyDescriptor(proto, name);
    if (!descriptor || !descriptor.set || !descriptor.configurable) return;
    const setter = function(value) {
      const before = active ? properties(this) : null;
      const result = Reflect.apply(descriptor.set, this, [value]);
      if (active) safely(() => {
        const state = ensureBuffer(this);
        const after = properties(this);
        if (state.current.successfulAppends && (!before || !Object.is(before[name], after[name]))) {
          invalidateCoverage(state.current, "property-change:" + name);
        }
        state.current.revision++;
        state.current.lastState = after;
        log("property-change", { id: state.current.id, property: name, state: after });
      });
      return result;
    };
    Object.defineProperty(proto, name, { ...descriptor, set: setter });
    patches.push({ proto, name, descriptor, replacement: setter, setter: true });
  }
  const sbProto = window.SourceBuffer && window.SourceBuffer.prototype;
  patchMethod(sbProto, "appendBuffer", original => function(data) {
    if (!active) return Reflect.apply(original, this, arguments);
    const state = safely(() => ensureBuffer(this));
    let copy = null, copyFailure = null, length = 0;
    try {
      const view = ArrayBuffer.isView(data) ? new Uint8Array(data.buffer, data.byteOffset, data.byteLength) : new Uint8Array(data);
      length = view.byteLength;
      if (totalBytes + length <= maxBytes && !metadataOverflow) copy = view.slice();
    } catch (error) { copyFailure = String(error); }
    let result;
    try { result = Reflect.apply(original, this, arguments); }
    catch (error) {
      if (state) safely(() => { problem(state.current, "synchronous-append-error", error); log("append-rejected", { id: state.current.id, bytes: length }); });
      throw error;
    }
    if (state) safely(() => {
      const record = state.current;
      record.revision++; record.successfulAppends++;
      record.lastState = properties(this);
      const metadata = log("append", { id: record.id, append: record.successfulAppends, bytes: length,
        captured: !!copy, state: properties(this), media: mediaState() });
      if (metadata) record.appendMetadata.push(metadata);
      state.pending.push({ kind: "append", record, metadata });
      if (copyFailure) problem(record, "capture-copy-error", copyFailure);
      else if (!copy) {
        record.overflow = true; record.incomplete = true; record.droppedBytes += length; droppedBytes += length;
        log("overflow", { id: record.id, bytes: length, maxBytes });
      } else {
        record.chunks.push(copy); record.bytes += copy.byteLength; totalBytes += copy.byteLength;
        if (record.firstBytes.length < 32) record.firstBytes.push(...copy.subarray(0, 32 - record.firstBytes.length));
        record.signature = prefixSignature(record);
        record.containerTracker.feed(copy);
        const framing = record.containerTracker.summary();
        if (framing.error && !record.errors.some(e => e.code === "container-framing-review")) problem(record, "container-framing-review", framing.error);
        record.initUnknown = record.discoveredExisting || !record.signature || !framing.initializationComplete;
      }
    });
    return result;
  });
  patchMethod(sbProto, "changeType", original => function(mime) {
    const result = Reflect.apply(original, this, arguments);
    if (active) safely(() => {
      const state = ensureBuffer(this); const old = state.current; old.closed = true; old.revision++;
      old.lastState = properties(this);
      if (state.pending.length) problem(old, "changeType-pending");
      log("changeType", { id: old.id, nextMime: String(mime) });
      newGeneration(state, mime, false, "changeType");
    });
    return result;
  });
  patchMethod(sbProto, "remove", original => function(start, end) {
    const result = Reflect.apply(original, this, arguments);
    if (active) safely(() => {
      const state = ensureBuffer(this); state.current.revision++;
      if (state.current.successfulAppends) invalidateCoverage(state.current, "range-removal");
      log("remove-range", { id: state.current.id, start: finite(start), end: finite(end), state: properties(this) });
      state.pending.push({ kind: "remove", record: state.current });
    });
    return result;
  });
  patchMethod(sbProto, "abort", original => function() {
    const result = Reflect.apply(original, this, arguments);
    if (active) safely(() => {
      const state = ensureBuffer(this); state.current.revision++;
      if (state.current.successfulAppends) invalidateCoverage(state.current, "buffer-abort");
      log("abort-call", { id: state.current.id, pending: state.pending.length });
      if (state.pending.length) problem(state.current, "abort-with-pending-operation");
    });
    return result;
  });
  for (const name of ["timestampOffset", "mode", "appendWindowStart", "appendWindowEnd"]) patchSetter(sbProto, name);
  const msProtos = new Set([window.MediaSource && window.MediaSource.prototype, window.ManagedMediaSource && window.ManagedMediaSource.prototype]);
  for (const proto of msProtos) {
    patchMethod(proto, "addSourceBuffer", original => function(mime) {
      const sb = Reflect.apply(original, this, arguments);
      if (active) safely(() => ensureBuffer(sb, mime, this, false));
      return sb;
    });
    patchMethod(proto, "removeSourceBuffer", original => function(sb) {
      const result = Reflect.apply(original, this, arguments);
      if (active) safely(() => {
        const state = ensureBuffer(sb); state.removed = true; state.current.revision++;
        invalidateCoverage(state.current, "sourcebuffer-removed");
        if (state.pending.length) problem(state.current, "removed-pending-buffer");
        log("removeSourceBuffer", { id: state.current.id });
      });
      return result;
    });
    patchMethod(proto, "endOfStream", original => function() {
      const result = Reflect.apply(original, this, arguments);
      if (active) safely(() => {
        const state = ensureSource(this);
        state.endOfStream = { elapsedMs: now(), reason: arguments.length ? redactUrls(arguments[0]) : null };
        log("endOfStream", { sourceId: state.id, ...state.endOfStream });
      });
      return result;
    });
  }
  for (const type of ["seeking", "seeked", "loadedmetadata", "emptied", "error", "ratechange"]) {
    listen(document, type, event => {
      if (/^(VIDEO|AUDIO)$/.test(event.target && event.target.tagName)) {
        if (type === "seeking" || type === "emptied") {
          for (const state of bufferList) if (state.current.successfulAppends) invalidateCoverage(state.current, "media-" + type);
        }
        log(`media-${type}`, { media: mediaState() });
      }
    }, true);
  }
  function normalizeInterval(value, label) {
    if (!value || typeof value !== "object" || Array.isArray(value) ||
        Object.keys(value).some(key => !["start", "end", "coordinate"].includes(key))) {
      throw new Error(label + " requires finite 0 <= start < end in the sourcebuffer coordinate.");
    }
    const { start, end, coordinate } = value;
    if (typeof start !== "number" || typeof end !== "number" ||
        !Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start ||
        (coordinate !== undefined && coordinate !== "sourcebuffer")) {
      throw new Error(label + " requires finite 0 <= start < end in the sourcebuffer coordinate.");
    }
    return { start, end, coordinate: "sourcebuffer" };
  }
  function observedCoverage(record) {
    const owner = bufferList.find(b => b.id === record.bufferId);
    const state = owner && owner.current === record ? properties(owner.sb) : plain(record.lastState);
    const reasons = record.coverageAttributionReasons.slice();
    const ranges = state.buffered;
    if (!Array.isArray(ranges) || ranges.some(pair => !Array.isArray(pair) || pair.length !== 2 ||
        !pair.every(value => typeof value === "number" && Number.isFinite(value)) || pair[1] <= pair[0])) {
      reasons.push("invalid-or-unavailable-buffer-ranges");
    }
    if (state.updating) reasons.push("buffer-updating");
    return { status: reasons.length ? "unestablished" : "observed",
      coordinate: "sourcebuffer", ranges: Array.isArray(ranges) ? plain(ranges) : [], reasons,
      attributionBasis: "Fresh buffer observed empty at its first generation; later generation ranges are not attributed from a shared buffer.",
      scope: "Browser-observed buffered ranges only; not native-packet coverage, full source retrieval or perceptual review." };
  }
  function requestedCoverage(record, interval) {
    const observed = observedCoverage(record);
    let status = interval ? "unestablished" : "not_requested";
    if (interval && observed.status === "observed") {
      let coveredTo = interval.start;
      for (const [start, end] of observed.ranges.slice().sort((a, b) => a[0] - b[0])) {
        if (end <= coveredTo) continue;
        if (start > coveredTo) break;
        coveredTo = end;
        if (coveredTo >= interval.end) break;
      }
      status = coveredTo >= interval.end ? "covered" : "uncovered";
    }
    return { status, requestedInterval: interval ? { ...interval } : null, observed,
      toleranceSeconds: 0,
      scope: "Preliminary export gate in explicit SourceBuffer seconds; native validation and source expectations remain separate." };
  }
  function exportChecks(record, interval) {
    const coverage = requestedCoverage(record, interval), flags = issues(record);
    if (interval && coverage.status !== "covered") flags.push("requested-coverage-" + coverage.status);
    return { coverage, flags, fingerprint: JSON.stringify(coverage) };
  }
  function exportStale(item) {
    return item.record.revision !== item.revision || (!!item.requestedInterval &&
      exportChecks(item.record, item.requestedInterval).fingerprint !== item.coverageFingerprint);
  }
  function issues(record) {
    const state = bufferList.find(b => b.id === record.bufferId);
    const result = [];
    if (!record.bytes) result.push("empty");
    if (record.overflow) result.push("overflow");
    if (metadataOverflow) result.push("metadata-overflow");
    if (record.errors.length) result.push("stream-errors");
    if (record.incomplete) result.push("incomplete");
    if (record.initUnknown) result.push("init-unknown");
    const framing = record.containerTracker.summary();
    if (framing.error) result.push("container-framing-review");
    if (!framing.mediaBoundaryComplete) result.push("container-boundary-incomplete");
    if (state && (state.pending.some(p => p.record === record) || (state.current === record && state.sb.updating))) result.push("pending");
    const ext = extension(record.mime);
    if (!ext) result.push("unknown-mime");
    if (record.signature && ext && !record.signature.startsWith(ext === "webm" ? "webm-" : "mp4-")) result.push("signature-mime-mismatch");
    return result;
  }
  function summary(record) {
    const state = bufferList.find(b => b.id === record.bufferId);
    return { id: record.id, bufferId: record.bufferId, sourceId: record.sourceId, generation: record.generation,
      mime: record.mime, bytes: record.bytes, successfulAppends: record.successfulAppends,
      firstBytes: record.firstBytes.slice(), signature: record.signature, initUnknown: record.initUnknown,
      containerTracking: record.containerTracker.summary(),
      discoveredExisting: record.discoveredExisting, overflow: record.overflow, droppedBytes: record.droppedBytes,
      incomplete: record.incomplete || metadataOverflow, errors: record.errors.slice(),
      pending: state ? state.pending.filter(p => p.record === record).length : 0,
      nativeUpdating: state && state.current === record ? !!state.sb.updating : false,
      removed: !!(state && state.removed), closed: record.closed,
      revision: record.revision,
      observedCaptureCoverage: observedCoverage(record),
      appendMetadataCount: record.appendMetadata.length, recentAppends: record.appendMetadata.slice(-5),
      exportIssues: issues(record), state: state && state.current === record ? properties(state.sb) : plain(record.lastState) };
  }
  const reviewEvents = () => events.filter(event => !["append", "updatestart", "update", "updateend"].includes(event.type));
  function captureEvidence() {
    return { schema_version: 1, active, sourceIdentity: { ...sourceIdentity },
      presentationObservations: mediaState(),
      streamTimelines: Array.from(records.values(), record => {
        const current = summary(record), state = current.state;
        return { id: record.id, sourceId: record.sourceId, generation: record.generation, mime: record.mime,
          bufferedRanges: plain(state.buffered || []), timestampOffset: state.timestampOffset ?? null,
          appendWindowStart: state.appendWindowStart ?? null, appendWindowEnd: state.appendWindowEnd ?? null,
          mode: state.mode || null, closedGeneration: record.closed,
          observedCaptureCoverage: observedCoverage(record),
          observationBasis: "SourceBuffer browser timeline in seconds; not raw packet PTS or an independent expected track endpoint" };
      }),
      sourceStates: sourceList.map(state => ({ id: state.id, readyState: safely(() => state.source.readyState),
        duration: finite(safely(() => state.source.duration)), endOfStream: state.endOfStream && { ...state.endOfStream } })),
      completeness: "not_established_by_append_capture",
      requestedCoverage: exports.map(item => ({ id: item.id, filename: item.filename,
        exportedRevision: item.revision, currentCaptureRevision: item.record.revision, stale: exportStale(item),
        ...plain(item.snapshot.requestedCoverage) })),
      durationPolicy: "Overall presentation duration and per-stream buffered endpoints are separate observations; unequal endpoints alone do not establish missing media." };
  }
  function exportMetadata() {
    return { schema_version: 1, active, captureEvidence: captureEvidence(), timelineEvents: plain(reviewEvents()),
      coverage: "Captured SourceBuffer append bytes; playback completeness and decoding require external verification.",
      files: exports.map(item => ({ ...plain(item.snapshot), filename: item.filename,
        anchorId: item.anchor.id, bytes: item.bytes, sha256: item.sha256,
        eligibleForValidation: !item.flags.length && !item.diagnostic,
        diagnostic: item.diagnostic, diagnosticReasons: item.flags.slice(),
        stale: exportStale(item), exportedRevision: item.revision,
        currentCaptureRevision: item.record.revision })) };
  }
  const api = {
    get active() { return active; },
    status() { return { active, startedAt, elapsedMs: now(), maxBytes, totalBytes, droppedBytes, metadataOverflow,
      capabilities: { sourceBufferPrototype: !!sbProto, mediaSourcePrototype: Array.from(msProtos).some(Boolean),
        sha256: !!(window.crypto && window.crypto.subtle), visibleDownloads: !!(document.body || document.documentElement) },
      eventCount: events.length, streamCount: records.size, streams: Array.from(records.values(), summary),
      media: mediaState(), marks: events.filter(e => e.type === "mark"), recentEvents: events.slice(-12),
      reviewEventCount: reviewEvents().length, recentReviewEvents: reviewEvents().slice(-100),
      exports: exports.map(e => ({ id: e.id, anchorId: e.anchor.id, filename: e.filename, bytes: e.bytes, sha256: e.sha256, diagnostic: e.diagnostic })) }; },
    mark(label) { return log("mark", { label: redactUrls(label).slice(0, 200), media: mediaState() }); },
    manifest: captureEvidence,
    exportMetadata,
    async prepareExports({ prefix = "media-capture", ids, limitedDiagnostic = false, requestedInterval, streamIntervals } = {}) {
      if (!active) throw new Error("Capture is restored; no export is available.");
      if (exporting) throw new Error("An export is already preparing.");
      if (!window.crypto || !window.crypto.subtle) throw new Error("SHA-256 requires crypto.subtle in a secure context.");
      if (ids !== undefined && !Array.isArray(ids)) throw new Error("ids must be an array of stream ids.");
      const selected = ids ? Array.from(new Set(ids)).map(id => {
        if (!records.has(id)) throw new Error(`Unknown stream id: ${id}`); return records.get(id);
      }) : Array.from(records.values());
      if (!selected.length) throw new Error("No captured streams.");
      const sharedInterval = requestedInterval === undefined ? null : normalizeInterval(requestedInterval, "requestedInterval");
      const perStream = new Map();
      if (streamIntervals !== undefined) {
        if (!streamIntervals || typeof streamIntervals !== "object" || Array.isArray(streamIntervals)) throw new Error("streamIntervals must be an object keyed by selected stream ids.");
        for (const [id, interval] of Object.entries(streamIntervals)) {
          if (!selected.some(record => record.id === id)) throw new Error("streamIntervals contains an unselected stream id: " + id);
          perStream.set(id, normalizeInterval(interval, "streamIntervals[" + id + "]"));
        }
        if (!sharedInterval && selected.some(record => !perStream.has(record.id))) throw new Error("streamIntervals requires every selected stream unless requestedInterval supplies a fallback.");
      }
      const safePrefix = String(prefix).replace(/[^a-zA-Z0-9_-]+/g, "_").slice(0, 80) || "media-capture";
      const prepared = [], newlyAttached = [];
      exporting = true;
      try {
        for (const record of selected) {
          const interval = perStream.get(record.id) || sharedInterval;
          const checked = exportChecks(record, interval), flags = checked.flags, ext = extension(record.mime);
          if (!ext || !record.bytes || (flags.length && !limitedDiagnostic)) throw new Error(`${record.id}: export refused (${flags.join(", ")}).`);
          const revision = record.revision;
          const filename = `${safePrefix}-${record.id}${limitedDiagnostic ? "-diagnostic" : ""}.${ext}`;
          let item = exports.find(e => e.id === record.id && e.revision === revision && e.filename === filename);
          if (!item) {
            if (exportBytes + prepared.filter(p => !p.existing).reduce((n, p) => n + p.bytes, 0) + record.bytes > maxBytes) throw new Error("Export Blob budget exceeded; transfer and restore before another capture.");
            const blob = new Blob(record.chunks.slice(), { type: record.mime });
            const digest = await window.crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
            if (!active || record.revision !== revision || (interval && exportChecks(record, interval).fingerprint !== checked.fingerprint) ||
                (!limitedDiagnostic && exportChecks(record, interval).flags.length)) throw new Error(`${record.id}: capture changed during export; retry when stable.`);
            const sha256 = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
            item = { id: record.id, revision, filename, bytes: blob.size, sha256, diagnostic: !!limitedDiagnostic,
              blob, record, flags: flags.slice(), existing: false };
          } else item = { ...item, original: item, existing: true };
          item.requestedInterval = interval ? { ...interval } : null;
          item.requestedCoverage = checked.coverage;
          item.coverageFingerprint = checked.fingerprint;
          item.flags = flags.slice();
          prepared.push(item);
        }
        for (const item of prepared) {
          const checked = exportChecks(item.record, item.requestedInterval);
          if (!active || item.record.revision !== item.revision || (item.requestedInterval && checked.fingerprint !== item.coverageFingerprint) ||
              (!limitedDiagnostic && checked.flags.length)) throw new Error(`${item.id}: capture changed during export; retry when stable.`);
          if (!item.requestedInterval) {
            item.requestedCoverage = checked.coverage;
            item.coverageFingerprint = checked.fingerprint;
          }
        }
        for (const item of prepared) if (!item.existing) {
          item.snapshot = plain({ ...summary(item.record), appendMetadata: item.record.appendMetadata,
            requestedCoverage: item.requestedCoverage });
          const anchor = document.createElement("a");
          anchor.id = `yt-media-export-${exports.length + 1}`;
          item.url = URL.createObjectURL(item.blob); item.anchor = anchor; newlyAttached.push(item);
          anchor.href = item.url; anchor.download = item.filename;
          anchor.textContent = `Download ${item.filename} (${item.bytes} bytes${item.diagnostic ? "; LIMITED DIAGNOSTIC" : ""})`;
          anchor.style.cssText = "display:block;padding:12px;margin:8px;background:#fff;color:#000;border:2px solid #333;position:relative;z-index:2147483647";
          (document.body || document.documentElement).appendChild(anchor);
          delete item.blob;
          exports.push(item); exportBytes += item.bytes;
        }
        for (const item of prepared) if (item.existing) {
          const snapshot = plain({ ...summary(item.record), appendMetadata: item.record.appendMetadata,
            requestedCoverage: item.requestedCoverage });
          Object.assign(item.original, { snapshot, requestedInterval: item.requestedInterval,
            coverageFingerprint: item.coverageFingerprint, flags: item.flags.slice() });
        }
        const result = exportMetadata(), selectedAnchors = new Set(prepared.map(item => item.anchor.id));
        result.files = result.files.filter(item => selectedAnchors.has(item.anchorId));
        return { ...result, limitedDiagnostic: !!limitedDiagnostic };
      } catch (error) {
        for (const item of newlyAttached) {
          safely(() => URL.revokeObjectURL(item.url)); safely(() => item.anchor.remove());
          const index = exports.indexOf(item);
          if (index !== -1) { exports.splice(index, 1); exportBytes -= item.bytes; }
        }
        throw error;
      } finally { exporting = false; }
    },
    restore() {
      active = false;
      for (const patch of patches.reverse()) safely(() => {
        const current = Object.getOwnPropertyDescriptor(patch.proto, patch.name);
        if (current && (patch.setter ? current.set : current.value) === patch.replacement) Object.defineProperty(patch.proto, patch.name, patch.descriptor);
      });
      for (const [target, type, fn, capture] of listeners) safely(() => target.removeEventListener(type, fn, capture));
      for (const item of exports) { safely(() => URL.revokeObjectURL(item.url)); safely(() => item.anchor.remove()); }
      const result = { active: false, releasedBytes: totalBytes, revokedExports: exports.length };
      for (const record of records.values()) record.chunks.length = 0;
      totalBytes = 0; exports.length = 0; exportBytes = 0;
      return result;
    }
  };
  window.__ytMediaCapture = api;
  log("installed", { maxBytes, sourceBufferAvailable: !!sbProto });
  return api.status();
})();
