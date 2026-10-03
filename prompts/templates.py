"""
Prompt templates for each question type.

Every template includes a strict instruction to return ONLY a JSON object
in the standardised format so the agent parser can reliably extract it.
"""

from __future__ import annotations

from moodle.models import Question, QuestionType


# ---------------------------------------------------------------------------
# Shared system instruction (prepended to all prompts)
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTION = """\
You are an expert exam assistant. Your task is to answer the question below \
correctly and concisely.

CRITICAL: You MUST respond with ONLY a valid JSON object — no prose, no \
markdown fences, no explanation outside the JSON.

Required JSON format:
{
  "answer": <answer value — see instructions per question type>,
  "confidence": <float between 0.0 and 1.0>,
  "reasoning": "<one sentence explaining why>"
}
"""


# ---------------------------------------------------------------------------
# Template builders
# ---------------------------------------------------------------------------

def build_prompt(question: Question) -> str:
    """Select and render the correct template for the question type."""
    q_type = question.question_type

    if q_type in (QuestionType.MCQ,):
        return _mcq_prompt(question)
    elif q_type == QuestionType.MULTI_SELECT:
        return _multi_select_prompt(question)
    elif q_type == QuestionType.TRUE_FALSE:
        return _true_false_prompt(question)
    elif q_type == QuestionType.SHORT_ANSWER:
        return _short_answer_prompt(question)
    elif q_type == QuestionType.ESSAY:
        return _essay_prompt(question)
    elif q_type == QuestionType.MATCHING:
        return _matching_prompt(question)
    elif q_type == QuestionType.GAP_SELECT:
        return _gap_select_prompt(question)
    elif q_type == QuestionType.DRAG_DROP:
        return _drag_drop_prompt(question)
    elif q_type == QuestionType.CLOZE:
        return _cloze_prompt(question)
    else:
        return _unknown_prompt(question)


# ---------------------------------------------------------------------------
# Individual templates
# ---------------------------------------------------------------------------

def _mcq_prompt(question: Question) -> str:
    options_text = question.formatted_options()
    return f"""{SYSTEM_INSTRUCTION}

Question type: Multiple choice (exactly ONE correct answer)

QUESTION:
{question.prompt_text}

OPTIONS:
{options_text}

Instructions:
- "answer" must be a JSON object with BOTH:
  - "letter": the UPPERCASE letter of the correct option (e.g. "A", "B", "C")
  - "text": the FULL text of that option (copy it exactly from OPTIONS if possible)
- Example: {{"answer": {{"letter":"C","text":"..."}}, "confidence": 0.86, "reasoning": "..." }}

Example response:
{{"answer": {{"letter": "B", "text": "..." }}, "confidence": 0.95, "reasoning": "B is correct because ..."}}
"""


def _multi_select_prompt(question: Question) -> str:
    options_text = question.formatted_options()
    return f"""{SYSTEM_INSTRUCTION}

Question type: Multiple select (one or MORE correct answers)

QUESTION:
{question.prompt_text}

OPTIONS:
{options_text}

Instructions:
- "answer" must be a JSON array of UPPERCASE letters for ALL correct options.
- Example: ["A", "C"] means options A and C are both correct.

Example response:
{{"answer": ["A", "C"], "confidence": 0.88, "reasoning": "A and C are correct because ..."}}
"""


def _true_false_prompt(question: Question) -> str:
    return f"""{SYSTEM_INSTRUCTION}

Question type: True / False

QUESTION:
{question.prompt_text}

Instructions:
- "answer" must be exactly "True" or "False" (capitalised, no other text).

Example response:
{{"answer": "True", "confidence": 0.97, "reasoning": "The statement is correct because ..."}}
"""


def _short_answer_prompt(question: Question) -> str:
    return f"""{SYSTEM_INSTRUCTION}

Question type: Short answer (fill in the blank or brief answer)

QUESTION:
{question.prompt_text}

Instructions:
- "answer" must be a SHORT text string — typically 1-5 words.
- Do NOT include explanations inside the answer field.

Example response:
{{"answer": "photosynthesis", "confidence": 0.91, "reasoning": "The process described is photosynthesis."}}
"""


