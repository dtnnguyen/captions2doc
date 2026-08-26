"""Unit tests: python -m unittest discover -s tests"""

import os
import re
import tempfile
import types
import unittest

from video_captions.cli import ensure_dir, safe_filename
from video_captions.captions import (
    Captions,
    Cue,
    fmt_ts,
    parse_vtt,
    timestamp_url,
    video_id_from_filename,
)
from video_captions.diagrams import graph_to_drawing, parse_mermaid
from video_captions.organize import (
    _system_prompt,
    build_markdown,
    estimate_input_cost,
    organize_heuristically,
)
from video_captions.prose import impersonal, to_reference_prose
from video_captions.pdf import write_pdf
from video_captions.textutil import latin1_safe, normalize_title
from video_captions.transcribe import cues_to_vtt

ROLLING_VTT = """WEBVTT

00:00:00.000 --> 00:00:02.000
the control plane decides

00:00:02.000 --> 00:00:04.000
the control plane decides
where packets go

00:00:04.000 --> 00:00:06.000
where packets go
and the data plane forwards them.
"""


class TestCaptionParsing(unittest.TestCase):
    def test_rolling_captions_are_collapsed(self):
        cues = parse_vtt(ROLLING_VTT)
        text = " ".join(c.text for c in cues)
        self.assertEqual(text.count("the control plane decides"), 1)
        self.assertEqual(text.count("where packets go"), 1)
        self.assertIn("data plane forwards", text)

    def test_tags_and_entities_are_stripped(self):
        vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n"
               "<c>routing</c> &amp; forwarding\n")
        self.assertEqual(parse_vtt(vtt)[0].text, "routing & forwarding")

    def test_timestamp_formatting(self):
        self.assertEqual(fmt_ts(65), "1:05")
        self.assertEqual(fmt_ts(3725), "1:02:05")


class TestMermaidParsing(unittest.TestCase):
    def test_edge_labels_are_not_nodes(self):
        g = parse_mermaid("flowchart TD\n A{Match?} -- yes --> B[Forward]\n"
                          " A -- no --> C((Drop))")
        self.assertEqual(set(g.nodes), {"A", "B", "C"})
        self.assertEqual([e.label for e in g.edges], ["yes", "no"])
        self.assertEqual(g.nodes["A"].shape, "diamond")
        self.assertEqual(g.nodes["C"].shape, "circle")

    def test_chained_links(self):
        g = parse_mermaid("flowchart LR\n A[One] --> B[Two] --> C[Three]")
        self.assertEqual([(e.src, e.dst) for e in g.edges], [("A", "B"), ("B", "C")])
        self.assertTrue(all(e.label == "" for e in g.edges))

    def test_pipe_labels_and_dashed_edges(self):
        g = parse_mermaid("flowchart TD\n A[X] -->|installs| B[Y]\n A -.-> C[Z]")
        self.assertEqual(g.edges[0].label, "installs")
        self.assertTrue(g.edges[1].dashed)

    def test_mindmap_builds_a_tree(self):
        g = parse_mermaid("mindmap\n  root((Topic))\n    Alpha\n      Deep\n    Beta")
        labels = {n.label for n in g.nodes.values()}
        self.assertEqual(labels, {"Topic", "Alpha", "Deep", "Beta"})
        self.assertEqual(len(g.edges), 3)

    def test_unsupported_diagram_type_returns_none(self):
        self.assertIsNone(parse_mermaid("sequenceDiagram\n A->>B: hi"))

    def test_drawing_fits_requested_width(self):
        g = parse_mermaid("flowchart LR\n" + "\n".join(
            f" N{i}[Node number {i}] --> N{i + 1}[Node number {i + 1}]"
            for i in range(8)
        ))
        drawing = graph_to_drawing(g, 400.0)
        self.assertLessEqual(drawing.width, 400.0)
        self.assertGreater(drawing.height, 0)


