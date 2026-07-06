import unittest
from unittest.mock import patch

from bot import (
    QuizEngine,
    _group_poll_explanation,
    _inline_dialogue_pool,
    _keyboard,
    _parse_play_args,
    _parse_quiz_mode,
    _question_text,
    _resolve_quiz_difficulty,
    _session_summary,
)
from wiki_dialogues import Dialogue, parse_dialogues


class ParseDialoguesTests(unittest.TestCase):
    def test_extracts_dialogue_and_cleans_wikilinks(self) -> None:
        source = """
        == Hero movement ==
        {{Aq|floryn.move09.ogg|Do you know the [[The Oasis|Oasis]]?}}
        {{aq|floryn.death.ogg|[groans]}}
        """

        self.assertEqual(
            parse_dialogues("Floryn/Audio", source),
            [
                Dialogue(
                    hero="Floryn",
                    text="Do you know the Oasis?",
                    audio_file="floryn.move09.ogg",
                    source_page="Floryn/Audio",
                )
            ],
        )

    def test_ignores_non_audio_templates(self) -> None:
        self.assertEqual(parse_dialogues("Miya/Audio", "{{Note|hello}}"), [])

    def test_assigns_skin_and_matching_portrait_from_tabber(self) -> None:
        source = """
        <tabber>
        |-|Default=
        {{Aq|floryn.select.ogg|Default line}}
        |-|Fluffy Dream=
        {{Aq|floryn.skin.select.ogg|Skin line}}
        </tabber>
        """

        dialogues = parse_dialogues(
            "Floryn/Audio",
            source,
            skin_portraits={"Fluffy Dream": "Hero1123-portrait.png"},
            default_skin_name="The Budding Hope",
        )

        self.assertEqual(dialogues[0].skin, "Default")
        self.assertEqual(dialogues[0].splash_file, "Floryn (The Budding Hope).jpg")
        self.assertEqual(dialogues[1].skin, "Fluffy Dream")
        self.assertEqual(dialogues[1].portrait_file, "Hero1123-portrait.png")
        self.assertEqual(dialogues[1].splash_file, "Floryn (Fluffy Dream).jpg")

    def test_infers_new_skin_portrait_from_numeric_audio_name(self) -> None:
        source = """
        <tabber>
        |-|Soul Reaver=
        {{Aq|Audio1097-kill02.ogg|A skin line}}
        </tabber>
        """

        dialogue = parse_dialogues("Aamon/Audio", source)[0]

        self.assertEqual(dialogue.skin, "Soul Reaver")
        self.assertEqual(dialogue.portrait_file, "Hero1097-portrait.png")


