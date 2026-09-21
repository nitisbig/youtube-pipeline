#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

# Add beat-gen directory to sys.path
BEAT_GEN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BEAT_GEN_DIR))

from beatalign import (
    DEFAULT_VIDEO_DURATION,
    adjust_timeline_for_videos,
    extract_video_tag,
    parse_beats,
    parse_srt,
    proportional_alignment,
)


class TestVideoTagParsing(unittest.TestCase):
    def test_extract_video_tag_standard(self):
        is_vid, dur, text = extract_video_tag("beat1 [video]", "Narration here")
        self.assertTrue(is_vid)
        self.assertEqual(dur, 8.0)
        self.assertEqual(text, "Narration here")

    def test_extract_video_tag_in_text(self):
        is_vid, dur, text = extract_video_tag("beat1 ->", "[video] Look at this rushing water.")
        self.assertTrue(is_vid)
        self.assertEqual(dur, 8.0)
        self.assertEqual(text, "Look at this rushing water.")

    def test_extract_video_tag_custom_duration(self):
        is_vid, dur, text = extract_video_tag("beat1 [video: 6.5s] ->", "Narration")
        self.assertTrue(is_vid)
        self.assertEqual(dur, 6.5)
        self.assertEqual(text, "Narration")

        is_vid2, dur2, text2 = extract_video_tag("beat2 ->", "[video: 10] Narration")
        self.assertTrue(is_vid2)
        self.assertEqual(dur2, 10.0)
        self.assertEqual(text2, "Narration")

    def test_extract_video_tag_variations(self):
        for tag in ("[clip]", "[video_clip]", "(video)", "<video>", "[VIDEO]"):
            is_vid, dur, text = extract_video_tag(f"beat1 {tag} ->", "Narration")
            self.assertTrue(is_vid, f"Failed on {tag}")
            self.assertEqual(dur, 8.0)
            self.assertEqual(text, "Narration")

    def test_no_video_tag(self):
        is_vid, dur, text = extract_video_tag("beat1 ->", "A regular image beat narration.")
        self.assertFalse(is_vid)
        self.assertEqual(dur, 8.0)
        self.assertEqual(text, "A regular image beat narration.")

    def test_parse_beats_mixed_formats(self):
        md = """
beat1 [video] → First beat is a video clip.

beat[2] -> Second beat is an image.

[3] [video] -> Third beat is another video clip.

4. Fourth beat is an image.

# Image 5 [video: 5s]
Fifth beat is a custom duration video.
"""
        beats = parse_beats(md)
        self.assertEqual(len(beats), 5)
        self.assertEqual(beats[0]["image_id"], 1)
        self.assertTrue(beats[0]["is_video"])
        self.assertEqual(beats[0]["video_duration"], 8.0)
        self.assertEqual(beats[0]["text"], "First beat is a video clip.")

        self.assertEqual(beats[1]["image_id"], 2)
        self.assertFalse(beats[1]["is_video"])

        self.assertEqual(beats[2]["image_id"], 3)
        self.assertTrue(beats[2]["is_video"])
        self.assertEqual(beats[2]["video_duration"], 8.0)

        self.assertEqual(beats[3]["image_id"], 4)
        self.assertFalse(beats[3]["is_video"])

        self.assertEqual(beats[4]["image_id"], 5)
        self.assertTrue(beats[4]["is_video"])
        self.assertEqual(beats[4]["video_duration"], 5.0)