class TestGeneratedCaptions(unittest.TestCase):
    """Captions produced by local speech-to-text must behave like published ones."""

    def test_cues_roundtrip_through_vtt(self):
        cues = [Cue(0.0, 2.5, "The control plane computes routes."),
                Cue(2.5, 4.25, "The data plane forwards packets.")]
        parsed = parse_vtt(cues_to_vtt(cues))
        self.assertEqual([c.text for c in parsed], [c.text for c in cues])
        self.assertEqual([(c.start, c.end) for c in parsed],
                         [(c.start, c.end) for c in cues])

    def test_vtt_header_and_timestamp_format(self):
        vtt = cues_to_vtt([Cue(3661.5, 3663.0, "Late cue.")])
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertIn("01:01:01.500 --> 01:01:03.000", vtt)


class TestReferenceProse(unittest.TestCase):
    def test_small_talk_is_dropped(self):
        for line in [
            "And welcome to our session on networking.",
            "I'm really excited to dive into these concepts with you today.",
            "So, let's take a closer look at the data plane.",
            "By the end, you'll have a solid grasp of these concepts.",
            "Thanks for watching and don't forget to subscribe.",
            "In this video we will cover three topics.",
        ]:
            self.assertIsNone(impersonal(line), line)

    def test_substance_is_kept_verbatim(self):
        line = "The data plane forwards packets based on the forwarding table."
        self.assertEqual(impersonal(line), line)

    def test_first_person_is_rewritten_not_dropped(self):
        self.assertEqual(
            impersonal("We can add more forwarding capacity as needed."),
            "It is possible to add more forwarding capacity as needed.",
        )
        self.assertEqual(
            impersonal("Let's say a link goes down between two routers."),
            "Suppose a link goes down between two routers.",
        )
        self.assertEqual(
            impersonal("Think of it like a transportation system for packets."),
            "This is analogous to a transportation system for packets.",
        )
        self.assertEqual(
            impersonal("Now, the control plane recalculates the best route."),
            "The control plane recalculates the best route.",
        )

    def test_disfluencies_and_stutters_are_removed(self):
        self.assertEqual(
            impersonal("Uh, the the tariff is, um, applied at the border."),
            "The tariff is, applied at the border.",
        )

    def test_output_is_free_of_personal_pronouns(self):
        kept = to_reference_prose([
            "So we need to understand that latency matters here.",
            "Our forwarding tables are updated by the control plane.",
            "You should monitor the CPU on the route processor.",
        ])
        for line in kept:
            self.assertNotRegex(line.lower(), r"\b(i|we|you|our|your|us|my)\b")


class TestSourceLinks(unittest.TestCase):
    def test_youtube_id_recovered_from_yt_dlp_filename(self):
        self.assertEqual(
            video_id_from_filename("Some Title [UV6TFPDCMOY].en.vtt"), "UV6TFPDCMOY"
        )
        self.assertIsNone(video_id_from_filename("Some Title.en.vtt"))
        self.assertIsNone(video_id_from_filename("Talk [short].vtt"))

    def test_timestamp_links_per_host(self):
        self.assertEqual(
            timestamp_url("https://www.youtube.com/watch?v=abc", 125),
            "https://www.youtube.com/watch?v=abc&t=125s",
        )
        self.assertEqual(
            timestamp_url("https://youtu.be/abc", 60), "https://youtu.be/abc?t=60s"
        )
        self.assertEqual(
            timestamp_url("https://vimeo.com/1", 30), "https://vimeo.com/1#t=30s"
        )
        self.assertIsNone(timestamp_url("https://example.com/v", 30))
        self.assertIsNone(timestamp_url("", 30))


class TestFilenames(unittest.TestCase):
    def test_characters_illegal_on_windows_are_replaced(self):
        self.assertEqual(safe_filename('A/B\\C*D?E:F|G'), "A-B-C-D-E-F-G")

    def test_windows_reserved_device_names_are_escaped(self):
        self.assertEqual(safe_filename("CON"), "CON-video")
        self.assertEqual(safe_filename("nul.mp4"), "nul.mp4-video")

    def test_trailing_dots_and_spaces_are_trimmed(self):
        self.assertEqual(safe_filename("Ends with dot. "), "Ends with dot")

    def test_unicode_titles_survive(self):
        self.assertEqual(safe_filename("Ünïcödé Tïtlé"), "Ünïcödé Tïtlé")

    def test_empty_title_falls_back(self):
        self.assertEqual(safe_filename("///"), "video-captions")


