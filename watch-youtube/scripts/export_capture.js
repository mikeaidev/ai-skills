// SPDX-License-Identifier: MIT
// Evaluate this complete expression in the page. If the host discards the
// expression result, read window.__ytMediaCapture.exportMetadata() afterwards;
// do not infer failure or create more exports solely from a null host result.
(async () => {
  "use strict";
  if (!window.__ytMediaCapture || !window.__ytMediaCapture.active) {
    throw new Error("Install capture_mse.js and capture media before exporting.");
  }
  return await window.__ytMediaCapture.prepareExports(window.__ytExportOptions || window.__ytCaptureExportOptions || {});
})();
