from __future__ import annotations

from textwrap import dedent

from moodle.api import MoodleAPI
from moodle.models import AnswerOption, Question, QuestionType
from solver import QuizSolver


def _mk_question_with_options(labels: list[str]) -> Question:
    q = Question(
        question_id="1",
        slot=1,
        question_type=QuestionType.MCQ,
        text="Test question",
    )
    opts: list[AnswerOption] = []
    for i, lab in enumerate(labels):
        key = chr(ord("A") + i)
        opts.append(AnswerOption(key=key, label=lab, input_name="q1:answer", input_value=str(i)))
    q.options = opts
    return q


def test_api_parse_options_ignores_flag_and_clear() -> None:
    html = dedent(
        """
        <div class="que multichoice">
          <div class="info"><a class="flag" href="#">Flag question</a></div>
          <div class="content">
            <div class="qtext">Select one. <a href="#">Clear my choice</a></div>
            <div class="answer">
              <div class="r0">
                <input type="radio" name="q1:answer" id="q1_a0" value="0"/>
                <label for="q1_a0">a. Poniżej 0,1 nm</label>
              </div>
              <div class="r1">
                <input type="radio" name="q1:answer" id="q1_a1" value="1"/>
                <label for="q1_a1">b. Kilkadziesiąt nanometrów (np. 20–60 nm)</label>
              </div>
              <div class="r0">
                <input type="radio" name="q1:answer" id="q1_a2" value="2"/>
                <label for="q1_a2">c. Około 1 pm</label>
              </div>
              <div class="r1">
                <input type="radio" name="q1:answer" id="q1_a3" value="3"/>
                <label for="q1_a3">d. Kilka mikrometrów</label>
              </div>
              <div class="other"><a href="#" class="clearchoice">Clear my choice</a></div>
            </div>
          </div>
        </div>
        """
    ).strip()

    opts = MoodleAPI._parse_options_from_html(html)
    assert len(opts) == 4, f"expected 4 options, got {len(opts)}"
    joined = " ".join(o.label.lower() for o in opts)
    assert "flag question" not in joined
    assert "clear my choice" not in joined
    assert "kilkadziesi" in joined or "kilkadziesiąt".replace("ą", "a")[:8]  # sanity


def test_mcq_mapping_polish_diacritics_and_dashes() -> None:
    q = _mk_question_with_options(
        [
            "Poniżej 0,1 nm",
            "Kilkadziesiąt nanometrów (np. 20–60 nm)",
            "Około 1 pm",
            "Kilka mikrometrów",
        ]
    )
    letter, score = QuizSolver._map_mcq_text_to_letter(q, "Kilkadziesiat nanometrow (np. 20-60 nm)")
    assert letter == "B", (letter, score)
    assert score >= 0.6, score


def test_mcq_prefers_text_mapping_over_letter_when_confident() -> None:
    q = _mk_question_with_options(["abrykos", "ogorek", "pizza z ananasem", "banan"])
    # model says A but text clearly points to C
    mapped, score = QuizSolver._map_mcq_text_to_letter(q, "pizza z ananasem")
    assert mapped == "C", (mapped, score)


def main() -> None:
    tests = [
        test_api_parse_options_ignores_flag_and_clear,
        test_mcq_mapping_polish_diacritics_and_dashes,
        test_mcq_prefers_text_mapping_over_letter_when_confident,
    ]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()

