from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import re


class QuestionType(str, Enum):
    MCQ = "multichoice"           # Single correct answer, radio buttons
    MULTI_SELECT = "multiselect"  # Multiple correct answers, checkboxes
    TRUE_FALSE = "truefalse"
    SHORT_ANSWER = "shortanswer"
    ESSAY = "essay"
    MATCHING = "match"
    GAP_SELECT = "gapselect"      # Select missing words (dropdown blanks)
    DRAG_DROP = "ddwtos"          # Drag and drop onto text
    CLOZE = "multianswer"         # Cloze / embedded answers (multiple text inputs)
    UNKNOWN = "unknown"


@dataclass
class AnswerOption:
    """A single selectable answer option for MCQ / multi-select questions."""
    key: str          # e.g. "a", "b", "c", "1", "2" — internal Moodle value
    label: str        # Display label shown to the student, e.g. "Paris"
    input_name: str   # HTML input name attribute (needed for scraper form fill)
    input_value: str  # HTML input value attribute


@dataclass
class GapItem:
    """One blank / gap in a gap-select or cloze question."""
    input_name: str       # HTML input/select name
    choices: list[str] = field(default_factory=list)   # For selects (gapselect / ddwtos)
    choice_values: list[str] = field(default_factory=list)
    is_text: bool = False  # True for text inputs (cloze/multianswer)
    # For cloze sub-questions that show "a word b word c word d word" labeled options
    labeled_options: dict[str, str] = field(default_factory=dict)  # {"a": "action", "b": "targets"}


@dataclass
class MatchPair:
    """One row in a matching question (left side → dropdown of options)."""
    stem: str             # Left-side text, e.g. "Capital of France"
    select_name: str      # HTML select name attribute
    choices: list[str]    # Display text of available options (excluding blank)
    choice_values: list[str] = field(default_factory=list)  # Numeric values from HTML
    correct_answer: Optional[str] = None  # Filled after solving


@dataclass
class Question:
    """Represents a single quiz question as fetched from Moodle."""
    question_id: str
    slot: int                       # Position in the quiz (1-based)
    question_type: QuestionType
    text: str                       # Full question text (HTML stripped)
    options: list[AnswerOption] = field(default_factory=list)
    match_pairs: list[MatchPair] = field(default_factory=list)
    gap_items: list[GapItem] = field(default_factory=list)  # for gap-select / cloze

    # For short-answer and essay questions — plain text input name
    text_input_name: Optional[str] = None

    # Moodle form validation token — must be included in every submission
    sequencecheck_name: Optional[str] = None
    sequencecheck_value: Optional[str] = None

    # Filled after solving
    chosen_answer: Optional[str | list[str]] = None
    chosen_match_pairs: Optional[dict[str, str]] = None

    # Raw HTML of the question block (used by scraper to re-parse if needed)
    raw_html: Optional[str] = None

    # Annotated version of text where blanks are marked [BLANK N] — used in AI prompts
    annotated_text: Optional[str] = None

    @property
    def prompt_text(self) -> str:
        """Return the best available text for AI prompts (annotated > plain)."""
        return (self.annotated_text or self.text).strip()

    def formatted_options(self) -> str:
        """Return options as a readable string for inclusion in AI prompts."""
        def _looks_broken(lines: list[str]) -> bool:
            if not lines:
                return True
            joined = " ".join(lines).lower()
            # Common Moodle UI placeholders that should not become "options"
            bad_tokens = ("choose...", "choose…", "clear my choice", "flag question")
            if any(t in joined for t in bad_tokens):
                # If most of the content is UI tokens, it's broken.
                useful = [ln for ln in lines if not any(t in ln.lower() for t in bad_tokens)]
                if len(useful) < max(1, len(lines) // 2):
                    return True
            # If all labels are identical/very short, it's broken.
            norm = [re.sub(r"\s+", " ", ln.strip().lower()) for ln in lines]
            if len(set(norm)) <= 1:
                return True
            return False

        # Primary: parsed options from HTML
        if self.options:
            lines = [f"  {opt.key.upper()}. {opt.label}".strip() for opt in self.options]
            if not _looks_broken(lines):
                return "\n".join(lines)

        # Fallback: extract inline "a ... b ... c ..." blocks from prompt text
        extracted = self._extract_inline_lettered_options(self.prompt_text)
        if extracted:
            return "\n".join([f"  {k}. {v}" for k, v in extracted])

        return ""

    @staticmethod
    def _extract_inline_lettered_options(text: str) -> list[tuple[str, str]]:
        """
        Extract options from flattened text when Moodle renders them inline after
        "Select one", e.g. "a  action   b  targets   c  strategy ...".
        Returns list of (LETTER, LABEL).
        """
        if not text:
            return []
        t = " ".join(text.replace("\xa0", " ").split())
        # Matches: "a." / "a)" / "a  " + some text, repeated
        # Use a lookahead to stop at the next letter label.
        pat = re.compile(r"\b([A-Ha-h])[.)]\s+(.+?)(?=\s+[A-Ha-h][.)]\s+|\s*$)")
        out: list[tuple[str, str]] = []
        for m in pat.finditer(t):
            key = m.group(1).upper()
            label = m.group(2).strip()
            # Filter out obvious UI fragments
            if label.lower() in ("choose...", "choose…"):
                continue
            if "clear my choice" in label.lower() or "flag question" in label.lower():
                continue
            if label:
                out.append((key, label))
        return out

    def formatted_match_pairs(self) -> str:
        """Return match pairs as a readable string for AI prompts."""
        if not self.match_pairs:
            return ""
        lines = []
        for i, pair in enumerate(self.match_pairs, 1):
            choices_str = ", ".join(pair.choices)
            lines.append(f"  {i}. {pair.stem}  →  [{choices_str}]")
        return "\n".join(lines)


@dataclass
class Quiz:
    """Represents a Moodle quiz."""
    quiz_id: str
    course_id: str
    name: str
    description: str
    # Course module id (the value used in view.php?id=CMID). May be None in scraper mode.
    cmid: Optional[str] = None
    attempt_id: Optional[str] = None   # Set when an attempt is started
    questions: list[Question] = field(default_factory=list)
    total_questions: int = 0

    # Metadata from REST API (may be None in scraper mode)
    time_limit_seconds: Optional[int] = None
    max_attempts: Optional[int] = None


@dataclass
class CourseInfo:
    """Minimal course information for listing available quizzes."""
    course_id: str
    full_name: str
    short_name: str
    quizzes: list[Quiz] = field(default_factory=list)