class TestTextUtil(unittest.TestCase):
    def test_fullwidth_title_becomes_ascii(self):
        self.assertEqual(normalize_title("A： B"), "A: B")

    def test_unrenderable_characters_are_folded(self):
        self.assertEqual(latin1_safe("“q” — x → y"), '"q" - x -> y')
        self.assertEqual(latin1_safe("emoji \U0001F600 gone"), "emoji  gone")


class TestDocumentBuild(unittest.TestCase):
    def _captions(self):
        cues = [
            Cue(i * 5.0, i * 5.0 + 5.0,
                f"The control plane computes routes and the data plane forwards "
                f"packets at line rate in step {i}.")
            for i in range(24)
        ]
        cues.append(Cue(130.0, 135.0,
                        "For example, a router installs a forwarding table entry."))
        return Captions("Sample Video", "", "Channel", 135.0, cues)

    def test_offline_organizer_emits_sections_and_a_diagram(self):
        md = organize_heuristically(self._captions())
        self.assertIn("## Overview", md)
        self.assertIn("```mermaid", md)
        self.assertIn("**Example:**", md)
        self.assertIn("## Key Takeaways", md)

    def test_diagrams_can_be_disabled(self):
        self.assertNotIn("```mermaid",
                         organize_heuristically(self._captions(), diagrams=False))

    def test_document_is_impersonal(self):
        cues = list(self._captions().cues)
        cues.insert(0, Cue(0.0, 3.0, "Hi and welcome to our session today."))
        cues.append(Cue(140.0, 145.0, "Thanks for watching, and see you next time."))
        cap = Captions("Sample Video", "", "Channel", 145.0, cues)
        md = organize_heuristically(cap)
        body = "\n".join(
            ln for ln in md.splitlines() if not ln.startswith(("#", "```", "  "))
        )
        self.assertNotRegex(body.lower(), r"\b(i|we|our|us|your|let's)\b")
        self.assertNotIn("welcome", body.lower())
        self.assertNotIn("thanks for watching", body.lower())

    def test_summary_section_is_extractive_and_offline(self):
        md = organize_heuristically(self._captions())
        self.assertIn("## Summary", md)
        section = md.split("## Summary")[1].split("\n## ")[0]
        bullets = [ln[2:] for ln in section.splitlines() if ln.startswith("- ")]
        self.assertTrue(bullets)
        for bullet in bullets:      # extractive: whole sentences, not fragments
            plain = bullet.replace("**", "")
            self.assertGreater(len(plain.split()), 5)
            self.assertTrue(plain.endswith((".", "?", "!")))

    def test_brief_mode_omits_topic_sections(self):
        md = organize_heuristically(self._captions(), brief=True)
        self.assertIn("## Summary", md)
        self.assertIn("## Key Terms", md)
        self.assertNotIn("## Key Takeaways", md)
        self.assertNotIn("**Key points**", md)
        self.assertNotIn("```mermaid", md)

    def test_emphasis_is_scarce_on_short_transcripts(self):
        cues = [Cue(0.0, 3.0, "The control plane computes routes for the network.")]
        md = organize_heuristically(Captions("Clip", "", "", 3.0, cues))
        emphasised = set(re.findall(r"\*\*(.+?)\*\*", md))
        # only the fixed structural labels, no keyword highlighting
        self.assertTrue(emphasised <= {"Key points", "Example:"}, emphasised)

    def test_key_terms_are_highlighted(self):
        md = organize_heuristically(self._captions())
        self.assertIn("## Key Terms", md)
        self.assertIn("Subjects covered:", md)
        self.assertRegex(md, r"\*\*(?:control plane|data plane)\*\*")

    def test_source_link_and_section_jumps(self):
        cap = self._captions()
        cap.url = "https://www.youtube.com/watch?v=UV6TFPDCMOY"
        md = build_markdown(cap, use_llm=False)
        self.assertIn(f"**Source:** [{cap.title}]({cap.url})", md)
        self.assertIn(f"[0:00]({cap.url}&t=0s)", md)
        self.assertIn("## Source", md)

    def test_no_link_when_source_unknown(self):
        md = build_markdown(self._captions(), use_llm=False)
        self.assertNotIn("**Source:**", md)
        self.assertNotIn("## Source", md)
        self.assertIn("*Starts at 0:00*", md)

    def test_pdf_is_written(self):
        md = ("# Title\n\n**Duration:** 2:15\n\n## Topic\n\n"
              "Body text with **bold**.\n\n"
              "```mermaid\nflowchart TD\n A[One] --> B[Two]\n```\n\n"
              "| a | b |\n|---|---|\n| 1 | 2 |\n\n- bullet\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.pdf")
            write_pdf(md, path, title="Title")
            self.assertGreater(os.path.getsize(path), 1000)
            with open(path, "rb") as fh:
                self.assertTrue(fh.read(5).startswith(b"%PDF"))


