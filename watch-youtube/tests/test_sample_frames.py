# SPDX-License-Identifier: MIT
"""Synthetic-media regression checks; no downloaded media or private fixtures."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'sample_frames.py'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(path):
    return {str(p.relative_to(path)): sha(p) for p in path.rglob('*') if p.is_file()}


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg/ffprobe required')
class SampleFramesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='sample-frames-regression-')
        cls.base = Path(cls.tmp.name)
        cls.source = cls.base / 'source.mp4'
        cls.env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                        'testsrc2=size=64x48:rate=10:duration=4', '-c:v', 'libx264',
                        '-threads', '1', '-pix_fmt', 'yuv420p', str(cls.source)], check=True,
                       capture_output=True, env=cls.env)
        cls.shifted = cls.base / 'shifted.mkv'
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(cls.source),
                        '-c', 'copy', '-output_ts_offset', '10', str(cls.shifted)],
                       check=True, capture_output=True, env=cls.env)
        cls.hold = cls.base / 'hold.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                        'testsrc2=size=64x48:rate=10:duration=8', '-vf',
                        r'select=eq(n\,0)+eq(n\,50)+eq(n\,70)', '-fps_mode', 'vfr',
                        '-c:v', 'libx264', '-bf', '0', '-threads', '1', str(cls.hold)],
                       check=True, capture_output=True, env=cls.env)
        cls.gap = cls.base / 'gap.ivf'
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                        'testsrc2=size=64x48:rate=10:duration=4', '-vf',
                        r'select=not(between(n\,10\,11))', '-fps_mode', 'passthrough',
                        '-c:v', 'libvpx', '-threads', '1', str(cls.gap)],
                       check=True, capture_output=True, env=cls.env)
        cls.bframes = cls.base / 'bframes.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                        'testsrc2=size=96x64:rate=12:duration=4', '-f', 'lavfi', '-i',
                        'sine=frequency=660:sample_rate=48000:duration=4',
                        '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'libx264',
                        '-bf', '2', '-threads', '1', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', str(cls.bframes)],
                       check=True, capture_output=True, env=cls.env)
        cls.fractional = cls.base / 'fractional.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                        'testsrc2=size=64x48:rate=24000/1001:duration=2',
                        '-c:v', 'libx264', '-threads', '1', str(cls.fractional)],
                       check=True, capture_output=True, env=cls.env)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.case = Path(tempfile.mkdtemp(dir=self.base, prefix='case-'))

    def command(self, out, source=None, start='0', end='4', extra=()):
        return [sys.executable, str(SCRIPT), '--input', str(source or self.source),
                '--output-dir', str(out), '--start', str(start), '--end', str(end),
                '--max-width', '64', *extra]

    def run_sampler(self, out, source=None, start='0', end='4', extra=()):
        return subprocess.run(self.command(out, source, start, end, extra),
                              capture_output=True, text=True, env=self.env, timeout=60)

    def manifest(self, out):
        return json.loads((out / 'frames_manifest.json').read_text())

    def test_normal_native_samples_unreviewed(self):
        out = self.case / 'run'
        before = sha(self.source)
        result = self.run_sampler(out)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = self.manifest(out)
        self.assertEqual(len(data['frames']), 8)
        self.assertEqual([f['source_time_seconds'] for f in data['frames']],
                         [x / 2 for x in range(8)])
        self.assertFalse(data['reviewed'])
        self.assertTrue(all(not f['reviewed'] for f in data['frames']))
        self.assertFalse(data['visual_review']['performed'])
        self.assertTrue(data['input_bytes_unchanged'])
        self.assertEqual(sha(self.source), before)
        self.assertEqual(data['sampling_coverage']['status'], 'sample_positions_span_requested_interval')
        self.assertFalse(data['sampling_coverage']['continuous_coverage'])

    def test_annotated_rerun_preserves_all_files(self):
        out = self.case / 'run'
        self.assertEqual(self.run_sampler(out).returncode, 0)
        manifest = self.manifest(out)
        manifest['frames'][0]['reviewed'] = True
        manifest['visual_review'] = {'performed': True, 'observations': 'Synthetic review annotation'}
        (out / 'frames_manifest.json').write_text(json.dumps(manifest))
        checkpoint = json.loads((out / 'checkpoint.json').read_text())
        checkpoint['review_annotation'] = 'Keep this checkpoint'
        (out / 'checkpoint.json').write_text(json.dumps(checkpoint))
        before = tree(out)
        result = self.run_sampler(out)
        self.assertEqual(result.returncode, 1)
        self.assertIn('refused', result.stderr)
        self.assertEqual(tree(out), before)
        self.assertEqual(len(self.manifest(out)['frames']), 8)

    def test_manifest_only_directory_is_preserved(self):
        out = self.case / 'partial'
        out.mkdir()
        (out / 'frames_manifest.json').write_text('{"reviewed": true}')
        before = tree(out)
        self.assertEqual(self.run_sampler(out).returncode, 1)
        self.assertEqual(tree(out), before)

    def test_empty_existing_directory_is_refused(self):
        out = self.case / 'empty'
        out.mkdir()
        self.assertEqual(self.run_sampler(out).returncode, 1)
        self.assertEqual(list(out.iterdir()), [])

    def test_output_directory_symlink_is_refused(self):
        target = self.case / 'target'
        target.mkdir()
        (target / 'checkpoint.json').write_text('{"annotation":"retain"}')
        out = self.case / 'linked'
        out.symlink_to(target, target_is_directory=True)
        before = tree(target)
        self.assertEqual(self.run_sampler(out).returncode, 1)
        self.assertEqual(tree(target), before)
        self.assertTrue(out.is_symlink())

    def test_hardlink_report_aliases_are_not_overwritten(self):
        for filename in ('frames_manifest.json', 'source_probe.json', 'checkpoint.json',
                         'frames_manifest.json.tmp'):
            with self.subTest(filename=filename):
                out = self.case / filename.replace('.', '_')
                out.mkdir()
                os.link(self.source, out / filename)
                before = tree(out)
                original = sha(self.source)
                self.assertEqual(self.run_sampler(out).returncode, 1)
                self.assertEqual(tree(out), before)
                self.assertEqual(sha(self.source), original)

    def test_symlink_report_alias_is_not_overwritten(self):
        out = self.case / 'alias'
        out.mkdir()
        (out / 'source_probe.json').symlink_to(self.source)
        before = tree(out)
        self.assertEqual(self.run_sampler(out).returncode, 1)
        self.assertEqual(tree(out), before)
        self.assertTrue((out / 'source_probe.json').is_symlink())

    def test_invalid_arguments_do_not_modify_occupied_directory(self):
        out = self.case / 'occupied'
        out.mkdir()
        (out / 'checkpoint.json').write_text('reviewed')
        before = tree(out)
        for extra in (('--fps', '0'), ('--fps', 'nan'), ('--timeout', '0'), ('--max-width', '-1')):
            with self.subTest(extra=extra):
                self.assertEqual(self.run_sampler(out, extra=extra).returncode, 1)
                self.assertEqual(tree(out), before)

    def test_missing_input_does_not_modify_occupied_directory(self):
        out = self.case / 'occupied'
        out.mkdir()
        (out / 'frames_manifest.json').write_text('prior manifest')
        before = tree(out)
        self.assertEqual(self.run_sampler(out, source=self.case / 'missing.mp4').returncode, 1)
        self.assertEqual(tree(out), before)

    def test_invalid_request_does_not_create_destination(self):
        out = self.case / 'fresh'
        self.assertEqual(self.run_sampler(out, extra=('--fps', 'nan')).returncode, 1)
        self.assertFalse(out.exists())

    def test_nonzero_timestamps_remain_native(self):
        out = self.case / 'shifted'
        result = self.run_sampler(out, self.shifted, start='10', end='14')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = self.manifest(out)
        self.assertEqual([f['source_time_seconds'] for f in data['frames']],
                         [10 + x / 2 for x in range(8)])
        self.assertEqual(data['sampling_coverage']['status'], 'sample_positions_span_requested_interval')

    def test_partial_request_exposes_actual_coverage(self):
        out = self.case / 'partial'
        result = self.run_sampler(out, start='3', end='8')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = self.manifest(out)
        self.assertEqual([f['source_time_seconds'] for f in data['frames']], [3.0, 3.5])
        self.assertEqual(data['sampling_coverage']['status'], 'partial')
        self.assertEqual(data['sampling_coverage']['last_sample_pts'], 3.5)
        self.assertTrue(data['warnings'])
        self.assertFalse(data['reviewed'])

    def test_empty_requested_interval_fails_in_owned_run(self):
        out = self.case / 'beyond'
        result = self.run_sampler(out, start='8', end='10')
        self.assertEqual(result.returncode, 1)
        data = self.manifest(out)
        self.assertEqual(data['status'], 'failed')
        self.assertEqual(data['sampling_coverage']['status'], 'no_frames')
        self.assertTrue(data['input_bytes_unchanged'])

    def test_failed_probe_keeps_input_and_owns_failure_checkpoint(self):
        source = self.case / 'invalid.mp4'
        source.write_text('<html>not media</html>')
        before = sha(source)
        out = self.case / 'invalid-run'
        self.assertEqual(self.run_sampler(out, source=source).returncode, 1)
        data = self.manifest(out)
        self.assertEqual(data['status'], 'failed')
        self.assertTrue(data['input_bytes_unchanged'])
        self.assertEqual(sha(source), before)

    def test_long_hold_crossing_requested_start_is_retained(self):
        out = self.case / 'hold-context'
        result = self.run_sampler(out, self.hold, start='3', end='4')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = self.manifest(out)
        self.assertEqual(len(data['frames']), 1)
        frame = data['frames'][0]
        self.assertEqual(frame['source_time_seconds'], 0)
        self.assertEqual(frame['role'], 'boundary_context')
        self.assertEqual(frame['display_interval']['end'], 5)
        self.assertTrue(frame['display_interval']['overlaps_requested_interval'])
        self.assertEqual(frame['display_interval']['duration_basis'], 'native_decoded_frame_duration')
        self.assertEqual(data['media_coverage']['status'], 'covered_by_native_frame_durations')
        self.assertEqual(data['sampling_coverage']['status'], 'partial')
        self.assertFalse(data['visual_review']['performed'])
        self.assertTrue(data['input_bytes_unchanged'])

    def test_boundary_context_does_not_replace_first_interval_sample(self):
        out = self.case / 'crossing'
        result = self.run_sampler(out, self.hold, start='4.8', end='7.1')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = self.manifest(out)
        self.assertEqual([f['source_time_seconds'] for f in data['frames']], [0, 5, 7])
        self.assertEqual([f['role'] for f in data['frames']], ['boundary_context', 'interval_sample', 'interval_sample'])
        self.assertEqual(data['media_coverage']['status'], 'covered_by_native_frame_durations')

    def test_short_boundary_hold_has_native_evidence(self):
        out = self.case / 'short-context'
        result = self.run_sampler(out, start='0.15', end='1.2')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = self.manifest(out)
        self.assertEqual(data['frames'][0]['source_time_seconds'], 0.1)
        self.assertEqual(data['frames'][0]['role'], 'boundary_context')
        self.assertEqual(data['frames'][1]['source_time_seconds'], 0.2)
        self.assertEqual(data['media_coverage']['status'], 'covered_by_native_frame_durations')

    def test_internal_media_gap_is_separate_from_sample_span(self):
        out = self.case / 'gap'
        result = self.run_sampler(out, self.gap)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = self.manifest(out)
        self.assertEqual(data['sampling_coverage']['status'], 'sample_positions_span_requested_interval')
        self.assertEqual(data['media_coverage']['status'], 'partial')
        self.assertIn([1.0, 1.2], data['media_coverage']['uncovered_or_unestablished_intervals'])
        self.assertFalse(data['sampling_coverage']['continuous_coverage'])

    def test_fractional_native_timestamps_are_retained(self):
        from fractions import Fraction
        out = self.case / 'fractional'
        result = self.run_sampler(out, self.fractional, end='2', extra=('--fps', '24'))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = self.manifest(out)
        times = [Fraction(f['source_time_exact']) for f in data['frames']]
        self.assertEqual(times[1] - times[0], Fraction(1001, 24000))
        self.assertTrue(all(Fraction(f['source_pts']) * Fraction(f['source_time_base']) == Fraction(f['source_time_exact']) for f in data['frames']))
        self.assertEqual(data['media_coverage']['status'], 'covered_by_native_frame_durations')

    def test_bframe_section_end_includes_reordered_tail_frames(self):
        out = self.case / 'bframe-boundary'
        result = self.run_sampler(out, self.bframes, start='0', end='2')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = self.manifest(out)
        self.assertEqual(data['media_coverage']['status'], 'covered_by_native_frame_durations')
        self.assertEqual(data['media_coverage']['native_display_intervals'], [[0, 2]])
        self.assertEqual(data['media_coverage']['uncovered_or_unestablished_intervals'], [])
        self.assertEqual([f['source_time_seconds'] for f in data['frames']], [0, 0.5, 1, 1.5])
        self.assertTrue(data['input_bytes_unchanged'])

    def test_bframe_probe_eof_does_not_invent_later_coverage(self):
        out = self.case / 'bframe-eof'
        result = self.run_sampler(out, self.bframes, start='3', end='5')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = self.manifest(out)
        self.assertEqual(data['media_coverage']['status'], 'partial')
        self.assertEqual(data['media_coverage']['native_display_intervals'], [[3, 4]])
        self.assertEqual(data['media_coverage']['uncovered_or_unestablished_intervals'], [[4, 5]])
        self.assertEqual([f['source_time_seconds'] for f in data['frames']], [3, 3.5])

    def test_image_budget_failure_preserves_source_and_diagnostics(self):
        out = self.case / 'budget'
        result = self.run_sampler(out, extra=('--max-frames', '3'))
        self.assertEqual(result.returncode, 1)
        data = self.manifest(out)
        self.assertIn('Image count budget', ' '.join(data['errors']))
        self.assertFalse(data['extraction']['performed'])
        self.assertTrue(data['input_bytes_unchanged'])
        self.assertTrue((out / 'frame_timing.stdout.log').is_file())

    def test_frame_metadata_budget_is_enforced(self):
        out = self.case / 'probe-budget'
        result = self.run_sampler(out, extra=('--max-probe-frames', '3'))
        self.assertEqual(result.returncode, 1)
        data = self.manifest(out)
        self.assertIn('metadata budget', ' '.join(data['errors']))
        self.assertTrue(data['input_bytes_unchanged'])

    def test_process_log_budget_is_enforced(self):
        out = self.case / 'log-budget'
        result = self.run_sampler(out, extra=('--max-log-bytes', '64'))
        self.assertEqual(result.returncode, 1)
        data = self.manifest(out)
        self.assertIn('log byte budget', ' '.join(data['errors']))
        self.assertTrue(data['input_bytes_unchanged'])

    def test_directory_budget_is_enforced(self):
        out = self.case / 'output-budget'
        result = self.run_sampler(out, extra=('--max-output-bytes', '64'))
        self.assertEqual(result.returncode, 1)
        data = self.manifest(out)
        self.assertIn('Output byte budget', ' '.join(data['errors']))
        self.assertTrue(data['input_bytes_unchanged'])

    def test_concurrent_runs_claim_directory_once(self):
        out = self.case / 'race'
        argv = self.command(out)
        processes = [subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, env=self.env) for _ in range(2)]
        results = []
        for proc in processes:
            stdout, stderr = proc.communicate(timeout=60)
            results.append((proc.returncode, stdout, stderr))
        self.assertEqual(sorted(x[0] for x in results), [0, 1], results)
        data = self.manifest(out)
        self.assertEqual(len(data['frames']), 8)
        self.assertEqual(data['status'], 'extracted_unreviewed')


if __name__ == '__main__':
    unittest.main()
