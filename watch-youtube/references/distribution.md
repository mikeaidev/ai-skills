# Distribution, dependencies and verification

The source code, documentation, tests and original icon in this package are offered under the MIT License in `../LICENSE`. Keep that license and copyright notice with redistributed copies or substantial portions. The generic contributor attribution contains no personal identity. The package does not include downloaded videos, credentials, private URLs, account identifiers, session artifacts or third-party executable binaries.

## External dependencies

Use Python 3.10 or later and an FFmpeg/ffprobe build supporting the commands in the helpers, including `-fps_mode passthrough`. Python helpers use the standard library. Install Pillow separately only when contact sheets are wanted; extraction works without it. JavaScript regression tests require Node.js 18 or later. Browser capture requires a permitted main-thread MSE browser integration with page evaluation, binary downloads and raw-file transfer; this package supplies no browser account, hosted service or access entitlement.

FFmpeg, Pillow, Node.js and any browser/service retain their own licenses and terms; the package's MIT license does not relicense them or retrieved media. Do not bundle their binaries or downloaded media into a source release without checking the relevant redistribution requirements. Refer to the projects' official documentation for those separate components.

## Offline regression tests

For a submission check, run `python3 scripts/check_release.py --run-tests`. It builds the allowlisted release, verifies its manifest and CRC, extracts it into a fresh temporary directory, checks import origins and dependencies, runs the full Python/JavaScript suites, and confirms a byte-identical rebuild. Skips, expected failures, missing tests and incomplete Node summaries fail this release gate. This checks source integrity and local regressions, not live acquisition or perception.

From the package directory, run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py' -v
node tests/test_capture_mse.js
```

Tests generate small synthetic media in temporary directories and do not download media or require an account. Integration cases skip if required executables are missing; missing fixture encoders produce test setup failures rather than passes. The complete synthetic suite needs FFmpeg with libx264, libvpx/libvpx-vp9, libaom-av1, AAC and libopus support. Report skips and setup failures separately from passes. Temporary fixtures, generated results and caches are not release contents.

Python CLI examples must name an output directory that does not already exist. Preserve original inputs and old review manifests. A failed or refused rerun is not permission to clear an old run.

## Deterministic source release

Build outside the skill directory using an explicit file allowlist:

```bash
python3 scripts/build_release.py --output ../watch-youtube.zip
```

The builder checks source-only UTF-8 contents, unsafe paths/symlinks, unexpected files and obvious credential/private-URL patterns. It stores sorted members with fixed ZIP metadata and generates `RELEASE_MANIFEST.json` containing each other member's SHA-256 and size. Rebuilding the same source tree, including from an extracted release, produces the same bytes. Treat the manifest as a corruption/provenance aid, not a publisher signature.

Identify published source and verification results by their repository commit identifier. Keep the source ZIP's SHA-256 alongside that identifier when distributing an archive.

Keep `release-files.json` synchronized with intentional source additions. Run `--help` for output/source budgets and an optional external forbidden-literals JSON list. Keep personal exclusion lists outside the package. Automated patterns cannot guarantee the absence of every possible private reference; inspect the allowlisted contents before release. Exclude generated media/reports, receipt URLs, accounts, checkpoints, caches and third-party executable binaries.

Extract into a new directory, verify all manifest hashes and CRC, run the offline suite, and use the documented CLI with only the extracted package. Confirm that imports resolve inside that extraction. Do not bundle test-generated fixtures or runtime results into the ZIP.

## Host and model portability

Use `SKILL.md` as the entry point in ChatGPT or another tool-capable model host. The Python/JavaScript core has no model SDK or account dependency. `agents/openai.yaml` supplies optional ChatGPT UI metadata; other hosts may ignore it. Map permitted browser operations through `references/browser-adapter.md`; the Skyvern reference is one provider-specific implementation, not a requirement for all hosts.

A fresh host still needs the stated execution/media-transfer/inspection capabilities. A source ZIP cannot grant missing tools or access rights. Validate the actual deployment instead of claiming universal compatibility from offline tests.

## Live deployment verification

Offline JavaScript simulations exercise capture logic, not a real hosting service. Before claiming end-to-end support in a deployment, perform a permitted test using independently identified public media: establish playback, capture fresh buffers, export binary files, obtain download receipts, transfer bytes, verify export hashes and run full media validation. Record actual quality/coverage and each stage separately. If page evaluation or downloads are blocked, report that capability as unavailable there; do not evade the restriction. Do not claim live compatibility from mocks or a prior environment.