def _essay_prompt(question: Question) -> str:
    return f"""{SYSTEM_INSTRUCTION}

Question type: Essay / extended answer

QUESTION:
{question.prompt_text}

Instructions:
- "answer" must be a well-structured essay response (2-5 paragraphs).
- Write the full essay text inside the "answer" field as a single string.
- Use \\n for paragraph breaks inside the JSON string.
- "confidence" reflects how certain you are the essay covers the topic well.

Example response:
{{"answer": "Paragraph one.\\n\\nParagraph two.", "confidence": 0.80, "reasoning": "Covered the main aspects of the topic."}}
"""


def _matching_prompt(question: Question) -> str:
    pairs_text = question.formatted_match_pairs()
    return f"""{SYSTEM_INSTRUCTION}

Question type: Matching

QUESTION:
{question.prompt_text}

PAIRS TO MATCH:
{pairs_text}

Instructions:
- "answer" must be a JSON object where each key is the LEFT-side stem text
  and each value is the matching RIGHT-side choice text.
- Use the EXACT text from the pairs listed above.
- IMPORTANT: Copy choices EXACTLY as written, including any typos (do NOT correct spelling).

Example response:
{{"answer": {{"Capital of France": "Paris", "Capital of Germany": "Berlin"}}, \
"confidence": 0.93, "reasoning": "Standard geography facts."}}
"""


def _gap_select_prompt(question: Question) -> str:
    """Prompt for select-missing-words (gapselect) questions."""
    if question.gap_items:
        choices_info = "\n".join(
            f"  Blank {i+1}: [{', '.join(g.choices)}]"
            for i, g in enumerate(question.gap_items)
            if g.choices
        )
    else:
        choices_info = "  (choices not available — infer from context)"
    return f"""{SYSTEM_INSTRUCTION}

Question type: Select missing words (choose from dropdown options for each blank)

QUESTION (blanks marked as [BLANK N]):
{question.prompt_text}

AVAILABLE CHOICES PER BLANK:
{choices_info}

Instructions:
- "answer" must be a JSON object where keys are blank numbers as strings ("1", "2", ...)
  and values are the EXACT choice text for that blank.
- Use the exact text shown in the available choices above.
- IMPORTANT: Copy choices EXACTLY as written, including any typos (do NOT correct spelling).

Example response:
{{"answer": {{"1": "however", "2": "therefore", "3": "although"}}, "confidence": 0.90, "reasoning": "These connectors fit the context."}}
"""


def _drag_drop_prompt(question: Question) -> str:
    """Prompt for drag-and-drop onto text (ddwtos) questions."""
    n_blanks = len(question.gap_items) if question.gap_items else "?"
    # All gap items share the same word list — take from first item
    if question.gap_items and question.gap_items[0].choices:
        words = question.gap_items[0].choices
        word_list = "\n".join(f"  {i+1}. {w}" for i, w in enumerate(words))
        word_note = "There is one extra word that is NOT used."
    else:
        word_list = "  (word list not available — infer from question context)"
        word_note = ""
    return f"""{SYSTEM_INSTRUCTION}

Question type: Drag and drop words into text (place the correct word in each blank)

QUESTION (blanks marked as [BLANK N]):
{question.prompt_text}

AVAILABLE WORDS (one per blank + one extra):
{word_list}
{word_note}

There are {n_blanks} blank(s) to fill.

Instructions:
- "answer" must be a JSON object where keys are blank position numbers as strings ("1", "2", ...)
  and values are the EXACT word/phrase text from the list above.
- Use the exact spelling shown in the word list.
- IMPORTANT: Copy words EXACTLY as written, including any typos (do NOT correct spelling).
- Do NOT use a word more than once (one word is left over unused).

Example response:
{{"answer": {{"1": "survey", "2": "respondents", "3": "follow-up"}}, "confidence": 0.85, "reasoning": "These words fit the meeting notes context."}}
"""


