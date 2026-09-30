from __future__ import annotations

import unittest

from build_frame_sync import build_rows


class BuildFrameSyncTests(unittest.TestCase):
    def test_maps_phone_timeline_to_nexus_and_vicon_frames(self) -> None:
        rows = build_rows(
            {"firstCameraFrameDeltaFromScheduledStartMilliseconds": 12.5},
            [
                {"frame": "0", "timestamp": "0.000000"},
                {"frame": "1", "timestamp": "0.016667"},
            ],
            vicon_rate_hz=100,
            vicon_pretrigger_seconds=0.25,
            vicon_first_frame=1,
        )
        self.assertEqual(rows[0]["nexus_capture_seconds"], "0.012500000")
        self.assertEqual(rows[0]["vicon_frame_float"], "27.250000")
        self.assertEqual(rows[0]["vicon_frame_nearest"], "27")
        self.assertEqual(rows[1]["vicon_frame_nearest"], "29")

    def test_requires_clock_aligned_first_frame(self) -> None:
        with self.assertRaisesRegex(ValueError, "first-camera-frame delta"):
            build_rows({}, [{"frame": "0", "timestamp": "0"}])


if __name__ == "__main__":
    unittest.main()