class TestFolderSetup(unittest.TestCase):
    """Missing folders are created; blocked paths explain themselves."""

    def test_creates_a_missing_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "output")
            ensure_dir(target, "output")
            self.assertTrue(os.path.isdir(target))

    def test_existing_folder_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            keeper = os.path.join(tmp, "keep.txt")
            with open(keeper, "w", encoding="utf-8") as fh:
                fh.write("data")
            ensure_dir(tmp, "output")
            self.assertTrue(os.path.exists(keeper))

    def test_a_file_in_the_way_names_the_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "output")
            with open(blocker, "w", encoding="utf-8") as fh:
                fh.write("")
            with self.assertRaises(RuntimeError) as caught:
                ensure_dir(blocker, "output")
            message = str(caught.exception)
            self.assertIn("not a folder", message)
            self.assertNotIn("Errno", message)


class TestCostEstimate(unittest.TestCase):
    """The estimate must price the request that is actually sent, for free."""

    def _captions(self):
        cues = [Cue(0.0, 5.0, "The control plane computes routes.")]
        return Captions("Sample", "https://youtu.be/abc123", "Channel", 5.0, cues)

    def _client(self, seen):
        class FakeMessages:
            @staticmethod
            def count_tokens(model, system, messages):
                seen.update(model=model, system=system, messages=messages)
                return types.SimpleNamespace(input_tokens=10_000)

        return types.SimpleNamespace(messages=FakeMessages)

    def test_prices_input_tokens_at_the_model_rate(self):
        tokens, dollars = estimate_input_cost(
            self._client({}), self._captions(), model="claude-opus-5"
        )
        self.assertEqual(tokens, 10_000)
        self.assertAlmostEqual(dollars, 0.05)   # 10k tokens at $5/M

    def test_unknown_model_falls_back_to_opus_rates(self):
        _, dollars = estimate_input_cost(
            self._client({}), self._captions(), model="not-a-real-model"
        )
        self.assertAlmostEqual(dollars, 0.05)

    def test_counts_the_same_prompt_the_billed_call_sends(self):
        seen = {}
        estimate_input_cost(self._client(seen), self._captions(), diagrams=True)
        self.assertEqual(
            seen["system"], _system_prompt(self._captions(), True, False)
        )
        self.assertEqual(seen["messages"][0]["role"], "user")

    def test_brief_drops_diagrams_and_timestamps_from_the_count(self):
        seen = {}
        estimate_input_cost(self._client(seen), self._captions(), brief=True)
        self.assertNotIn("mermaid", seen["system"].lower())
        self.assertNotIn("youtu.be", seen["system"])


if __name__ == "__main__":
    unittest.main()
