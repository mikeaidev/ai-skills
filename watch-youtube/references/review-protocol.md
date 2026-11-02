# Timestamped video inspection

## Choose evidence for the question

Use broad distributed samples for an initial overview; use short complete sections for detailed study. For a targeted timestamp question, focus on that interval plus context. For an explicit whole-video review, continue through the requested extent and keep gaps visible. Do not label a frame-sampled review as uninterrupted audiovisual viewing.

Use 2fps initially for ordinary visual structure and 4fps around transitions. For rapid motion, acting, contact, gaze changes or animation timing, inspect a short burst of consecutive native frames or supported normal-speed playback. Coarse frame samples cannot establish subtle motion or audiovisual synchronization. Preserve intentional holds; do not interpolate source video merely for analysis.

Use `scripts/sample_frames.py --help` and extract a bounded interval into a fresh output directory. Read `media_coverage` separately from sample span. Retain boundary-context frames and their original PTS when an evidenced hold overlaps the requested start; a displayed interval does not establish that its duration was watched. The generated manifest identifies actual decoded frame PTS and begins with `reviewed=false`. Open images/contact sheets before recording observations. Images default to a maximum width of 960 pixels; pass `--max-width 0` for source resolution when UI text or small details matter. Contact sheets remain thumbnails. Mark only frames that were actually opened and examined; extraction success is not review.

Treat sample timestamps as source-media timestamps. If a derived file rebases time, retain the exact source-to-derivative mapping. A filename such as `60s.png` is not sufficient provenance if the selected frame actually has another PTS. Retain the tool manifest and command/settings.

## Checkpoint after each section

Use `scripts/review_ledger.py` as described in workflow.md to append explicit review entries without modifying extraction evidence. Verify source/image hashes and use the saved next interval. Keep a machine-readable review record with at least:

```json
{
  "source_identity": {"video_id": "...", "source_url": "...", "media_sha256": "..."},
  "requested_interval_seconds": [0, 30],
  "actual_inspected_frame_pts_seconds": [],
  "inspection_method": "decoded frames opened with image tool",
  "continuous_viewing": false,
  "audio_audition": false,
  "observations": [],
  "interpretations": [],
  "proposed_applications": [],
  "unreviewed_intervals_seconds": [],
  "next_interval_seconds": [30, 60]
}
```

Use precise observations: composition, pose, visible expression, text, framing, cut boundaries and objects. Label causal claims, intention, taste judgments and proposed reuse as interpretations or applications. Dense successive frames can support observed direction/change; one frame cannot establish movement. Attribute caption-derived information separately.

For tutorial analysis, inspect the relevant controls/keyframes and before/after states at readable resolution. Avoid inferring exact settings from a tiny contact-sheet view. For pacing, calculate source-time intervals between inspected cuts; do not estimate musical accents from silent frames. For animation, distinguish camera transforms/effects from character deformation and acting.

For audio-dependent questions, acquire and actually audition sound through a supported route. Stream metadata and successful audio decoding establish technical presence, not what was heard. Transcripts do not establish delivery, music, ambient sound, volume or synchronization.

## Final report shape

Describe retrieval and validation first, then exact visual coverage and sound-review status. Answer the user's question using timestamped evidence. State the relevant gaps and any next section. Prefer a short coverage statement over vague language such as "I watched it". Keep resumable records beside the media and use the environment's persistent storage for expensive or irreproducible checkpoints.

## Preserve review state

Create a new, previously absent output directory for every extraction. Refused reruns must leave all earlier images, manifests and annotations untouched. Keep requested coverage separate from actual sampled bounds; a successful partial extraction is still partial. Record observed timestamps and the source hash when adding review notes. Do not represent a new extraction as resuming or preserving prior review annotations automatically.
