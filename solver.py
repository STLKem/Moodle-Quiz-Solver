"""
QuizSolver — the main orchestration loop.

For each question:
  1. Build a prompt from the question type
  2. Ask all agents (in parallel, each called twice)
  3. Vote on the answers
  4. Submit the winning answer to Moodle
  5. Log and display results

Usage (called from main.py, not directly):
    solver = QuizSolver(config)
    await solver.run(quiz_url_or_id)
"""

from __future__ import annotations

import asyncio
import json
import time
import difflib
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional
import base64
from bs4 import BeautifulSoup

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich import box

from agents.base import AgentAnswer
from agents.gemini_agent import GeminiFlash25Agent
from agents.groq_agent import GroqLlama4ScoutAgent
from agents.orchestrator import AgentOrchestrator, build_agents
from agents.voting import VoteResult, vote, vote_essay
from moodle.api import MoodleAPI, MoodleAttemptFinishedError
from moodle.client import ConnectionMode, MoodleClient
from moodle.models import Question, QuestionType
from prompts.templates import build_prompt

console = Console()


class QuizSolver:
    @staticmethod
    def _norm_loose(text: str) -> str:
        """
        Strong normalisation for matching:
        - casefold
        - strip Polish/diacritic marks
        - normalise dashes (– — −) to '-'
        - drop most punctuation
        - collapse whitespace
        """
        if not text:
            return ""
        t = str(text)
        # normalise various dashes
        t = t.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
        # unicode normalise then strip combining marks (diacritics)
        t = unicodedata.normalize("NFKD", t)
        t = "".join(ch for ch in t if not unicodedata.combining(ch))
        # case-insensitive (better than lower for some languages)
        t = t.casefold()
        # remove punctuation except '-' and alphanumerics
        t = re.sub(r"[^0-9a-z\- ]+", " ", t)
        t = re.sub(r"\s{2,}", " ", t).strip()
        return t
    def __init__(self, config: dict) -> None:
        self.config = config
        self.moodle_cfg: dict = config.get("moodle", {})
        self.agent_cfg: dict = config.get("agents", {})
        self.solver_cfg: dict = config.get("solver", {})

        agents = build_agents(self.agent_cfg)
        self.orchestrator = AgentOrchestrator(
            agents=agents,
            calls_per_agent=self.agent_cfg.get("calls_per_agent", 2),
            temperature_low=self.agent_cfg.get("temperature_low", 0.2),
            temperature_high=self.agent_cfg.get("temperature_high", 0.7),
            timeout_seconds=self.agent_cfg.get("timeout_seconds", 270),
        )

        self._log: list[dict] = []
        self._vision_render_cache: dict[str, str] = {}
        self._debug_root: Optional[Path] = None
        self._attempt_id: Optional[str] = None

    def _ensure_debug_root(self) -> Path:
        logs_dir = self.solver_cfg.get("logs_dir", "logs")
        root = Path(logs_dir)
        root.mkdir(parents=True, exist_ok=True)
        if not self._attempt_id:
            # Fallback: use timestamp-only folder
            attempt_folder = f"attempt_unknown_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        else:
            attempt_folder = f"attempt_{self._attempt_id}"
        attempt_root = root / attempt_folder
        attempt_root.mkdir(parents=True, exist_ok=True)
        return attempt_root

    @staticmethod
    def _safe_slug(s: str) -> str:
        s = (s or "").strip().replace(" ", "_")
        s = re.sub(r"[^0-9A-Za-z_\-]+", "", s)
        return s[:80] or "item"

    @staticmethod
    def _extract_media_urls(html: str) -> dict[str, list[str]]:
        if not html:
            return {"img": [], "video": [], "audio": [], "source": [], "href": []}
        # crude but robust: capture common attributes
        def grab(attr: str) -> list[str]:
            return list({m.group(1) for m in re.finditer(rf'{attr}="([^"]+)"', html, flags=re.IGNORECASE)})

        return {
            "img": grab("src"),
            "source": grab("src"),
            "href": grab("href"),
            "poster": grab("poster"),
        }

    def _dump_question_artifacts(
        self,
        *,
        question: Question,
        prompt: str,
        round_answers: list[AgentAnswer],
        round_vision: list[AgentAnswer],
        result: VoteResult,
        image_b64: str | None = None,
    ) -> None:
        """
        Writes debug artifacts per question:
        - raw_html, annotated_text, prompt
        - parsed options
        - all agent raw responses
        - vote summary
        - rendered PNG (if available)
        - media manifest (URLs found in raw HTML)
        """
        try:
            if self._debug_root is None:
                self._debug_root = self._ensure_debug_root()

            qdir = self._debug_root / f"q{int(question.slot):03d}_{question.question_type.value}"
            qdir.mkdir(parents=True, exist_ok=True)

            # Core text artifacts
            if question.raw_html:
                (qdir / "raw.html").write_text(question.raw_html, encoding="utf-8", errors="ignore")
                manifest = self._extract_media_urls(question.raw_html)
                (qdir / "media_manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            (qdir / "question_text.txt").write_text(question.text or "", encoding="utf-8", errors="ignore")
            (qdir / "annotated_text.txt").write_text(question.prompt_text or "", encoding="utf-8", errors="ignore")
            (qdir / "prompt.txt").write_text(prompt or "", encoding="utf-8", errors="ignore")
            # Options / choices (varies by question type)
            qt = question.question_type
            if qt in (QuestionType.MCQ, QuestionType.TRUE_FALSE, QuestionType.MULTI_SELECT):
                options_payload: object = [
                    {"key": o.key, "label": o.label, "name": o.input_name, "value": o.input_value}
                    for o in (question.options or [])
                ]
            elif qt == QuestionType.GAP_SELECT:
                options_payload = [
                    {"name": g.input_name, "choices": g.choices, "choice_values": g.choice_values}
                    for g in (question.gap_items or [])
                ]
            elif qt == QuestionType.MATCHING:
                options_payload = [
                    {"stem": p.stem, "name": p.select_name, "choices": p.choices, "choice_values": p.choice_values}
                    for p in (question.match_pairs or [])
                ]
            elif qt == QuestionType.DRAG_DROP:
                # Word list is stored in choices of each gap item (usually identical).
                options_payload = {
                    "inputs": [g.input_name for g in (question.gap_items or [])],
                    "words": (question.gap_items[0].choices if question.gap_items and question.gap_items[0].choices else []),
                }
            elif qt == QuestionType.CLOZE:
                options_payload = [
                    {
                        "name": g.input_name,
                        "is_text": bool(getattr(g, "is_text", False)),
                        "choices": g.choices,
                        "choice_values": g.choice_values,
                    }
                    for g in (question.gap_items or [])
                ]
            else:
                options_payload = {
                    "note": "No structured options parsed for this question type.",
                    "question_type": str(getattr(qt, "value", qt)),
                    "has_options": bool(question.options),
                    "has_gap_items": bool(getattr(question, "gap_items", None)),
                    "has_match_pairs": bool(getattr(question, "match_pairs", None)),
                }

            (qdir / "options.json").write_text(
                json.dumps(options_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # Answers + vote
            (qdir / "vote_result.json").write_text(
                json.dumps(
                    {
                        "winner": result.winner,
                        "vote_count": result.vote_count,
                        "total_votes": result.total_votes,
                        "win_ratio": result.win_ratio,
                        "confidence_sum": result.confidence_sum,
                        "tiebreak_used": result.tiebreak_used,
                        "error_count": result.error_count,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            all_ans = round_answers + round_vision
            (qdir / "agent_answers.json").write_text(
                json.dumps(
                    [
                        {
                            "agent": a.agent_name,
                            "model": a.model_name,
                            "answer": a.answer,
                            "confidence": a.confidence,
                            "temperature": a.temperature,
                            "error": a.error,
                            "reasoning": a.reasoning,
                            "raw_response": a.raw_response,
                        }
                        for a in all_ans
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            # Rendered image (if we have it)
            if image_b64:
                try:
                    png_bytes = base64.b64decode(image_b64)
                    (qdir / "render.png").write_bytes(png_bytes)
                except Exception:
                    pass
        except Exception:
            # Never break solving due to logging
            return

    def _dump_attempt_summary(self) -> None:
        """
        Write a compact per-attempt summary log (no HTML/prompts/images).
        Location: logs/attempt_<id>/attempt_summary.json (+ .txt)
        """
        try:
            if self._debug_root is None:
                self._debug_root = self._ensure_debug_root()
            summary_path = self._debug_root / "attempt_summary.json"
            text_path = self._debug_root / "attempt_summary.txt"

            summary = {
                "timestamp": datetime.now().isoformat(),
                "attempt_id": self._attempt_id,
                "moodle_url": self.moodle_cfg.get("url", ""),
                "total_questions": len(self._log),
                "questions": [
                    {
                        "slot": q.get("slot"),
                        "question_id": q.get("question_id"),
                        "question_type": q.get("question_type"),
                        "submitted": q.get("submitted"),
                        "winner_raw": q.get("winner"),
                        "vote_count": q.get("vote_count"),
                        "total_votes": q.get("total_votes"),
                        "win_ratio": q.get("win_ratio"),
                        "confidence_sum": q.get("confidence_sum"),
                        "tiebreak_used": q.get("tiebreak_used"),
                        "error_count": q.get("error_count"),
                        "elapsed_seconds": q.get("elapsed_seconds"),
                    }
                    for q in self._log
                ],
            }

            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # Also write a short human-readable view.
            lines = [
                f"attempt_id={self._attempt_id}",
                f"total_questions={len(self._log)}",
                "",
            ]
            for q in self._log:
                slot = q.get("slot")
                qtype = q.get("question_type")
                submitted = q.get("submitted")
                win_ratio = q.get("win_ratio")
                elapsed = q.get("elapsed_seconds")
                lines.append(
                    f"slot={slot} type={qtype} submitted={submitted} "
                    f"win_ratio={win_ratio} elapsed_s={elapsed}"
                )
            text_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            return

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self, quiz_url_or_id: str) -> None:
        """Run the full solve loop for one quiz."""
        # Always enable fuzzy matching for Moodle dropdown choices.
        # Some quizzes contain typos in choices (e.g. "Fasle" instead of "False"),
        # and strict matching would leave blanks unfilled → "Incomplete answer".
        fuzzy_choice_match = True
        client = MoodleClient(
            base_url=self.moodle_cfg["url"],
            username=self.moodle_cfg["username"],
            password=self.moodle_cfg["password"],
            preferred_mode=ConnectionMode(
                self.moodle_cfg.get("preferred_mode", "auto")
            ),
            fuzzy_choice_match=fuzzy_choice_match,
        )

        console.print(
            Panel(
                f"[bold cyan]Moodle Quiz Solver[/bold cyan]\n"
                f"Target: [yellow]{quiz_url_or_id}[/yellow]\n"
                f"Agents: [green]{len(self.orchestrator.agents)}[/green] × "
                f"[green]{self.orchestrator.calls_per_agent}[/green] calls = "
                f"[bold green]{len(self.orchestrator.agents) * self.orchestrator.calls_per_agent}[/bold green] votes/question",
                title="Starting",
                border_style="cyan",
            )
        )

        async with client:
            mode_label = (
                "REST API" if client.mode == ConnectionMode.API else "Playwright"
            )
            console.print(f"[dim]Connection mode: {mode_label}[/dim]")

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                transient=True,
            ) as progress:
                task = progress.add_task("Starting quiz attempt...", total=None)
                attempt_id, questions = await client.start_quiz(quiz_url_or_id)
                progress.update(task, description=f"Loaded {len(questions)} questions")
            self._attempt_id = str(attempt_id)
            self._debug_root = self._ensure_debug_root()

            if not questions:
                console.print("[red]No questions found. Is the quiz active?[/red]")
                return

            # Defensive: dedupe questions by slot to avoid accidental repeats
            # (can happen with some API/scraper edge cases).
            seen_slots: set[str] = set()
            deduped: list[Question] = []
            dup_count = 0
            for q in questions:
                slot = str(getattr(q, "slot", ""))
                if slot and slot in seen_slots:
                    dup_count += 1
                    continue
                if slot:
                    seen_slots.add(slot)
                deduped.append(q)
            if dup_count:
                console.print(f"[yellow]Dropped {dup_count} duplicate question(s) by slot.[/yellow]")
            questions = deduped

            console.print(
                f"[bold]Quiz loaded:[/bold] {len(questions)} questions, "
                f"attempt ID: {attempt_id}"
            )
            console.rule()

            already_finished = False
            # Deferred queue: questions that couldn't be safely interpreted/submitted.
            # Configurable retries (default 1 extra try).
            max_defer_retries = int(self.solver_cfg.get("defer_retries", 1) or 1)
            queue: list[tuple[Question, int]] = [(q, 0) for q in questions]  # (question, retry_count)
            total_initial = len(queue)
            idx = 0
            while idx < len(queue):
                question, retry_count = queue[idx]
                idx += 1
                try:
                    ok = await self._solve_question(
                        client,
                        question,
                        min(idx, total_initial),
                        total_initial,
                        retry_count=retry_count,
                    )
                except MoodleAttemptFinishedError:
                    already_finished = True
                    break

                if not ok:
                    if retry_count < max_defer_retries:
                        console.print(
                            f"[yellow]Deferring question slot={question.slot} for one retry at end.[/yellow]"
                        )
                        queue.append((question, retry_count + 1))
                    else:
                        console.print(
                            f"[red]Skipping question slot={question.slot} after {max_defer_retries + 1} failed attempts.[/red]"
                        )

                delay = self.solver_cfg.get("question_delay", 1)
                if delay and idx < len(queue):
                    await asyncio.sleep(delay)

            if already_finished:
                console.rule()
                console.print(
                    "[bold yellow]This quiz attempt was already submitted. "
                    "Results may be viewable on Moodle.[/bold yellow]"
                )
            else:
                console.rule()
                if self.solver_cfg.get("auto_finish", False):
                    console.print("[bold cyan]Finishing quiz...[/bold cyan]")
                    await client.finish_quiz()
                    console.print("[bold green]Quiz submitted successfully![/bold green]")
                else:
                    console.print(
                        "[bold yellow]Not finishing attempt automatically.[/bold yellow]\n"
                        "All answers were saved. Open the quiz on Moodle and submit when ready."
                    )

        self._print_summary()
        self._save_log()
        self._dump_attempt_summary()

    # ------------------------------------------------------------------
    # Single question loop
    # ------------------------------------------------------------------

    @staticmethod
    def _count_empty_blanks(question: Question, answer) -> int:
        """Return the number of blanks that would be left unfilled by this answer."""
        if question.question_type in (
            QuestionType.GAP_SELECT, QuestionType.DRAG_DROP, QuestionType.CLOZE
        ):
            if not question.gap_items:
                return 0
            if not answer or answer in ("", [], {}):
                return len(question.gap_items)
            ans_dict: dict = {}
            if isinstance(answer, dict):
                ans_dict = answer
            elif isinstance(answer, list):
                ans_dict = {str(i + 1): v for i, v in enumerate(answer)}
            empty = sum(1 for i in range(len(question.gap_items)) if not ans_dict.get(str(i + 1)))
            return empty
        if question.question_type == QuestionType.MATCHING:
            if not question.match_pairs:
                return 0
            if not answer or not isinstance(answer, dict):
                return len(question.match_pairs)
            api = MoodleAPI.__new__(MoodleAPI)
            empty = 0
            for pair in question.match_pairs:
                chosen = api._fuzzy_lookup(answer, pair.stem)
                if not chosen:
                    empty += 1
            return empty
        return 0

    async def _solve_question(
        self,
        client: MoodleClient,
        question: Question,
        index: int,
        total: int,
        *,
        retry_count: int = 0,
    ) -> bool:
        console.print(
            f"\n[bold]Question {index}/{total}[/bold] "
            f"[dim]({question.question_type.value})[/dim]"
        )
        console.print(f"[white]{question.text[:300]}[/white]")

        # If there are no answer controls (informational block), skip safely.
        if self._looks_like_info_only(question):
            console.print("[dim]No answer fields detected for this question — skipping.[/dim]")
            self._log.append({
                "slot": question.slot,
                "question_id": question.question_id,
                "question_type": question.question_type.value,
                "question_text": question.text[:500],
                "winner": "",
                "submitted": None,
                "vote_count": 0,
                "total_votes": 0,
                "win_ratio": 0.0,
                "confidence_sum": 0.0,
                "tiebreak_used": False,
                "error_count": 0,
                "elapsed_seconds": 0.0,
                "skipped_reason": "info_only_no_answer_fields",
                "agent_answers": [],
            })
            return True

        if question.options:
            for opt in question.options:
                console.print(f"  [cyan]{opt.key.upper()}.[/cyan] {opt.label}")

        # Build prompt and ask agents (up to 3 attempts if blanks are left empty)
        # Multiple rounds are only useful for question types where we can
        # accidentally leave blanks unfilled.
        if question.question_type in (QuestionType.GAP_SELECT, QuestionType.DRAG_DROP, QuestionType.CLOZE, QuestionType.MATCHING):
            max_rounds = 3
        elif question.question_type == QuestionType.MCQ:
            # MCQ иногда ломается из-за мусорных опций ("Choose...", "Clear my choice").
            # Дадим 2-й (fallback) раунд если 1-й получился низкоуверенным или опции выглядят сломанными.
            max_rounds = 2
        else:
            max_rounds = 1
        result: Optional[VoteResult] = None
        all_answers: list[AgentAnswer] = []
        all_vision_answers: list[AgentAnswer] = []
        elapsed = 0.0
        vision_done = False

        for attempt_round in range(max_rounds):
            prompt = build_prompt(question)
            if attempt_round > 0:
                n_empty = self._count_empty_blanks(question, result.winner if result else None)
                prefix = ""
                if question.question_type == QuestionType.MCQ:
                    prefix = (
                        "IMPORTANT: The previous attempt may have had malformed options "
                        '(e.g., only "Choose..." / "Clear my choice") or low confidence. '
                        "Re-evaluate using the FULL question text and pick the best option.\n"
                        'If you cannot confidently return a LETTER, you may return the FULL TEXT of the correct option as \"answer\".\n\n'
                    )
                else:
                    prefix = (
                        f"IMPORTANT: Previous attempt left {n_empty} blank(s) unfilled. "
                        f"You MUST provide an answer for EVERY blank. Do not skip any.\n\n"
                    )
                prompt = prefix + prompt

            t0 = time.monotonic()
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                transient=True,
            ) as progress:
                label = (
                    f"Round {attempt_round+1} — "
                    f"asking {len(self.orchestrator.agents)} agents "
                    f"× {self.orchestrator.calls_per_agent}..."
                )
                progress.add_task(label, total=None)
                round_answers: list[AgentAnswer] = await self.orchestrator.ask_all(prompt)

                # Vision rendering/calls are expensive; do them at most once per question.
                round_vision: list[AgentAnswer] = []
                if not vision_done:
                    # Vision agents get separate tracking — they receive 3× vote weight
                    # so text-only models cannot dilute their answers on visual questions.
                    round_vision = await self._ask_vision_agents(question, prompt)
                    vision_done = True

            elapsed += time.monotonic() - t0
            all_answers.extend(round_answers)
            all_vision_answers.extend(round_vision)

            # Vote on all answers collected so far.
            # Vision answers are passed separately and given 3× weight so that
            # text-only models (which lack visual context) cannot outvote them.
            if question.question_type == QuestionType.ESSAY:
                result = vote_essay(all_answers)
            else:
                result = vote(
                    all_answers,
                    vision_answers=all_vision_answers if all_vision_answers else None,
                )

            # Persist per-question debug artifacts every round (best for QA).
            # Save the rendered image if we have it cached.
            image_b64 = None
            if question.raw_html:
                cache_key = f"{question.question_id}:{hash(question.raw_html)}"
                image_b64 = self._vision_render_cache.get(cache_key)
            self._dump_question_artifacts(
                question=question,
                prompt=prompt,
                round_answers=round_answers,
                round_vision=round_vision,
                result=result,
                image_b64=image_b64,
            )

            # Check completeness
            empty = self._count_empty_blanks(question, result.winner)
            if question.question_type == QuestionType.MCQ and attempt_round == 0 and max_rounds > 1:
                # Fallback criteria for MCQ
                avg_conf = (result.confidence_sum / max(result.vote_count, 1)) if result else 0.0
                opts_text = question.formatted_options()
                opts_bad = (not opts_text) or ("choose..." in opts_text.lower()) or ("clear my choice" in opts_text.lower())
                if opts_bad or avg_conf < 0.5:
                    console.print(
                        f"  [yellow]⚠ MCQ fallback:[/yellow] options_bad={opts_bad}, avg_conf={avg_conf:.2f} — retrying..."
                    )
                    continue

            if empty == 0:
                break
            if attempt_round < max_rounds - 1:
                console.print(
                    f"  [yellow]⚠ {empty} blank(s) still empty after round {attempt_round+1} — retrying...[/yellow]"
                )

        # Display final result
        self._display_vote_result(result, elapsed)

        # Submit
        winner = result.winner if result else ""

        # Before submit: if mandatory structure is missing (options/blanks/pairs),
        # try to recover it from raw_html (best effort).
        self._ensure_question_parsed(question)

        if question.question_type == QuestionType.MCQ:
            letter, text = self._extract_mcq_letter_and_text(winner)
            mapped_letter, score = self._map_mcq_text_to_letter(question, text) if text else ("", 0.0)

            # Soft validation:
            # - Prefer model's letter if it looks sane.
            # - If text clearly maps to a different letter, use mapped letter (prevents option-shift bugs).
            # - If we can't map anything and don't even have a letter, defer.
            chosen_letter = ""
            if letter:
                chosen_letter = letter
            if mapped_letter and score >= 0.72:
                if chosen_letter and mapped_letter != chosen_letter:
                    console.print(
                        f"[yellow]MCQ validate:[/yellow] model_letter={chosen_letter} "
                        f"but text maps to {mapped_letter} (score={score:.2f}) — using mapped letter."
                    )
                chosen_letter = mapped_letter

            if not chosen_letter:
                console.print(
                    "[red]MCQ interpret failed:[/red] no usable letter and text could not be mapped. "
                    "Deferring."
                )
                return False

            winner = chosen_letter

        # If we still can't submit reliably (e.g. zero options), defer.
        if not self._is_question_ready_for_submit(question):
            console.print(
                "[red]Cannot submit:[/red] missing required parsed fields "
                f"for type={question.question_type.value}. Deferring."
            )
            return False

        submitted = None
        if winner not in ("", [], {}):
            try:
                await client.submit_answer(question, winner)
                question.chosen_answer = winner
                submitted = winner
            except MoodleAttemptFinishedError:
                console.print(
                    "[yellow]Quiz attempt is already finished — answers were previously submitted.[/yellow]"
                )
                raise
            except Exception as exc:
                console.print(
                    f"[red]submit_answer FAILED:[/red] {exc}"
                )
                # Try to extract the underlying cause
                # In API mode, the exception message from _call may contain the Moodle response
                error_msg = str(exc)
                if hasattr(exc, '__cause__') and exc.__cause__:
                    error_msg += f" | Cause: {exc.__cause__}"
                console.print(f"[red]  Error details: {error_msg[:500]}[/red]")
                return False
        else:
            console.print("[red]WARNING: No valid answer found — skipping submission.[/red]")
            return False

        # Post-submit verification (API mode): refetch question HTML and confirm saved fields.
        if client.mode == ConnectionMode.API and submitted is not None:
            # Capture questionsummary snapshot right after save for false-negative detection.
            qs_map: dict[str, str] = {}
            try:
                qs_map = await client.submit_answer_capture(question, submitted)
            except Exception:
                pass
            ok = await self._verify_saved(client, question, submitted_answer=submitted, questionsummary_map=qs_map)
            if not ok:
                console.print("[yellow]Post-submit verify failed — retrying once with refreshed HTML...[/yellow]")
                refreshed = await client.refetch_questions()
                q2 = next((q for q in refreshed if q.slot == question.slot), None)
                if not q2 or not q2.raw_html:
                    return False

                # Refresh HTML + parsed structure, then retry submit once.
                question.raw_html = q2.raw_html
                question.annotated_text = question.annotated_text or q2.annotated_text
                self._ensure_question_parsed(question)
                if not self._is_question_ready_for_submit(question):
                    return False

                await client.submit_answer(question, submitted)
                ok2 = await self._verify_saved(client, question, submitted_answer=submitted, questionsummary_map=qs_map)
                if not ok2:
                    console.print("[red]Post-submit verify failed twice — deferring.[/red]")
                    return False

        # Log
        self._log.append({
            "slot": question.slot,
            "question_id": question.question_id,
            "question_type": question.question_type.value,
            "question_text": question.text[:500],
            "winner": result.winner,
            "submitted": submitted,
            "vote_count": result.vote_count,
            "total_votes": result.total_votes,
            "win_ratio": result.win_ratio,
            "confidence_sum": result.confidence_sum,
            "tiebreak_used": result.tiebreak_used,
            "error_count": result.error_count,
            "elapsed_seconds": round(elapsed, 2),
            "agent_answers": [
                {
                    "agent": a.agent_name,
                    "model": a.model_name,
                    "answer": a.answer,
                    "confidence": a.confidence,
                    "temperature": a.temperature,
                    "error": a.error,
                }
                for a in all_answers
            ],
        })
        return True

    @staticmethod
    def _looks_like_info_only(question: Question) -> bool:
        """
        Some Moodle pages include non-question informational blocks.
        If raw_html contains no answer controls, treat it as info-only.
        """
        if not question.raw_html:
            return False
        soup = BeautifulSoup(question.raw_html, "html.parser")
        container = soup.find("div", class_="answer") or soup
        # Ignore hidden inputs/buttons
        for inp in container.find_all(["input", "select", "textarea"]):
            if inp.name == "select" or inp.name == "textarea":
                return False
            t = (inp.get("type") or "").lower()
            if t not in ("hidden", "submit", "button"):
                return False
        # No visible inputs/selects/textareas found
        return True

    @staticmethod
    def _is_question_ready_for_submit(question: Question) -> bool:
        """
        Return True if we have enough parsed structure to submit an answer reliably.
        """
        qt = question.question_type
        if qt in (QuestionType.MCQ, QuestionType.MULTI_SELECT, QuestionType.TRUE_FALSE):
            return bool(question.options)
        if qt == QuestionType.MATCHING:
            return bool(question.match_pairs)
        if qt in (QuestionType.GAP_SELECT, QuestionType.DRAG_DROP, QuestionType.CLOZE):
            return bool(question.gap_items)
        if qt in (QuestionType.SHORT_ANSWER, QuestionType.ESSAY):
            return True
        return True

    def _ensure_question_parsed(self, question: Question) -> None:
        """
        Best-effort recovery of missing parsed fields from raw_html.
        Never raises.
        """
        try:
            if not question.raw_html:
                return
            api = MoodleAPI.__new__(MoodleAPI)
            qt = question.question_type

            if qt in (QuestionType.MCQ, QuestionType.MULTI_SELECT, QuestionType.TRUE_FALSE) and not question.options:
                question.options = api._parse_options_from_html(question.raw_html)

            if qt == QuestionType.MATCHING and not question.match_pairs:
                question.match_pairs = api._parse_match_pairs_from_html(question.raw_html)

            if qt == QuestionType.GAP_SELECT and not question.gap_items:
                question.gap_items = api._parse_gapselect_from_html(question.raw_html)

            if qt == QuestionType.DRAG_DROP and not question.gap_items:
                question.gap_items = api._parse_ddwtos_from_html(question.raw_html)

            if qt == QuestionType.CLOZE and not question.gap_items:
                question.gap_items = api._parse_cloze_from_html(question.raw_html)

            # Refresh sequencecheck if missing (submission often requires it)
            if question.sequencecheck_name is None or question.sequencecheck_value is None:
                sc_name, sc_val = api._find_sequencecheck(question.raw_html)
                question.sequencecheck_name = sc_name
                question.sequencecheck_value = sc_val
        except Exception:
            return

    @staticmethod
    def _is_checked(inp) -> bool:
        return inp.has_attr("checked") or str(inp.get("aria-checked", "")).lower() == "true"

    @staticmethod
    def _extract_checked_letters_from_html(html: str, *, input_type: str) -> list[str]:
        """
        Extract selected option letters from Moodle HTML by looking for checked inputs
        inside div.answer. input_type: "radio" or "checkbox".
        """
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        container = soup.find("div", class_="answer") or soup
        inputs = container.find_all("input", {"type": input_type})
        labels = ["A", "B", "C", "D", "E", "F", "G", "H"]
        out: list[str] = []
        for i, inp in enumerate(inputs):
            if QuizSolver._is_checked(inp):
                if i < len(labels):
                    out.append(labels[i])
        return out

    @staticmethod
    def _selected_value(select) -> str:
        # Moodle usually marks the selected option with selected attr
        opt = select.find("option", selected=True)
        if opt is None:
            # Fallback: first option with selected="selected" or current value-like
            for o in select.find_all("option"):
                if str(o.get("selected", "")).lower() in ("selected", "true", "1"):
                    opt = o
                    break
        if opt is None:
            return ""
        return str(opt.get("value", "")).strip()

    @staticmethod
    def _input_value_by_name(soup: BeautifulSoup, name: str) -> str:
        if not name:
            return ""
        tag = soup.find(attrs={"name": name})
        if not tag:
            return ""
        if tag.name == "textarea":
            return (tag.get_text() or "").strip()
        return str(tag.get("value", "")).strip()

    async def _verify_saved(
        self, client: MoodleClient, question: Question, *, submitted_answer,
        questionsummary_map: dict[str, str] | None = None,
    ) -> bool:
        """
        Verify (API mode) that Moodle shows the answer as saved for this question.
        Checks that required fields are filled for the question type.

        questionsummary_map: optional dict of slot->questionsummary text captured
        right after save. Used to detect false negatives for CLOZE text inputs
        (Moodle API HTML doesn't show input values even when saved).
        """
        try:
            refreshed = await client.refetch_questions()
            q = next((qq for qq in refreshed if qq.slot == question.slot), None)
            if not q or not q.raw_html:
                return False
            html = q.raw_html
            soup = BeautifulSoup(html, "html.parser")
            qt = question.question_type

            if qt in (QuestionType.MCQ, QuestionType.TRUE_FALSE):
                chosen = str(submitted_answer).strip().upper()
                saved = self._extract_checked_letters_from_html(html, input_type="radio")
                return bool(saved) and saved[0] == chosen

            if qt == QuestionType.MULTI_SELECT:
                want = submitted_answer if isinstance(submitted_answer, list) else [str(submitted_answer)]
                want_set = {str(x).strip().upper() for x in want if str(x).strip()}
                saved_set = set(self._extract_checked_letters_from_html(html, input_type="checkbox"))
                return bool(want_set) and want_set == saved_set

            if qt in (QuestionType.SHORT_ANSWER, QuestionType.ESSAY):
                # Use known input name when possible
                name = question.text_input_name or ""
                val = self._input_value_by_name(soup, name) if name else ""
                if not val:
                    # fallback: any textarea or text input in answer container
                    container = soup.find("div", class_="answer") or soup
                    ta = container.find("textarea")
                    if ta:
                        val = (ta.get_text() or "").strip()
                    else:
                        inp = container.find("input", {"type": "text"})
                        if inp:
                            val = str(inp.get("value", "")).strip()
                return bool(val)

            if qt == QuestionType.MATCHING:
                # Each select must be non-zero
                if not question.match_pairs:
                    # fallback: any selects in answer container
                    container = soup.find("div", class_="answer") or soup
                    sels = container.find_all("select")
                    return all(self._selected_value(s) not in ("", "0") for s in sels) if sels else False
                for pair in question.match_pairs:
                    tag = soup.find("select", attrs={"name": pair.select_name})
                    if not tag:
                        return False
                    if self._selected_value(tag) in ("", "0"):
                        return False
                return True

            if qt == QuestionType.GAP_SELECT:
                if not question.gap_items:
                    return False
                for gap in question.gap_items:
                    tag = soup.find("select", attrs={"name": gap.input_name})
                    if not tag:
                        return False
                    if self._selected_value(tag) in ("", "0"):
                        return False
                return True

            if qt == QuestionType.DRAG_DROP:
                # hidden inputs like *_p1 must be non-zero
                if not question.gap_items:
                    return False
                for gap in question.gap_items:
                    val = self._input_value_by_name(soup, gap.input_name)
                    if val in ("", "0"):
                        return False
                return True

            if qt == QuestionType.CLOZE:
                if not question.gap_items:
                    return False
                for gap in question.gap_items:
                    if gap.is_text:
                        val = self._input_value_by_name(soup, gap.input_name)
                        if not val:
                            # False negative guard: Moodle API HTML doesn't persist
                            # input value attributes even when answer IS saved.
                            # If we have a questionsummary_map and this slot has content,
                            # it means Moodle processed the question — trust it.
                            if questionsummary_map:
                                qs = questionsummary_map.get(str(question.slot), "")
                                if qs.strip():
                                    continue
                            return False
                    else:
                        tag = soup.find("select", attrs={"name": gap.input_name})
                        if not tag:
                            return False
                        if self._selected_value(tag) in ("", "0"):
                            return False
                return True

            # Unknown types: best effort — assume saved
            return True
        except Exception:
            return False

    @staticmethod
    def _extract_mcq_letter_and_text(winner: object) -> tuple[str, str]:
        """
        Accept MCQ winner formats:
        - "C"
        - "Some option text..."
        - {"letter": "C", "text": "..."}  (new recommended format)
        """
        if isinstance(winner, dict):
            letter = str(winner.get("letter", "")).strip().upper()
            text = str(winner.get("text", "")).strip()
            # Some models might return "answer": {"answer": "..."} etc; be defensive
            if not text and "label" in winner:
                text = str(winner.get("label", "")).strip()
            if len(letter) == 1 and "A" <= letter <= "H":
                return letter, text
            return "", text
        if isinstance(winner, str):
            s = winner.strip()
            if len(s) == 1 and "A" <= s.upper() <= "H":
                return s.upper(), ""
            return "", s
        return "", ""

    @staticmethod
    def _map_mcq_text_to_letter(question: Question, text: str) -> tuple[str, float]:
        """
        Map a free-form option text back to a letter using fuzzy similarity.
        Returns (LETTER, score) or ("", 0.0) if not possible.
        """
        if not text:
            return "", 0.0
        def tokens(s: str) -> set[str]:
            if not s:
                return set()
            return {t for t in s.split(" ") if t}

        def jaccard(a: set[str], b: set[str]) -> float:
            if not a and not b:
                return 1.0
            if not a or not b:
                return 0.0
            inter = len(a & b)
            union = len(a | b)
            return inter / union if union else 0.0

        def containment(a: set[str], b: set[str]) -> float:
            # How much of a is covered by b (asymmetric)
            if not a:
                return 0.0
            return len(a & b) / len(a)

        def score_pair(opt_norm: str, ans_norm: str) -> float:
            if not opt_norm or not ans_norm:
                return 0.0
            if opt_norm == ans_norm:
                return 1.0

            a_tok = tokens(opt_norm)
            b_tok = tokens(ans_norm)

            seq = difflib.SequenceMatcher(None, opt_norm, ans_norm).ratio()
            jac = jaccard(a_tok, b_tok)
            cov = max(containment(a_tok, b_tok), containment(b_tok, a_tok))
            # substring / near-substring helps when model truncates or adds small extras
            sub = 1.0 if (opt_norm in ans_norm or ans_norm in opt_norm) else 0.0

            # Weighted blend: token similarity dominates; seq helps for short strings.
            return max(
                0.55 * jac + 0.30 * cov + 0.15 * seq,
                0.85 * sub + 0.15 * seq,
            )

        candidates: list[tuple[str, float]] = []
        for opt in question.options:
            label = str(opt.label or "").strip()
            if not label:
                continue
            # Similarity on normalised strings
            a = QuizSolver._norm_loose(label)
            b = QuizSolver._norm_loose(text)
            score = score_pair(a, b)
            candidates.append((opt.key.upper(), score))
        if not candidates:
            return "", 0.0
        # Prefer the best score, but also require a margin to avoid ambiguous matches.
        candidates.sort(key=lambda x: x[1], reverse=True)
        best_letter, best_score = candidates[0]
        second = candidates[1][1] if len(candidates) > 1 else 0.0
        margin = best_score - second
        # If too ambiguous, treat as not mappable.
        if best_score < 0.60 or margin < 0.06:
            return "", float(best_score)
        return best_letter, float(best_score)

    # ------------------------------------------------------------------
    # Vision helpers (for table / image-based questions)
    # ------------------------------------------------------------------

    # Question types that benefit from visual rendering (contain complex tables)
    _VISION_TYPES = (QuestionType.CLOZE, QuestionType.MATCHING, QuestionType.UNKNOWN)

    async def _ask_vision_agents(
        self, question: Question, prompt: str
    ) -> list[AgentAnswer]:
        """
        Render the question's raw HTML as a PNG image and send to vision-capable agents.
        Only runs when:
          1. The question has raw_html.
          2. The question type is one where visual context helps (table matching, etc.).
          3. Vision agents are available.
        Returns empty list if Playwright is unavailable or no vision agents exist.
        """
        if not question.raw_html:
            return []
        if question.question_type not in self._VISION_TYPES:
            return []
        # CLOZE часто чисто текстовый; включаем vision для CLOZE только если есть таблица/картинка.
        if question.question_type == QuestionType.CLOZE:
            html_l = question.raw_html.lower()
            if ("<table" not in html_l) and ("<img" not in html_l) and ("svg" not in html_l):
                return []

        # Find vision-capable agents (Gemini, Groq, and any agent with supports_vision=True)
        vision_agents = []
        for agent in self.orchestrator.agents:
            if isinstance(agent, (GeminiFlash25Agent, GroqLlama4ScoutAgent)):
                if hasattr(agent, "ask_with_image"):
                    vision_agents.append(agent)
            elif getattr(agent, "supports_vision", False) and hasattr(agent, "ask_with_image"):
                vision_agents.append(agent)

        if not vision_agents:
            return []

        # Try to render HTML to PNG via Playwright (cached per question HTML)
        cache_key = f"{question.question_id}:{hash(question.raw_html)}"
        image_b64 = self._vision_render_cache.get(cache_key)
        if not image_b64:
            image_b64 = await self._render_html_to_base64(question.raw_html)
            if image_b64:
                self._vision_render_cache[cache_key] = image_b64
        if not image_b64:
            return []

        # Run vision calls (match normal calls_per_agent temps)
        vision_tasks = []
        for agent in vision_agents:
            for temp in self.orchestrator._temperatures():
                async def _vision_call(a=agent, t=temp):
                    try:
                        return await asyncio.wait_for(
                            a.ask_with_image(prompt, image_b64, t),
                            timeout=self.orchestrator.timeout_seconds,
                        )
                    except Exception as exc:
                        return AgentAnswer(
                            agent_name=a.name + "_vision",
                            model_name=a.model,
                            answer="",
                            confidence=0.0,
                            reasoning="",
                            temperature=t,
                            raw_response="",
                            error=str(exc),
                        )
                vision_tasks.append(asyncio.create_task(_vision_call()))

        if vision_tasks:
            results = await asyncio.gather(*vision_tasks)
            return list(results)
        return []

    @staticmethod
    async def _render_html_to_base64(html: str) -> Optional[str]:
        """Render HTML fragment to PNG via Playwright and return base64. Returns None on failure."""
        try:
            from playwright.async_api import async_playwright
            import base64
            wrapped = f"""<!DOCTYPE html><html><body style="background:white;padding:20px;font-family:Arial,sans-serif;max-width:900px">
{html}
</body></html>"""
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page(viewport={"width": 950, "height": 2000})
                await page.set_content(wrapped, wait_until="domcontentloaded")
                png_bytes = await page.screenshot(full_page=True)
                await browser.close()
            return base64.b64encode(png_bytes).decode()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def _display_vote_result(self, result: VoteResult, elapsed: float) -> None:
        show_reasoning = self.solver_cfg.get("show_reasoning", True)

        table = Table(box=box.SIMPLE, show_header=True, header_style="bold magenta")
        table.add_column("Agent", style="cyan", no_wrap=True)
        table.add_column("Answer", style="white")
        table.add_column("Conf", justify="right", style="yellow")
        table.add_column("Temp", justify="right", style="dim")
        table.add_column("Status", style="green")

        for ans in result.all_answers:
            is_winner = str(ans.answer).strip().upper() == str(result.winner).strip().upper()
            status = "[bold green]✓[/bold green]" if is_winner else ""
            if ans.error:
                status = f"[red]ERR[/red]"
            table.add_row(
                ans.agent_name,
                str(ans.answer)[:60],
                f"{ans.confidence:.2f}",
                f"{ans.temperature:.1f}",
                status,
            )

        console.print(table)

        # Winner banner
        win_pct = f"{result.win_ratio * 100:.0f}%"
        tiebreak_note = " [dim](tiebreak)[/dim]" if result.tiebreak_used else ""

        letter, text = self._extract_mcq_letter_and_text(result.winner)
        if letter and text:
            answer_display = f"{letter}: {text[:100]}"
        elif letter:
            answer_display = letter
        elif text:
            answer_display = text[:100]
        else:
            answer_display = str(result.winner) if result.winner else ""

        console.print(
            f"  [bold green]→ ANSWER:[/bold green] [bold white]{answer_display}[/bold white]"
            f"  [dim]{result.vote_count}/{result.total_votes} votes ({win_pct}){tiebreak_note}  {elapsed:.1f}s[/dim]"
        )

        if show_reasoning and result.reasoning_samples:
            sample = result.reasoning_samples[0]
            console.print(f"  [dim italic]{sample[:200]}[/dim italic]")

    def _print_summary(self) -> None:
        if not self._log:
            return
        total = len(self._log)
        errors = sum(1 for q in self._log if q["error_count"] == q["total_votes"])
        tiebreaks = sum(1 for q in self._log if q["tiebreak_used"])
        avg_ratio = sum(q["win_ratio"] for q in self._log) / total

        console.print(
            Panel(
                f"[bold]Total questions:[/bold] {total}\n"
                f"[bold]Average consensus:[/bold] {avg_ratio * 100:.0f}%\n"
                f"[bold]Tiebreaks used:[/bold] {tiebreaks}\n"
                f"[bold]Fully failed questions:[/bold] {errors}",
                title="[bold cyan]Session Summary[/bold cyan]",
                border_style="cyan",
            )
        )

    def _save_log(self) -> None:
        if not self.solver_cfg.get("save_log", True):
            return
        log_file = Path(self.solver_cfg.get("log_file", "quiz_log.json"))
        existing: list[dict] = []
        if log_file.exists():
            try:
                existing = json.loads(log_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = []

        session = {
            "timestamp": datetime.now().isoformat(),
            "questions": self._log,
        }
        existing.append(session)
        log_file.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        console.print(f"[dim]Log saved to {log_file}[/dim]")