class QuizEngineTests(unittest.TestCase):
    def test_shared_lines_are_excluded(self) -> None:
        dialogues = [
            Dialogue("A", "Shared", "a.ogg", "A/Audio"),
            Dialogue("B", "Shared", "b.ogg", "B/Audio"),
            Dialogue("A", "Unique A", "a2.ogg", "A/Audio"),
            Dialogue("B", "Unique B", "b2.ogg", "B/Audio"),
            Dialogue("C", "Unique C", "c.ogg", "C/Audio"),
            Dialogue("D", "Unique D", "d.ogg", "D/Audio"),
        ]

        engine = QuizEngine(dialogues)

        self.assertNotIn("Shared", [item.text for item in engine.dialogues])

    def test_prefers_metadata_similar_distractors(self) -> None:
        dialogues = [
            Dialogue("A", "Line A", "a.ogg", "A/Audio", "Female", ("Mage",), "Magic"),
            Dialogue("B", "Line B", "b.ogg", "B/Audio", "Female", ("Mage",), "Magic"),
            Dialogue("C", "Line C", "c.ogg", "C/Audio", "Female", ("Mage",), "Magic"),
            Dialogue("D", "Line D", "d.ogg", "D/Audio", "Female", ("Mage",), "Magic"),
            Dialogue("E", "Line E", "e.ogg", "E/Audio", "Male", ("Fighter",), "Physical"),
        ]
        engine = QuizEngine(dialogues)

        with patch("bot.random.choice", return_value=dialogues[0]):
            _, choices = engine.new_question()

        self.assertEqual(set(choices), {"A", "B", "C", "D"})

    def test_hard_mode_hides_transcript(self) -> None:
        dialogue = Dialogue("A", "Secret transcript", "a.ogg", "A/Audio")

        text = _question_text(dialogue, "hard")

        self.assertNotIn("Secret transcript", text)
        self.assertIn("Transcript hidden", text)

    def test_expert_mode_prefers_other_skins_of_same_hero(self) -> None:
        dialogues = [
            Dialogue("A", "A default", "a0.ogg", "A/Audio", skin="Default"),
            Dialogue("A", "A skin one", "a1.ogg", "A/Audio", skin="Skin One"),
            Dialogue("A", "A skin two", "a2.ogg", "A/Audio", skin="Skin Two"),
            Dialogue("A", "A skin three", "a3.ogg", "A/Audio", skin="Skin Three"),
            Dialogue("B", "Line B", "b.ogg", "B/Audio"),
            Dialogue("C", "Line C", "c.ogg", "C/Audio"),
            Dialogue("D", "Line D", "d.ogg", "D/Audio"),
        ]
        engine = QuizEngine(dialogues)

        with patch("bot.random.choice", return_value=dialogues[0]):
            _, choices = engine.new_question("expert")

        self.assertEqual(
            set(choices),
            {
                "A — Default",
                "A — Skin One",
                "A — Skin Two",
                "A — Skin Three",
            },
        )

    def test_quiz_mode_defaults_to_random(self) -> None:
        self.assertEqual(_parse_quiz_mode([]), "random")

    def test_quiz_mode_accepts_case_insensitive_parameter(self) -> None:
        self.assertEqual(_parse_quiz_mode(["HARD"]), "hard")

    def test_random_mode_resolves_to_a_difficulty(self) -> None:
        with patch("bot.random.choice", return_value="expert"):
            self.assertEqual(_resolve_quiz_difficulty("random"), "expert")

    def test_easy_and_normal_use_only_default_voice_sets(self) -> None:
        dialogues = [
            Dialogue("A", "A default", "a0.ogg", "A/Audio", skin="Default"),
            Dialogue("A", "A skin", "a1.ogg", "A/Audio", skin="Special Skin"),
            Dialogue("B", "Line B", "b.ogg", "B/Audio", skin="Default"),
            Dialogue("C", "Line C", "c.ogg", "C/Audio", skin="Default"),
            Dialogue("D", "Line D", "d.ogg", "D/Audio", skin="Default"),
        ]
        engine = QuizEngine(dialogues)

        with patch("bot.random.choice", side_effect=lambda values: values[0]) as choose:
            engine.new_question("easy")
            self.assertTrue(all(item.skin == "Default" for item in choose.call_args.args[0]))

            engine.new_question("normal")
            self.assertTrue(all(item.skin == "Default" for item in choose.call_args.args[0]))

    def test_hard_can_use_skin_voice_sets(self) -> None:
        dialogues = [
            Dialogue("A", "A default", "a0.ogg", "A/Audio", skin="Default"),
            Dialogue("A", "A skin", "a1.ogg", "A/Audio", skin="Special Skin"),
            Dialogue("B", "Line B", "b.ogg", "B/Audio", skin="Default"),
            Dialogue("C", "Line C", "c.ogg", "C/Audio", skin="Default"),
            Dialogue("D", "Line D", "d.ogg", "D/Audio", skin="Default"),
        ]
        engine = QuizEngine(dialogues)

        with patch("bot.random.choice", return_value=dialogues[1]):
            question, _ = engine.new_question("hard")

        self.assertEqual(question.skin, "Special Skin")

    def test_question_pool_does_not_repeat_seen_audio(self) -> None:
        dialogues = [
            Dialogue("A", "Line A", "a.ogg", "A/Audio"),
            Dialogue("B", "Line B", "b.ogg", "B/Audio"),
            Dialogue("C", "Line C", "c.ogg", "C/Audio"),
            Dialogue("D", "Line D", "d.ogg", "D/Audio"),
        ]
        engine = QuizEngine(dialogues)
        seen = {"a.ogg"}

        with patch("bot.random.choice", side_effect=lambda values: values[0]):
            question, _ = engine.new_question("easy", seen)

        self.assertNotEqual(question.audio_file, "a.ogg")

    def test_play_arguments(self) -> None:
        self.assertEqual(_parse_play_args([]), (10, "random"))
        self.assertEqual(_parse_play_args(["20", "hard"]), (20, "hard"))
        self.assertEqual(_parse_play_args(["expert", "5"]), (5, "expert"))

    def test_session_summary(self) -> None:
        text = _session_summary(
            {"answered": 10, "correct": 8, "points": 24, "mode": "hard"}
        )

        self.assertIn("8/10", text)
        self.assertIn("80%", text)
        self.assertIn("24", text)

    def test_group_poll_explanation_includes_answer_and_is_bounded(self) -> None:
        dialogue = Dialogue("Miya", "A line", "miya.ogg", "Miya/Audio")
        text = _group_poll_explanation(dialogue)

        self.assertIn("Miya", text)
        self.assertIn("A line", text)
        self.assertLessEqual(len(text), 200)

    def test_answer_keyboard_uses_one_button_per_row(self) -> None:
        keyboard = _keyboard("question", ["A", "B", "C", "D"])

        self.assertEqual(len(keyboard.inline_keyboard), 4)
        self.assertTrue(all(len(row) == 1 for row in keyboard.inline_keyboard))

    def test_inline_pool_filters_by_hero_name(self) -> None:
        dialogues = [
            Dialogue("Miya", "Miya line", "m.ogg", "Miya/Audio"),
            Dialogue("Layla", "Layla line", "l.ogg", "Layla/Audio"),
            Dialogue("Clint", "Clint line", "c.ogg", "Clint/Audio"),
            Dialogue("Bruno", "Bruno line", "b.ogg", "Bruno/Audio"),
        ]
        engine = QuizEngine(dialogues)

        pool = _inline_dialogue_pool(engine, "miya")

        self.assertEqual({item.hero for item in pool}, {"Miya"})


if __name__ == "__main__":
    unittest.main()