class TestTimelineCalculations(unittest.TestCase):
    def setUp(self):
        self.subtitles = [
            {"index": 1, "start": 0.0, "end": 10.0, "text": "Sub 1"},
            {"index": 2, "start": 10.0, "end": 20.0, "text": "Sub 2"},
            {"index": 3, "start": 20.0, "end": 30.0, "text": "Sub 3"},
        ]  # Total video length: 30.0s

    def test_proportional_without_videos(self):
        beats = [
            {"image_id": 1, "text": "Five words in beat one", "is_video": False},
            {"image_id": 2, "text": "Five words in beat two", "is_video": False},
        ]
        result = proportional_alignment(beats, self.subtitles)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["start_time"], 0.0)
        self.assertEqual(result[0]["end_time"], 15.0)
        self.assertEqual(result[1]["start_time"], 15.0)
        self.assertEqual(result[1]["end_time"], 30.0)

    def test_proportional_with_video(self):
        beats = [
            {"image_id": 1, "text": "Five words in beat one", "is_video": False},
            {"image_id": 2, "text": "Five words in beat two", "is_video": True, "video_duration": 8.0},
            {"image_id": 3, "text": "Five words in beat three", "is_video": False},
        ]
        # Total = 30s. Video = 8s. Remaining = 22s.
        # Beat 1 & 3 have equal words (5 each), so they share 22s equally -> 11s each.
        result = proportional_alignment(beats, self.subtitles)
        self.assertEqual(len(result), 3)

        self.assertEqual(result[0]["start_time"], 0.0)
        self.assertEqual(result[0]["end_time"], 11.0)
        self.assertAlmostEqual(result[0]["end_time"] - result[0]["start_time"], 11.0, places=3)

        # Video clip must be exactly 8.0s
        self.assertEqual(result[1]["start_time"], 11.0)
        self.assertEqual(result[1]["end_time"], 19.0)
        self.assertAlmostEqual(result[1]["end_time"] - result[1]["start_time"], 8.0, places=3)
        self.assertTrue(result[1]["is_video"])

        self.assertEqual(result[2]["start_time"], 19.0)
        self.assertEqual(result[2]["end_time"], 30.0)
        self.assertAlmostEqual(result[2]["end_time"] - result[2]["start_time"], 11.0, places=3)

    def test_adjust_timeline_for_videos_exact_reservation(self):
        # Suppose LLM returned an alignment where video beat 2 only got 3.0 seconds
        initial_alignment = [
            {"image_id": 1, "start_time": 0.0, "end_time": 10.0},
            {"image_id": 2, "start_time": 10.0, "end_time": 13.0},  # Video clip
            {"image_id": 3, "start_time": 13.0, "end_time": 30.0},
        ]
        beats = [
            {"image_id": 1, "text": "Image 1 text", "is_video": False},
            {"image_id": 2, "text": "Video 2 text", "is_video": True, "video_duration": 8.0},
            {"image_id": 3, "text": "Image 3 text", "is_video": False},
        ]
        adjusted = adjust_timeline_for_videos(
            initial_alignment,
            beats,
            video_start=0.0,
            video_end=30.0,
            default_video_duration=8.0,
        )

        self.assertEqual(len(adjusted), 3)
        # Check continuity: no gaps
        self.assertEqual(adjusted[0]["start_time"], 0.0)
        self.assertEqual(adjusted[0]["end_time"], adjusted[1]["start_time"])
        self.assertEqual(adjusted[1]["end_time"], adjusted[2]["start_time"])
        self.assertEqual(adjusted[2]["end_time"], 30.0)

        # Video clip must be exactly 8.000s
        video_dur = round(adjusted[1]["end_time"] - adjusted[1]["start_time"], 3)
        self.assertEqual(video_dur, 8.0)
        self.assertTrue(adjusted[1].get("is_video"))

    def test_adjust_timeline_multiple_videos(self):
        beats = [
            {"image_id": 1, "text": "Image 1", "is_video": False},
            {"image_id": 2, "text": "Video 2", "is_video": True, "video_duration": 8.0},
            {"image_id": 3, "text": "Image 3", "is_video": False},
            {"image_id": 4, "text": "Video 4", "is_video": True, "video_duration": 8.0},
            {"image_id": 5, "text": "Image 5", "is_video": False},
        ]
        # Total = 40s. 2 videos * 8s = 16s. Images get 24s.
        initial = [
            {"image_id": 1, "start_time": 0.0, "end_time": 8.0},
            {"image_id": 2, "start_time": 8.0, "end_time": 16.0},
            {"image_id": 3, "start_time": 16.0, "end_time": 24.0},
            {"image_id": 4, "start_time": 24.0, "end_time": 32.0},
            {"image_id": 5, "start_time": 32.0, "end_time": 40.0},
        ]
        adjusted = adjust_timeline_for_videos(
            initial, beats, video_start=0.0, video_end=40.0, default_video_duration=8.0
        )
        self.assertEqual(len(adjusted), 5)
        # Verify both videos are exactly 8.0s
        self.assertEqual(round(adjusted[1]["end_time"] - adjusted[1]["start_time"], 3), 8.0)
        self.assertEqual(round(adjusted[3]["end_time"] - adjusted[3]["start_time"], 3), 8.0)
        # Verify continuous
        for i in range(len(adjusted) - 1):
            self.assertEqual(adjusted[i]["end_time"], adjusted[i + 1]["start_time"])
        self.assertEqual(adjusted[-1]["end_time"], 40.0)

    def test_real_project_beat_files(self):
        project_root = BEAT_GEN_DIR.parent
        test_files = [
            (project_root / "out/dog-sad-story/beat.md", 20),
            (project_root / "out/how-does-himalay-form/beat.md", 10),
            (project_root / "out/monk-story/beat.md", 37),
            (project_root / "out/why-you-stay-broke/beat.md", 40),
        ]
        for path, expected_count in test_files:
            if path.exists():
                beats = parse_beats(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    len(beats),
                    expected_count,
                    f"Failed on {path.name}: expected {expected_count}, got {len(beats)}"
                )


if __name__ == "__main__":
    unittest.main()
