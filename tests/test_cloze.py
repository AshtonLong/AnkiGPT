"""Cloze deletions are read the way Anki reads them: a deletion ends at the first "}}"."""

import pytest

from app.services.cloze import (
    cloze_answers,
    cloze_blanked,
    cloze_problem,
    cloze_prompts,
    is_valid_cloze,
    parse_cloze,
    repair_cloze,
)

# Written the natural way, each of these loses the end of its answer in Anki.
CUT_SHORT = [
    (r"Area is {{c1::\frac{a}{b}}} units", r"Area is {{c1::\frac{a}{b} }} units"),
    (r"\( v = {{c1::\sqrt{2gh}}} \)", r"\( v = {{c1::\sqrt{2gh} }} \)"),
    (r"{{c1::\(\frac{1}{x^{2}}\)}} is the derivative", r"{{c1::\(\frac{1}{x^{2} }\)}} is the derivative"),
    (r"{{c1::\{1,2\}}} is a set", r"{{c1::\{1,2\} }} is a set"),
    (r"{{c1::\mathbf{F}}} = m{{c2::\mathbf{a}}}", r"{{c1::\mathbf{F} }} = m{{c2::\mathbf{a} }}"),
    (r"{{c1::a {{c2::\frac{a}{b}}}}} end", r"{{c1::a {{c2::\frac{a}{b} }}}} end"),
]

# Anki already reads these as they were meant.
SOUND = [
    "The capital is {{c1::Paris}}.",
    "{{c1::A}} binds {{c2::B}}",
    r"\( E = mc^{{c1::2}} \)",
    r"\( x^{{{c1::2}}} + 1 \)",
    r"The ratio \(\frac{a}{b}\) is {{c1::constant}}.",
    "{{c1::ATP synthase::enzyme}} makes ATP",
    r"{{c1::x^{2}::power}}",
    "{{c1::if (x) {}} opens a block",
    "{{c2::Only the second}} number",
]


class TestRepair:
    @pytest.mark.parametrize("written, repaired", CUT_SHORT)
    def test_a_brace_before_the_close_gets_a_space(self, written, repaired):
        assert not is_valid_cloze(written)
        assert repair_cloze(written) == repaired
        assert is_valid_cloze(repaired)

    @pytest.mark.parametrize("written, repaired", CUT_SHORT)
    def test_the_answer_is_what_the_author_wrote(self, written, repaired):
        # The parser reads the unrepaired text as it was meant, braces and all; the repair
        # adds spaces and nothing else.
        assert [a.replace(" ", "") for a in cloze_answers(written)] == [a.replace(" ", "") for a in cloze_answers(repaired)]
        assert cloze_answers(written)[0].count("{") == cloze_answers(written)[0].count("}")

    @pytest.mark.parametrize("text", SOUND + [repaired for _written, repaired in CUT_SHORT])
    def test_sound_text_comes_back_unchanged(self, text):
        assert repair_cloze(text, renumber=False) == text
        assert is_valid_cloze(text)

    def test_numbers_are_made_to_run_from_one(self):
        assert repair_cloze("{{c0::zero}} then {{c1::one}}") == "{{c1::zero}} then {{c2::one}}"
        assert repair_cloze("{{c2::a}} {{c5::b}} {{c2::c}}") == "{{c1::a}} {{c2::b}} {{c1::c}}"
        # What a student typed keeps its numbers; only the braces are made safe.
        assert repair_cloze(r"{{c2::\frac{a}{b}}}", renumber=False) == r"{{c2::\frac{a}{b} }}"

    def test_text_without_a_deletion_is_left_alone(self):
        assert repair_cloze("No deletion, just }} and {braces}.") == "No deletion, just }} and {braces}."
        assert repair_cloze("") == "" and repair_cloze(None) == ""

    def test_junk_with_hundreds_of_markers_is_not_followed_down(self):
        text = "{{c1::" * 5000 + "x" + "}}" * 5000
        assert parse_cloze(text).deletions == [] and repair_cloze(text) == text
        assert "at most" in cloze_problem(text)

    def test_nesting_is_sound_only_as_deep_as_anki_follows_it(self):
        def nested(depth):
            return "{{c1::" * depth + "x" + "}}" * depth

        assert len(parse_cloze(nested(10)).deletions) == 10 and is_valid_cloze(nested(10))
        assert "at most 10 deep" in cloze_problem(nested(11))


class TestValidity:
    def test_each_problem_is_named(self):
        assert "No deletion yet" in cloze_problem("Nothing to recall here")
        assert "No deletion yet" in cloze_problem("")
        assert "never closed" in cloze_problem("The capital is {{c1::Paris.")
        assert "start at 1" in cloze_problem("{{c0::Paris}} is the capital")
        assert "no answer" in cloze_problem("{{c1::}} is the capital")
        assert "no answer" in cloze_problem("{{c1::::city}} is the capital")
        assert "end a deletion early" in cloze_problem(r"{{c1::\frac{a}{b}}}")
        assert cloze_problem("{{c1::Paris}} is the capital") is None

    def test_one_unclosed_marker_spoils_the_note(self):
        assert not is_valid_cloze("{{c1::Paris}} is the capital of {{c2::France")


class TestPrompts:
    def test_one_card_per_number_with_the_other_answers_in_view(self):
        cards = cloze_prompts("{{c1::Insulin}} lowers blood {{c2::glucose}}.")
        assert [(c["number"], c["prompt"], c["answer"]) for c in cards] == [
            (1, "[...] lowers blood glucose.", "Insulin"),
            (2, "Insulin lowers blood [...].", "glucose"),
        ]
        assert cards[0]["segments"] == [("blank", "[...]"), ("text", " lowers blood "), ("text", "glucose"), ("text", ".")]

    def test_deletions_sharing_a_number_are_blanked_together(self):
        (card,) = cloze_prompts("{{c1::Na+}} flows in and {{c1::K+}} flows out.")
        assert card["prompt"] == "[...] flows in and [...] flows out." and card["answer"] == "Na+ / K+"

    def test_a_hint_stands_in_for_the_blank(self):
        text = "{{c1::ATP synthase::enzyme}} makes {{c2::ATP}}"
        first, second = cloze_prompts(text)
        assert first["prompt"] == "[enzyme] makes ATP" and first["answer"] == "ATP synthase"
        # The hint belongs to the blank: it is not shown once the answer is.
        assert second["prompt"] == "ATP synthase makes [...]"
        assert cloze_prompts(text, hint_label="[hint: {}]")[0]["prompt"] == "[hint: enzyme] makes ATP"

    def test_a_nested_deletion_is_hidden_with_the_one_around_it(self):
        outer, inner = cloze_prompts("{{c1::The {{c2::mitochondrion}} makes ATP}}.")
        assert outer["prompt"] == "[...]." and outer["answer"] == "The mitochondrion makes ATP"
        assert inner["prompt"] == "The [...] makes ATP."

    def test_the_whole_note_in_one_line(self):
        text = "{{c1::A::hint}} binds {{c2::B}}"
        assert cloze_blanked(text) == "[...] binds [...]"
        assert cloze_answers(text) == ["A", "B"]
