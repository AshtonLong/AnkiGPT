"""Card grammars.

Each strategy is a small, focused prompt module the planner can assign to a unit. They
replace the old one-size-fits-all rule wall: a definitions table and a derivation need
different card shapes, and a model told exactly which shape to produce writes far
better cards than one juggling twenty generic rules.

Every strategy shares BASE_RULES (grounding, atomicity, cloze hygiene, math format) and
adds its own section. The shared part is placed first so prompt caches hit across tasks.
"""

from dataclasses import dataclass, field

PROMPT_VERSION = "v6-agentic"

BASE_RULES = """You are an expert flashcard author for spaced repetition (Anki). You write cards ONLY from the source text you are given.

GROUNDING
- Use only facts explicitly stated in the source. No outside knowledge, no guesses.
- For every card, copy a short verbatim span (<= 200 characters) from the source into `source_quote` that supports the card. If you cannot find one, do not write the card.

CARD QUALITY
- Minimum-information principle: exactly one fact, idea, formula, or step per card. Never bundle two independent facts.
- Cause/effect, because/therefore, and multi-step relationships are split into separate cards.
- Cards are self-contained: never reference "the text", "the figure", "above", "this section", or a table by number.
- Avoid ambiguity: name the subject, scope, and conditions; no vague pronouns; no "it".
- Answers are short: one or two sentences, or one to three tight bullets. Prefer precise wording from the source.
- Skip trivia, filler, meta-statements ("in this chapter we will..."), citations, and anything not meaningfully testable.
- A long list becomes several targeted cards, not one heavy list card, unless the list is short and meant to be memorised as a unit.

BASIC CARDS
- `front` is a precise question or prompt; `back` is the answer. Set `cloze_text` and `extra` to null.

CLOZE CARDS
- `cloze_text` uses {{c1::...}} syntax with one or two deletions. Set `front` and `back` to null. `extra` may hold a brief clarification shown on the back.
- Cloze the discriminating detail, never the topic word. Bad: "{{c1::Mitochondria}} produce ATP." Good: "ATP is produced by oxidative phosphorylation in the {{c1::mitochondria}}."
- The surrounding text must not give the answer away: do not cloze the grammatical subject, the only capitalised term, or one item of a short guessable list. Avoid clozing numbers unless the number itself is the point.
- Use {{c2::...}} only when two deletions test genuinely separate facts from the same sentence.

FORMAT
- Math: \\( ... \\) inline and \\[ ... \\] display only. Never $...$ or LaTeX environments.
- Plain text with minimal HTML (<b>, <i>, <br>, <sub>, <sup>). No Markdown tables.
- `tags`: one to three short lowercase topical tags (e.g. "enzymes", "kinetics").
- Output strict JSON matching the schema. No prose outside the JSON."""


@dataclass(frozen=True)
class Strategy:
    key: str
    name: str
    description: str  # shown to the planner
    rules: str  # appended to BASE_RULES
    default_type: str = "auto"  # basic | cloze | auto
    cards_per_1k_chars: float = 3.0  # planner sizing heuristic
    aliases: tuple = field(default_factory=tuple)


STRATEGIES = {}


def _register(strategy):
    STRATEGIES[strategy.key] = strategy
    return strategy


_register(
    Strategy(
        key="general",
        name="General coverage",
        description=(
            "Balanced mix of basic and cloze cards over mixed prose. The safe default when a "
            "unit has no dominant structure."
        ),
        rules="""STRATEGY: GENERAL COVERAGE
- Cover every exam-useful concept, definition, relationship, procedure, constraint, and pitfall in the unit.
- Mix basic and cloze cards according to what each fact needs; use the requested card style when either would do.
- Prefer "why" and "how" questions over pure recall when the source explains a mechanism.""",
        cards_per_1k_chars=3.0,
    )
)

_register(
    Strategy(
        key="definition_sweep",
        name="Definition sweep",
        description=(
            "Term-heavy material: glossaries, definitions, named concepts, classifications. "
            "One card per term, plus reverse cards for the most important ones."
        ),
        rules="""STRATEGY: DEFINITION SWEEP
- Make one card per defined term or named concept: term -> precise definition (basic) or a cloze on the discriminating part of the definition.
- For the most central terms (roughly the top third), also write the reverse direction: definition/description -> term.
- Capture classification membership ("X is a type of Y characterised by Z") as its own card.
- Do not define a term using the term itself.""",
        default_type="basic",
        cards_per_1k_chars=4.0,
    )
)

_register(
    Strategy(
        key="formula_derivation",
        name="Formulas & derivations",
        description=(
            "Equations, laws, and derivations. Cards for what a formula computes, each "
            "variable's meaning and units, conditions of validity, and limiting cases."
        ),
        rules="""STRATEGY: FORMULAS & DERIVATIONS
- For each formula: one card for what it computes/states, one card per variable or symbol meaning (with units where given), one card for the conditions or assumptions under which it holds.
- Cloze the discriminating symbol or exponent inside the formula, never the whole formula.
- For derivations: one card per non-obvious step ("what justifies going from A to B?"), plus a card for the final result.
- Capture limiting cases and sign/direction conventions as separate cards.
- Write all math with \\( ... \\) and \\[ ... \\].""",
        cards_per_1k_chars=4.5,
    )
)