def _cloze_prompt(question: Question) -> str:
    """Prompt for cloze (multianswer / embedded answers) questions."""
    n_blanks = len(question.gap_items) if question.gap_items else "?"

    # The annotated text shows [BLANK N] markers where each blank should go.
    q_text = question.prompt_text

    # Check if any blank has labeled options (a/b/c/d format) or select choices
    has_labeled = any(g.labeled_options for g in question.gap_items)
    has_selects = any(not g.is_text and g.choices for g in question.gap_items)

    if has_labeled:
        opts_lines = []
        for i, g in enumerate(question.gap_items, 1):
            if g.labeled_options:
                opts_str = "  ".join(f"{k} {v}" for k, v in sorted(g.labeled_options.items()))
                opts_lines.append(f"  [BLANK {i}]: {opts_str}")
            elif g.choices:
                opts_str = "  ".join(f"{chr(96+j+1)} {c}" for j, c in enumerate(g.choices))
                opts_lines.append(f"  [BLANK {i}]: {opts_str}")
            else:
                opts_lines.append(f"  [BLANK {i}]: (free text)")
        options_section = "\n".join(opts_lines)
        return f"""{SYSTEM_INSTRUCTION}

Question type: Choose the correct option — fill each [BLANK N] with a single LETTER

QUESTION (each blank position is shown as [BLANK N]):
{q_text}

OPTIONS FOR EACH BLANK:
{options_section}

Instructions:
- "answer" must be a JSON object where keys are blank numbers ("1", "2", ...) and values
  are the SINGLE LETTER of the correct option (e.g. "a", "b", "c", "d").
- Return ONLY the letter — do NOT write the full word.
- There are {n_blanks} blanks total — provide an answer for every one.

Example response:
{{"answer": {{"1": "d", "2": "c", "3": "a", "4": "c", "5": "a"}}, "confidence": 0.92, "reasoning": "Each option fits the sentence context."}}
"""
    elif has_selects:
        opts_lines = []
        for i, g in enumerate(question.gap_items, 1):
            if g.choices:
                opts_lines.append(f"  [BLANK {i}]: [{', '.join(g.choices)}]")
            else:
                opts_lines.append(f"  [BLANK {i}]: (free text)")
        options_section = "\n".join(opts_lines)
        return f"""{SYSTEM_INSTRUCTION}

Question type: Cloze / embedded answers — choose from dropdown options

QUESTION (each blank position is shown as [BLANK N]):
{q_text}

OPTIONS FOR EACH BLANK:
{options_section}

Instructions:
- "answer" must be a JSON object where keys are blank numbers ("1", "2", ...) and values
  are the EXACT option text for that blank.
- There are {n_blanks} blanks total.
- IMPORTANT: Copy option text EXACTLY as written, including any typos (do NOT correct spelling).

Example response:
{{"answer": {{"1": "customer", "2": "feedback", "3": "analysis"}}, "confidence": 0.88, "reasoning": "These terms fit."}}
"""
    else:
        return f"""{SYSTEM_INSTRUCTION}

Question type: Cloze / fill-in-the-blanks

QUESTION (each blank position is shown as [BLANK N]):
{q_text}

Instructions:
- "answer" must be a JSON object where keys are blank numbers ("1", "2", ...) and values
  are the SHORT TEXT answer for that blank (typically 1-3 words).
- There are {n_blanks} blanks total — provide an answer for EVERY blank.
- For matching tasks (write a-g in gaps): each value should be a single letter a-g.
- For question tags: answer with just the auxiliary + pronoun, e.g. "isn't it", "have they".
- [BLANK N] in the text shows exactly where each answer goes — use this as context.

Example response:
{{"answer": {{"1": "is", "2": "it", "3": "have", "4": "didn't"}}, "confidence": 0.90, "reasoning": "Question tags reverse the main verb."}}
"""


def _unknown_prompt(question: Question) -> str:
    """Fallback for unrecognised question types."""
    options_text = question.formatted_options()
    options_section = f"\nOPTIONS:\n{options_text}" if options_text else ""
    return f"""{SYSTEM_INSTRUCTION}

QUESTION (blanks shown as [BLANK N] if present):
{question.prompt_text}{options_section}

Instructions:
- Answer this question as best you can.
- If it is multiple choice, put the letter in "answer".
- If it is free text, put your answer text in "answer".

Example response:
{{"answer": "B", "confidence": 0.75, "reasoning": "Best available option."}}
"""
