"""
Unified Moodle client.

Tries the REST API first (fast, no browser needed).
If the API is unavailable or login fails, transparently falls back to
the Playwright headless browser scraper.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from .api import MoodleAPI, MoodleAPIError, MoodleAttemptFinishedError
from .models import CourseInfo, Question, Quiz
from .scraper import MoodleScraper, MoodleScraperError


class ConnectionMode(str, Enum):
    AUTO = "auto"
    API = "api"
    SCRAPER = "scraper"


class MoodleClient:
    """
    High-level Moodle interface used by the solver.

    Exposes the same methods regardless of whether the REST API or the
    Playwright scraper is being used underneath.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        preferred_mode: ConnectionMode = ConnectionMode.AUTO,
        headless: bool = True,
        fuzzy_choice_match: bool = False,
    ) -> None:
        self.base_url = base_url
        self.username = username
        self.password = password
        self.preferred_mode = preferred_mode
        self.headless = headless
        self.fuzzy_choice_match = bool(fuzzy_choice_match)

        self._api: Optional[MoodleAPI] = None
        self._scraper: Optional[MoodleScraper] = None
        self._active_mode: Optional[ConnectionMode] = None
        self._current_quiz: Optional[Quiz] = None
        self._attempt_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> ConnectionMode:
        """
        Establish connection. Returns the mode actually used.
        In AUTO mode, tries API first; falls back to scraper on failure.
        """
        if self.preferred_mode in (ConnectionMode.AUTO, ConnectionMode.API):
            try:
                self._api = MoodleAPI(
                    self.base_url,
                    self.username,
                    self.password,
                    fuzzy_choice_match=self.fuzzy_choice_match,
                )
                await self._api.__aenter__()
                self._active_mode = ConnectionMode.API
                return ConnectionMode.API
            except MoodleAPIError as exc:
                if self.preferred_mode == ConnectionMode.API:
                    raise
                # Fall through to scraper
                print(f"[client] REST API unavailable ({exc}), switching to scraper.")

        self._scraper = MoodleScraper(
            self.base_url, self.username, self.password, headless=self.headless
        )
        await self._scraper.__aenter__()
        self._active_mode = ConnectionMode.SCRAPER
        return ConnectionMode.SCRAPER

    async def disconnect(self) -> None:
        if self._api:
            await self._api.__aexit__(None, None, None)
        if self._scraper:
            await self._scraper.__aexit__(None, None, None)

    async def __aenter__(self) -> "MoodleClient":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.disconnect()

    @property
    def mode(self) -> Optional[ConnectionMode]:
        return self._active_mode

    # ------------------------------------------------------------------
    # Courses & quiz discovery
    # ------------------------------------------------------------------

    async def get_enrolled_courses(self) -> list[CourseInfo]:
        if self._active_mode == ConnectionMode.API:
            return await self._api.get_enrolled_courses()
        return await self._scraper.get_enrolled_courses()

    async def get_quizzes_by_course(self, course_id: str) -> list[Quiz]:
        if self._active_mode == ConnectionMode.API:
            return await self._api.get_quizzes_by_course(course_id)
        return await self._scraper.get_quizzes_by_course(course_id)

    async def get_all_quizzes(self) -> list[Quiz]:
        """Return quizzes from all enrolled courses."""
        courses = await self.get_enrolled_courses()
        all_quizzes: list[Quiz] = []
        for course in courses:
            quizzes = await self.get_quizzes_by_course(course.course_id)
            course.quizzes = quizzes
            all_quizzes.extend(quizzes)
        return all_quizzes

    # ------------------------------------------------------------------
    # Monitoring helpers
    # ------------------------------------------------------------------

    async def list_unfinished_attempts(self, quiz_id: str) -> list[dict]:
        """List unfinished attempts for the current user for quiz_id (API only)."""
        if self._active_mode == ConnectionMode.API:
            return await self._api.list_unfinished_attempts(quiz_id)
        return []

    # ------------------------------------------------------------------
    # Attempt management
    # ------------------------------------------------------------------

    async def start_quiz(self, quiz_id_or_url: str) -> tuple[str, list[Question]]:
        """
        Start (or resume) a quiz and fetch all questions.
        Returns (attempt_id, questions).
        """
        if self._active_mode == ConnectionMode.API:
            # If a URL was provided, convert cmid → quiz_id first
            quiz_id = quiz_id_or_url
            if quiz_id_or_url.startswith("http"):
                import re
                cmid = re.search(r"[?&]id=(\d+)", quiz_id_or_url)
                if cmid:
                    quiz_id = await self._api.cmid_to_quiz_id(cmid.group(1))

            attempt_id = await self._api.start_attempt(quiz_id)
            self._attempt_id = attempt_id
            questions = await self._api.get_all_questions(attempt_id)
            return attempt_id, questions
        else:
            attempt_id = await self._scraper.start_attempt(quiz_id_or_url)
            self._attempt_id = attempt_id
            questions = await self._scraper.get_all_questions()
            return attempt_id, questions

    # ------------------------------------------------------------------
    # Answer submission
    # ------------------------------------------------------------------

    async def submit_answer(
        self,
        question: Question,
        answer: str | list[str] | dict[str, str],
    ) -> None:
        """Submit the answer for a single question."""
        if self._active_mode == ConnectionMode.API:
            await self._api.save_answer(self._attempt_id, question, answer)
        else:
            await self._scraper.fill_and_submit_answer(question, answer)

    async def submit_answer_capture(
        self,
        question: Question,
        answer: str | list[str] | dict[str, str],
    ) -> dict[str, str]:
        """
        Submit the answer and capture questionsummary snapshot for verification.
        Returns dict of slot -> questionsummary text for all questions on that page.
        Used by solver's _verify_saved to detect false negatives.
        """
        if self._active_mode == ConnectionMode.API:
            await self._api.save_answer(self._attempt_id, question, answer)
        else:
            await self._scraper.fill_and_submit_answer(question, answer)
        summary_map: dict[str, str] = {}
        try:
            raw_qs = await self._api._get_raw_questions(self._attempt_id)
            for qd in raw_qs:
                slot = str(qd.get("slot", ""))
                qs = qd.get("questionsummary", "")
                summary_map[slot] = qs
        except Exception:
            pass
        return summary_map

    async def finish_quiz(self) -> dict:
        """Finish and submit the quiz attempt."""
        if self._active_mode == ConnectionMode.API:
            return await self._api.finish_attempt(self._attempt_id)
        else:
            await self._scraper.finish_attempt()
            return {}

    async def refetch_questions(self) -> list[Question]:
        """
        Re-fetch questions for the current attempt (API only).
        Useful for verifying that an answer was persisted.
        """
        if not self._attempt_id:
            return []
        if self._active_mode == ConnectionMode.API:
            return await self._api.get_all_questions(self._attempt_id)
        # Scraper mode re-fetching is expensive and not implemented here.
        return []
