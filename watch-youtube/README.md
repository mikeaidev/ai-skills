# Watch YouTube

An MIT-licensed agent skill and Python/JavaScript toolkit for acquiring playable video, checking its integrity, extracting native timestamped frames and recording what was actually inspected.

The source is self-contained and requires no saved media or particular model SDK. An agent host supplies browser interaction, binary transfer and actual image/audio inspection. The local CLI analyzes files you have already acquired; it does not provide a general-purpose YouTube downloader.

## Requirements

| Component | Requirement |
| --- | --- |
| Python | 3.10 or later; runtime helpers use the standard library |
| FFmpeg and ffprobe | Available on `PATH`, with support for `-fps_mode passthrough` |
| Pillow | Optional for contact sheets; install for the complete test suite |
| Node.js | 18 or later for JavaScript regression tests |
| Agent host | File execution and actual image inspection; audio inspection when listening is requested |
| Browser fallback | A permitted main-thread MediaSource integration with page evaluation, binary downloads and raw-file transfer |

Use maintained Python and Node.js releases for new installations. The minimum versions above describe source compatibility rather than upstream security support.

Install FFmpeg/ffprobe using your platform's package manager. Install Pillow if needed:

```bash
python3 -m pip install Pillow
```

The synthetic integration tests additionally need FFmpeg encoders `libx264`, `libvpx`, `libvpx-vp9`, `libaom-av1`, `aac` and `libopus`. Missing dependencies or skipped tests are not successful validation of those stages.

## Install as an agent skill

Extract the source release so that the `watch-youtube` directory contains `SKILL.md`, `scripts/` and `references/`. Register that directory using your host's documented skill installation mechanism. Start with [SKILL.md](SKILL.md); `agents/openai.yaml` is optional UI metadata for compatible hosts.

The helper commands below also work without installing or registering a skill. From a checkout or extracted release, run them in the directory containing `SKILL.md`.

## Local quickstart

Check the runtime:

```bash
python3 scripts/workflow.py doctor
```

Choose an existing video and an interval that fits it. This example expects video at `../media/input.mp4` and writes a new run outside the source directory:

```bash
python3 scripts/workflow.py run \
  --input ../media/input.mp4 \
  --output-dir ../runs/clip-001 \
  --start 0 --end 10 --fps 2
```

Add `--audio ../media/audio.webm` for a separate audio track. Each output directory must be previously absent. Keep raw media and generated results outside the source directory; refused reruns preserve earlier files.

Read `workflow.json`, validation reports and `section_0000/frames_manifest.json`. Open the actual extracted images or contact sheets before recording observations. The [workflow reference](references/workflow.md) explains acquisition records, review-ledger commands and continuation. The [browser adapter](references/browser-adapter.md) explains acquisition through host tools; [the Skyvern reference](references/skyvern-mse.md) is one provider-specific route.

## Interpret results correctly

| State | Meaning |
| --- | --- |
| Exit 0 | No confirmed blocking technical failure; inspect completeness and individual stages |
| Exit 1 | A confirmed failure, invalid request or refused run |
| Exit 2 / `needs_review` | Evidence requires interpretation; inspect retained warnings and underlying strict outcomes |
| `unknown`, `not_compared`, `unsupported` | That particular conclusion was not established |

Acquisition, successful decoding, encoded-packet preservation, decoded-content equality, frame extraction, visual inspection, continuous viewing and audio audition are separate stages. Exit 0 does not certify whole-source completeness or that somebody watched or listened to the video.

For interval validation, add `--validate-interval`; sampling bounds alone are not a declaration of expected track coverage. Supply presentation duration or per-track timing expectations only from actual evidence. See [timing and validation](references/timing-and-validation.md) and [review protocol](references/review-protocol.md).

## Capabilities and limitations

The toolkit checks container timing, narrowly defined AV1 framing equivalence, optional clean Opus audio analysis copies and requested capture-interval coverage. See [format evidence](references/format-evidence.md) for the evidence required by each check. Container, encoded-preservation and duration diagnostics remain separate from derivative usability and actual inspection.

Automated checks and source-release verification are reproducible below. GitHub CI results, when available, describe the exact commit and environment; a configured workflow is not a claim that every matrix job has passed. Verify live acquisition in each deployment using the [distribution guidance](references/distribution.md). Offline checks do not establish whole-source completeness, continuous audiovisual viewing or audio audition.

Browser compatibility depends on the host. Worker-only playback, missing initialization bytes, access barriers, codec changes or capture limits may prevent acquisition. Interrupted-capture stitching and universal browser/model-host compatibility are not implemented. See [runtime limitations](references/runtime-limitations.md). Captions and metadata are supplementary evidence; they do not replace actual audiovisual inspection.

## Test and build

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py' -v
node tests/test_capture_mse.js
python3 scripts/check_release.py
python3 scripts/build_release.py --output ../watch-youtube.zip
```

The output ZIP must not already exist. The builder uses the explicit [release allowlist](release-files.json), rejects unexpected or private content, and generates `RELEASE_MANIFEST.json` with hashes. Rebuilding an extracted release produces identical bytes. Automated privacy patterns supplement a human review; they do not certify that every possible private reference is absent.

Keep the allowlist synchronized with intentional repository additions. Git ignore rules do not override the release builder's refusal of unexpected files.

## License and contributions

The code, documentation, tests and original icon are provided under the [MIT License](LICENSE), with attribution to the watch-youtube contributors. Preserve the copyright and permission notices when redistributing. External dependencies keep their own licenses. Retrieved videos, browser accounts and generated runtime evidence are not included in this source release or granted rights by its license.

Bug reports should include the source commit identifier, runtime/tool versions, command and sanitized stage results, plus a minimal synthetic reproduction when possible. Keep private source URLs, credentials, signed receipts and captured third-party media out of public issues. Contributions should include focused regression tests and documentation for user-visible changes.