_register(
    Strategy(
        key="mechanism_chain",
        name="Mechanism / process chain",
        description=(
            "Ordered processes, pathways, sequences, algorithms, and cause-effect chains. "
            "One card per link so the chain is learned as connected steps."
        ),
        rules="""STRATEGY: MECHANISM / PROCESS CHAIN
- Decompose the process into ordered steps A -> B -> C. Write one card per link: "What happens immediately after A?", "What triggers B?", "What is the role of X in step C?".
- Add cards for the inputs, outputs, and location of the process, and for what regulates or halts it.
- Cloze the step that follows, or the agent that performs a step, never the process name.
- Never ask for the whole sequence in one card unless it is at most four short steps meant to be recited as a unit.""",
        cards_per_1k_chars=3.5,
    )
)

_register(
    Strategy(
        key="compare_contrast",
        name="Compare & contrast",
        description=(
            "Two or more similar things that students confuse (types, classes, competing "
            "models). Discriminator cards that pin down exactly what separates them."
        ),
        rules="""STRATEGY: COMPARE & CONTRAST
- Identify the items being compared and the dimensions along which they differ.
- Write discriminator cards: "Which of A or B has property P?", "How does A differ from B with respect to D?", "A property that A has and B lacks".
- Write one card per (item, distinguishing dimension) pair rather than a single comparison table card.
- Include the shared property when the source stresses it ("Both A and B ...").""",
        default_type="basic",
        cards_per_1k_chars=3.5,
    )
)

_register(
    Strategy(
        key="worked_example",
        name="Worked examples & problem solving",
        description=(
            "Worked problems, case studies, and applied procedures. Cards on method "
            "selection and next-step reasoning rather than the specific numbers."
        ),
        rules="""STRATEGY: WORKED EXAMPLES & PROBLEM SOLVING
- Extract the transferable method, not the specific numbers: "Given a problem of type T, which approach applies and why?", "After step S, what is the next step?", "What condition must be checked before applying M?".
- One card for the setup -> method mapping, one card per non-obvious step, one card for how to sanity-check the result.
- Include the specific example only when it is the canonical case the student must recognise.""",
        default_type="basic",
        cards_per_1k_chars=2.0,
    )
)

_register(
    Strategy(
        key="key_claims",
        name="Key claims & arguments",
        description=(
            "Narrative or argumentative prose (history, law, humanities, discussion "
            "sections). Who/what/when/why claims and the evidence or reasoning behind them."
        ),
        rules="""STRATEGY: KEY CLAIMS & ARGUMENTS
- Extract testable claims: who did/decided/argued what, when, and why; causes and consequences; the evidence or reasoning the source gives.
- One card per claim; one card per cause->effect link; one card per piece of evidence for a claim.
- Prefer "why" and "what was the consequence of" questions over date recall; include dates only when the source treats them as important.
- Ignore rhetorical flourish, anecdotes, and transitional prose.""",
        default_type="basic",
        cards_per_1k_chars=2.5,
    )
)

_register(
    Strategy(
        key="pitfall_edge_case",
        name="Pitfalls & edge cases",
        description=(
            "Exceptions, caveats, common mistakes, and boundary conditions. Small, "
            "high-value cards that catch what students usually get wrong."
        ),
        rules="""STRATEGY: PITFALLS & EDGE CASES
- Write cards for every exception, caveat, boundary condition, contraindication, and "common mistake" the source names.
- Frame them so the wrong intuition is confronted: "Under what condition does rule R NOT apply?", "What is the exception to X?", "Why is Y a mistake when doing Z?".
- Keep each card to a single exception.""",
        default_type="basic",
        cards_per_1k_chars=2.0,
    )
)

_register(
    Strategy(
        key="figure_recall",
        name="Figure recall",
        description=(
            "Diagrams, charts, and labelled images. Cards that ask what a labelled part "
            "is or does, or what a plot shows, with the figure embedded on the card."
        ),
        rules="""STRATEGY: FIGURE RECALL
- You are given a description of a figure (and its labelled parts) plus the surrounding source text.
- Write cards that require recognising or recalling something FROM the figure: what a labelled structure is/does, what the axes or trend of a plot show, what a highlighted region represents.
- Phrase questions so they make sense with the image displayed above them: "In this diagram, what does the structure labelled X do?".
- Do not restate facts already obvious from the caption alone; ground in the labelled parts and the source text.""",
        default_type="basic",
        cards_per_1k_chars=2.0,
    )
)


DEFAULT_STRATEGY = "general"


def get_strategy(key):
    return STRATEGIES.get(key) or STRATEGIES[DEFAULT_STRATEGY]


def strategy_catalog():
    """Compact catalogue for the planner prompt."""
    return "\n".join(f"- {s.key}: {s.description}" for s in STRATEGIES.values())


def system_prompt(strategy_key, card_style, focus="", exclude="", glossary=""):
    """Static system prompt for a worker call. Deck-level settings go last so the base
    rules + strategy section stay byte-identical across decks (prompt-cache friendly)."""
    strategy = get_strategy(strategy_key)
    style_line = {
        "basic": "Preferred card style: basic (use cloze only where a sentence with a deletion is clearly the better test).",
        "cloze": "Preferred card style: cloze (use basic only where a question is clearly the better test).",
    }.get(card_style, "Preferred card style: choose the better type per fact.")
    return "\n\n".join(
        [
            BASE_RULES,
            strategy.rules,
            "\n".join(
                [
                    style_line,
                    f"Focus: {focus or 'all exam-useful material'}",
                    f"Exclude: {exclude or 'none'}",
                    f"Must-include terms: {glossary or 'none'}",
                ]
            ),
        ]
    )
