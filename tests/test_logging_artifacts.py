from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

from agents.voting import VoteResult
from moodle.models import AnswerOption, Question, QuestionType
from solver import QuizSolver
from agents.base import AgentAnswer


def main() -> None:
    # Minimal config: only logging matters here.
    cfg = {
        "moodle": {"url": "https://example.invalid", "username": "x", "password": "x", "preferred_mode": "api"},
        "agents": {"enabled": {}},
        "solver": {"logs_dir": "logs", "save_log": False, "show_reasoning": False},
    }

    # Create QuizSolver without requiring configured agents/orchestrator.
    # We only want to test on-disk logging methods.
    s = QuizSolver.__new__(QuizSolver)
    s.config = cfg
    s.moodle_cfg = cfg.get("moodle", {})
    s.agent_cfg = cfg.get("agents", {})
    s.solver_cfg = cfg.get("solver", {})
    s._log = []
    s._vision_render_cache = {}
    s._debug_root = None
    s._attempt_id = None
    # Simulate attempt context (normally set after start_quiz)
    s._attempt_id = "TEST_ATTEMPT_123"
    s._debug_root = s._ensure_debug_root()

    html = dedent(
        """
        <div class="que multichoice">
          <div class="info"><a class="flag" href="#">Flag question</a></div>
          <div class="content">
            <div class="qtext">
              <p>Typowa szerokość widmowa diody LED wynosi:</p>
              <p><em>Select one.</em></p>
              <a href="#" class="clearchoice">Clear my choice</a>
              <img src="https://cdn.example/img.png" />
              <audio controls src="https://cdn.example/a.mp3"></audio>
              <video controls poster="https://cdn.example/p.jpg"><source src="https://cdn.example/v.mp4"></video>
            </div>
            <div class="answer">
              <div class="r0"><input type="radio" name="q1:answer" id="q1_a0" value="0"/><label for="q1_a0">a. Poniżej 0,1 nm</label></div>
              <div class="r1"><input type="radio" name="q1:answer" id="q1_a1" value="1"/><label for="q1_a1">b. Kilkadziesiąt nanometrów (np. 20–60 nm)</label></div>
              <div class="r0"><input type="radio" name="q1:answer" id="q1_a2" value="2"/><label for="q1_a2">c. Około 1 pm</label></div>
              <div class="r1"><input type="radio" name="q1:answer" id="q1_a3" value="3"/><label for="q1_a3">d. Kilka mikrometrów</label></div>
            </div>
          </div>
        </div>
        """
    ).strip()

    q = Question(
        question_id="1",
        slot=1,
        question_type=QuestionType.MCQ,
        text="Typowa szerokość widmowa diody LED wynosi:",
        raw_html=html,
        annotated_text="Typowa szerokość widmowa diody LED wynosi: Select one. a) ... b) ...",
    )
    q.options = [
        AnswerOption(key="A", label="Poniżej 0,1 nm", input_name="q1:answer", input_value="0"),
        AnswerOption(key="B", label="Kilkadziesiąt nanometrów (np. 20–60 nm)", input_name="q1:answer", input_value="1"),
        AnswerOption(key="C", label="Około 1 pm", input_name="q1:answer", input_value="2"),
        AnswerOption(key="D", label="Kilka mikrometrów", input_name="q1:answer", input_value="3"),
    ]

    prompt = "PROMPT TEST"
    answers = [
        AgentAnswer(agent_name="A1", model_name="m1", answer={"letter": "B", "text": "Kilkadziesiąt nanometrów (np. 20-60 nm)"}, confidence=0.9, reasoning="x", temperature=0.2, raw_response="RAW1", error=""),
        AgentAnswer(agent_name="A2", model_name="m2", answer={"letter": "B", "text": "Kilkadziesiąt nanometrów (np. 20–60 nm)"}, confidence=0.8, reasoning="y", temperature=0.2, raw_response="RAW2", error=""),
    ]
    result = VoteResult(
        winner={"letter": "B", "text": "Kilkadziesiąt nanometrów (np. 20–60 nm)"},
        vote_count=2,
        total_votes=2,
        confidence_sum=1.7,
        reasoning_samples=[],
        tiebreak_used=False,
        error_count=0,
        all_answers=answers,
    )

    s._dump_question_artifacts(
        question=q,
        prompt=prompt,
        round_answers=answers,
        round_vision=[],
        result=result,
        image_b64=None,
    )

    # Simulate attempt-level summary (normally built from solving loop log entries)
    s._log = [
        {
            "slot": q.slot,
            "question_id": q.question_id,
            "question_type": q.question_type.value,
            "question_text": q.text,
            "winner": result.winner,
            "submitted": "B",
            "vote_count": result.vote_count,
            "total_votes": result.total_votes,
            "win_ratio": result.win_ratio,
            "confidence_sum": result.confidence_sum,
            "tiebreak_used": result.tiebreak_used,
            "error_count": result.error_count,
            "elapsed_seconds": 0.1,
        }
    ]
    s._dump_attempt_summary()

    root = Path("logs") / "attempt_TEST_ATTEMPT_123" / "q001_multichoice"
    must_exist = [
        root / "raw.html",
        root / "question_text.txt",
        root / "annotated_text.txt",
        root / "prompt.txt",
        root / "options.json",
        root / "agent_answers.json",
        root / "vote_result.json",
        root / "media_manifest.json",
        Path("logs") / "attempt_TEST_ATTEMPT_123" / "attempt_summary.json",
        Path("logs") / "attempt_TEST_ATTEMPT_123" / "attempt_summary.txt",
    ]

    missing = [str(p) for p in must_exist if not p.exists()]
    if missing:
        raise SystemExit("Missing files:\n" + "\n".join(missing))

    # Print a short preview
    manifest = json.loads((root / "media_manifest.json").read_text(encoding="utf-8"))
    print("OK: logs created")
    print("media_manifest keys:", sorted(manifest.keys()))
    print("example src/href count:", {k: len(v) for k, v in manifest.items()})
    print("attempt folder:", str((Path('logs') / 'attempt_TEST_ATTEMPT_123').resolve()))


if __name__ == "__main__":
    main()

