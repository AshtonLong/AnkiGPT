from app.services.validators import (
    is_in_scope,
    is_math_valid,
    is_valid_cloze,
    normalize_cloze,
    normalize_math,
    normalize_text,
)


class TestCloze:
    def test_valid_single_cloze(self):
        assert is_valid_cloze("The capital is {{c1::Paris}}.")

    def test_valid_multiple_clozes(self):
        assert is_valid_cloze("{{c1::A}} and {{c2::B}}")

    def test_missing_cloze(self):
        assert not is_valid_cloze("No deletion here")

    def test_empty(self):
        assert not is_valid_cloze("")

    def test_latex_braces_outside_a_deletion_are_fine(self):
        assert is_valid_cloze(r"The ratio \frac{a}{b} is {{c1::constant}}")

    def test_a_deletion_ending_in_a_latex_brace_is_cut_short_by_anki(self):
        # Anki ends a deletion at the first "}}": here that is inside \frac{a}{b}, so the
        # answer would lose its last brace. A model-written cloze is repaired with a space.
        assert not is_valid_cloze(r"Area is {{c1::\frac{a}{b}}} units")
        assert normalize_cloze(r"Area is  {{c1::\frac{a}{b}}} units") == r"Area is {{c1::\frac{a}{b} }} units"
        assert is_valid_cloze(r"Area is {{c1::\frac{a}{b} }} units")

    def test_a_model_written_cloze_is_cleaned_like_any_other_field(self):
        assert normalize_cloze("  $E$ = {{c1::$mc^{2}$}}  ") == r"\(E\) = {{c1::\(mc^{2}\)}}"
        assert normalize_cloze(None) == ""


class TestMath:
    def test_currency_is_not_treated_as_math(self):
        # A lone "$" (currency) must remain valid and not be stripped.
        text = normalize_math("It costs $5 total")
        assert "$5" in text
        assert is_math_valid(text)

    def test_block_math_converted(self):
        assert normalize_math("$$x^2$$") == "\\[x^2\\]"

    def test_inline_math_converted(self):
        assert normalize_math("$x$") == "\\(x\\)"

    def test_unbalanced_inline_invalid(self):
        assert not is_math_valid("\\(x")

    def test_leftover_paired_dollars_invalid(self):
        assert not is_math_valid("$x$ remained")


class TestScope:
    def test_in_scope(self):
        chunk = "Photosynthesis converts carbon dioxide and water into glucose using sunlight."
        card = "Photosynthesis converts carbon dioxide into glucose."
        assert is_in_scope(card, chunk)

    def test_off_topic_dropped(self):
        chunk = "Photosynthesis converts carbon dioxide and water into glucose."
        card = "Napoleon Bonaparte commanded French artillery regiments during numerous campaigns."
        assert not is_in_scope(card, chunk)

    def test_paraphrase_with_stemming_stays_in_scope(self):
        chunk = "The enzyme inhibits replication of the viral genome."
        card = "Replication of the viral genome is inhibited by the enzyme."
        assert is_in_scope(card, chunk)

    def test_empty_card_is_in_scope(self):
        assert is_in_scope("", "anything")


def test_normalize_text_collapses_whitespace():
    assert normalize_text("  a   b\n c ") == "a b c"
