"""
Moodle REST API client.

Authenticates via /login/token.php and uses the Moodle Web Services API.
Falls back gracefully — callers should catch MoodleAPIError and switch to
the Playwright scraper.
"""

from __future__ import annotations

import re
import unicodedata
import difflib
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .models import (
    AnswerOption,
    CourseInfo,
    GapItem,
    MatchPair,
    Question,
    QuestionType,
    Quiz,
)


class MoodleAPIError(Exception):
    """General Moodle API error."""


class MoodleAttemptFinishedError(MoodleAPIError):
    """Raised when trying to save answers to an already-finished attempt."""


class MoodleAPI:
    """Thin async wrapper around the Moodle Web Services REST API."""

    WS_ENDPOINT = "/webservice/rest/server.php"
    TOKEN_ENDPOINT = "/login/token.php"

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        fuzzy_choice_match: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.fuzzy_choice_match = bool(fuzzy_choice_match)
        self.token: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "MoodleAPI":
        self._client = httpx.AsyncClient(timeout=30, follow_redirects=True)
        await self.login()
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def login(self) -> None:
        """Obtain a web service token via /login/token.php."""
        url = urljoin(self.base_url, self.TOKEN_ENDPOINT)
        params = {
            "username": self.username,
            "password": self.password,
            "service": "moodle_mobile_app",
        }
        try:
            resp = await self._client.post(url, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise MoodleAPIError(f"HTTP error during login: {exc}") from exc

        data = resp.json()
        if "token" not in data:
            error = data.get("error", data.get("debuginfo", str(data)))
            raise MoodleAPIError(f"Login failed: {error}")

        self.token = data["token"]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _call(self, function: str, **params) -> dict | list:
        """Make a Web Services API call."""
        if not self.token:
            raise MoodleAPIError("Not authenticated — call login() first.")

        url = urljoin(self.base_url, self.WS_ENDPOINT)
        payload = {
            "wstoken": self.token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
            **params,
        }
        try:
            resp = await self._client.post(url, data=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise MoodleAPIError(f"HTTP error calling {function}: {exc}") from exc

        result = resp.json()
        if isinstance(result, dict) and "exception" in result:
            raise MoodleAPIError(
                f"API error in {function}: {result.get('message', result)}"
            )
        return result

    @staticmethod
    def _strip_html(html: str) -> str:
        """Remove HTML tags and decode entities."""
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text(separator=" ").strip()

    @staticmethod
    def _annotated_text(html: str) -> str:
        """
        Convert HTML to readable text with blank markers instead of inputs/selects.
        Tables are rendered with | separators so structure is preserved.
        Each <input type="text">, <select>, and drag-drop blank is replaced with
        _____N_____ (where N is 1-based blank number) so the AI knows exactly
        where each answer should go.
        """
        soup = BeautifulSoup(html, "html.parser")

        # Remove Moodle "Flag question" UI — it is not part of answers, but it
        # contains a checkbox input which would otherwise be mistaken for a blank.
        for flag in soup.select("div.questionflag"):
            flag.decompose()

        # Replace all form inputs with ___N___ markers (numbered in DOM order)
        blank_counter = [0]

        def replace_input(tag) -> str:
            # Skip hidden inputs (sequencecheck, etc.)
            if tag.get("type") in ("hidden", "submit", "button"):
                return ""
            blank_counter[0] += 1
            return f" [BLANK {blank_counter[0]}] "

        for tag in soup.find_all(["input", "select", "textarea"]):
            # Skip Moodle question-flag checkbox if it survived for any reason
            name = tag.get("name", "") or ""
            tag_type = tag.get("type") or ""
            if ":flagged" in name or "flaggedcheckbox" in (tag.get("id") or ""):
                tag.decompose()
                continue

            if tag_type in ("hidden", "submit", "button"):
                tag.decompose()
            else:
                blank_counter[0] += 1
                tag.replace_with(f"[BLANK {blank_counter[0]}]")

        # Render tables with | cell separators so structure survives
        for table in soup.find_all("table"):
            lines = []
            for row in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                lines.append(" | ".join(cells))
            table.replace_with("\n" + "\n".join(lines) + "\n")

        # Collapse excess whitespace
        text = soup.get_text(separator=" ").strip()
        # Strip Moodle UI strings that are not part of the question content.
        # They often appear near "Select one" and can pollute option detection.
        text = re.sub(r"\bClear my choice\b", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\bFlag question\b", "", text, flags=re.IGNORECASE)
        text = re.sub(r'[ \t]{3,}', '  ', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text

    # ------------------------------------------------------------------
    # Courses & quizzes
    # ------------------------------------------------------------------

    async def get_enrolled_courses(self) -> list[CourseInfo]:
        """Return all courses the current user is enrolled in."""
        user_info = await self._call("core_webservice_get_site_info")
        user_id = user_info["userid"]

        raw_courses = await self._call(
            "core_enrol_get_users_courses", userid=user_id
        )
        courses: list[CourseInfo] = []
        for c in raw_courses:
            courses.append(
                CourseInfo(
                    course_id=str(c["id"]),
                    full_name=self._strip_html(c.get("fullname", "")),
                    short_name=c.get("shortname", ""),
                )
            )
        return courses

    async def get_quizzes_by_course(self, course_id: str) -> list[Quiz]:
        """Return all quizzes in a course."""
        data = await self._call(
            "mod_quiz_get_quizzes_by_courses",
            **{"courseids[0]": course_id},
        )
        quizzes: list[Quiz] = []
        for q in data.get("quizzes", []):
            quizzes.append(
                Quiz(
                    quiz_id=str(q["id"]),
                    course_id=course_id,
                    cmid=str(q.get("coursemodule") or q.get("cmid") or "") or None,
                    name=self._strip_html(q.get("name", "")),
                    description=self._strip_html(q.get("intro", "")),
                    time_limit_seconds=q.get("timelimit") or None,
                    max_attempts=q.get("attempts") or None,
                )
            )
        return quizzes

    async def cmid_to_quiz_id(self, cmid: str) -> str:
        """
        Convert a Course Module ID (the ?id= value in view.php URLs)
        to the actual Moodle quiz ID using core_course_get_course_module.
        """
        data = await self._call("core_course_get_course_module", cmid=int(cmid))
        cm = data.get("cm", {})
        instance = cm.get("instance")
        if not instance:
            raise MoodleAPIError(
                f"Could not resolve cmid={cmid} to a quiz ID. "
                "The activity may not be a quiz or may be unavailable."
            )
        return str(instance)

    async def get_quiz_by_url(self, quiz_url: str) -> Quiz:
        """
        Extract quiz id from a Moodle URL and fetch quiz metadata.

        Handles both URL formats:
          .../mod/quiz/view.php?id=589604   (id = cmid — course module ID)
          .../mod/quiz/attempt.php?attemptid=99
        """
        # If URL contains attemptid, extract attempt directly
        attempt_id = self._extract_id_from_url(quiz_url, "attemptid")
        if attempt_id:
            raise MoodleAPIError("Pass attempt URL to start_attempt, not get_quiz_by_url.")

        cmid = self._extract_id_from_url(quiz_url, "id")
        if not cmid:
            raise MoodleAPIError(f"Cannot extract id from URL: {quiz_url}")

        quiz_id = await self.cmid_to_quiz_id(cmid)
        return await self._get_quiz_by_id(quiz_id)

    async def _get_quiz_by_id(self, quiz_id: str) -> Quiz:
        """Fetch a quiz by its actual quiz ID."""
        data = await self._call("mod_quiz_get_quizzes_by_courses")
        for q in data.get("quizzes", []):
            if str(q["id"]) == str(quiz_id):
                return Quiz(
                    quiz_id=str(q["id"]),
                    course_id=str(q.get("course", "")),
                    cmid=str(q.get("coursemodule") or q.get("cmid") or "") or None,
                    name=self._strip_html(q.get("name", "")),
                    description=self._strip_html(q.get("intro", "")),
                )
        raise MoodleAPIError(f"Quiz {quiz_id} not found via API.")

    # ------------------------------------------------------------------
    # Attempt management
    # ------------------------------------------------------------------

    async def list_unfinished_attempts(self, quiz_id: str) -> list[dict]:
        """List unfinished attempts for current user for a given quiz_id."""
        data = await self._call(
            "mod_quiz_get_user_attempts",
            quizid=int(quiz_id),
            status="unfinished",
        )
        return list(data.get("attempts", []) or [])

    async def start_attempt(self, quiz_id: str) -> str:
        """Start a new attempt or resume an existing in-progress attempt."""
        try:
            data = await self._call("mod_quiz_start_attempt", quizid=int(quiz_id))
            attempt = data.get("attempt", {})
            attempt_id = str(attempt.get("id", ""))
            if not attempt_id:
                raise MoodleAPIError("Failed to start quiz attempt.")
            return attempt_id
        except MoodleAPIError as exc:
            if "still in progress" in str(exc).lower() or "in progress" in str(exc).lower():
                return await self._resume_existing_attempt(quiz_id)
            raise

    async def _resume_existing_attempt(self, quiz_id: str) -> str:
        """Find and return the ID of the current in-progress attempt."""
        # status="unfinished" covers inprogress and overdue states
        data = await self._call(
            "mod_quiz_get_user_attempts",
            quizid=int(quiz_id),
            status="unfinished",
        )
        attempts = data.get("attempts", [])
        if not attempts:
            # Fallback: fetch all and filter manually
            data_all = await self._call(
                "mod_quiz_get_user_attempts",
                quizid=int(quiz_id),
                status="all",
            )
            attempts = [
                a for a in data_all.get("attempts", [])
                if a.get("state") in ("inprogress", "overdue")
            ]
        if not attempts:
            raise MoodleAPIError(
                f"Quiz {quiz_id} has an attempt in progress but could not retrieve it."
            )
        attempt_id = str(attempts[-1]["id"])
        return attempt_id

    async def get_attempt_data(
        self, attempt_id: str, page: int = 0
    ) -> list[Question]:
        """
        Fetch questions for a single page of the attempt.
        page=0 returns all questions (works when quiz is not paginated).
        """
        data = await self._call(
            "mod_quiz_get_attempt_data",
            attemptid=int(attempt_id),
            page=page,
        )
        questions: list[Question] = []
        for q_data in data.get("questions", []):
            question = self._parse_question(q_data)
            if question:
                questions.append(question)
        return questions

    async def get_all_questions(self, attempt_id: str) -> list[Question]:
        """Fetch all questions across all pages (one question per page in paginated quizzes)."""
        all_questions: list[Question] = []
        seen_slots: set[int] = set()
        page = 0
        while True:
            try:
                page_questions = await self.get_attempt_data(attempt_id, page)
            except MoodleAPIError as exc:
                if "invalid page" in str(exc).lower() or "page number" in str(exc).lower():
                    break
                raise
            if not page_questions:
                break
            new_questions = [q for q in page_questions if q.slot not in seen_slots]
            if not new_questions:
                break
            for q in new_questions:
                seen_slots.add(q.slot)
            all_questions.extend(new_questions)
            page += 1
        return all_questions

    async def _get_raw_questions(self, attempt_id: str) -> list[dict]:
        """
        Fetch raw question data dicts for all pages.
        Returns list of q_data dicts including questionsummary field.
        Used by submit_answer_capture for post-submit verification.
        """
        raw_qs: list[dict] = []
        seen_slots: set[int] = set()
        page = 0
        while True:
            try:
                data = await self._call(
                    "mod_quiz_get_attempt_data",
                    attemptid=int(attempt_id),
                    page=page,
                )
            except MoodleAPIError as exc:
                if "invalid page" in str(exc).lower() or "page number" in str(exc).lower():
                    break
                raise
            for q_data in data.get("questions", []):
                slot = int(q_data.get("slot", 0))
                if slot not in seen_slots:
                    seen_slots.add(slot)
                    raw_qs.append(q_data)
            page += 1
        return raw_qs

    async def save_answer(
        self,
        attempt_id: str,
        question: "Question",
        answer: str | list[str] | dict[str, str],
    ) -> None:
        """Submit a single answer during an attempt (saves, does not finish)."""
        data = self._build_answer_data(question, answer)
        import logging, json
        logger = logging.getLogger("moodle.api")
        logger.debug(
            f"[save_answer] attempt={attempt_id} qid={question.question_id} "
            f"type={question.question_type} data={json.dumps(data, ensure_ascii=False)}"
        )
        try:
            result = await self._call(
                "mod_quiz_save_attempt",
                attemptid=int(attempt_id),
                **data,
            )
            logger.debug(
                f"[save_answer] response qid={question.question_id}: {json.dumps(result, ensure_ascii=False)[:500]}"
            )
        except MoodleAPIError as exc:
            msg = str(exc).lower()
            if "already been finished" in msg or "already finished" in msg:
                raise MoodleAttemptFinishedError(
                    f"Attempt {attempt_id} is already finished."
                ) from exc
            raise

    async def finish_attempt(self, attempt_id: str) -> dict:
        """Submit and finish the quiz attempt."""
        result = await self._call(
            "mod_quiz_process_attempt",
            attemptid=int(attempt_id),
            finishattempt=1,
        )
        return result

    # ------------------------------------------------------------------
    # Answer payload construction
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Text normalisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _norm(text: str) -> str:
        """Normalise text for fuzzy comparison: straight quotes, strip, lowercase."""
        text = text.replace('\u2018', "'").replace('\u2019', "'")   # curly single quotes
        text = text.replace('\u201c', '"').replace('\u201d', '"')   # curly double quotes
        text = text.replace('\u2013', '-').replace('\u2014', '-')   # en/em dash
        # Strip diacritics (Polish letters, etc.) so comparisons tolerate accents.
        text = unicodedata.normalize("NFKD", text)
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        return text.strip().casefold()

    @classmethod
    def _match_text(cls, candidate: str, target: str) -> bool:
        """Return True if candidate matches target after normalisation."""
        n_cand = cls._norm(candidate)
        n_targ = cls._norm(target)
        if n_cand == n_targ:
            return True
        # Also try stripping trailing punctuation (comma, period, etc.)
        if n_cand.rstrip('.,;:!?') == n_targ.rstrip('.,;:!?'):
            return True
        return False

    @classmethod
    def _fuzzy_lookup(cls, d: dict, key: str) -> str:
        """Look up *key* in dict *d* with normalisation fallback. Returns '' if not found."""
        if key in d:
            return d[key]
        n_key = cls._norm(key)
        n_key_strip = n_key.rstrip('.,;:!?')
        for k, v in d.items():
            nk = cls._norm(k)
            if nk == n_key or nk.rstrip('.,;:!?') == n_key_strip:
                return v
        return ""

    @classmethod
    def _find_choice_value(
        cls,
        choices: list[str],
        choice_values: list[str],
        chosen: str,
        *,
        fuzzy: bool = False,
    ) -> str:
        """
        Return the numeric choice_value matching chosen text.

        Primary: exact match after normalisation.
        Fallback: fuzzy match for minor typos (e.g. "False" vs "Fasle") and punctuation.
        Returns '0' if not found.
        """
        chosen = (chosen or "").strip()
        if not chosen:
            return "0"

        # Exact match first
        for choice_text, choice_val in zip(choices, choice_values):
            if cls._match_text(chosen, choice_text):
                return choice_val

        # Fuzzy fallback (typos / spacing).
        # NOTE: caller may pass fuzzy=False, but the solver enables fuzzy matching by default.
        if not fuzzy:
            return "0"
        # Dynamic threshold: short tokens are more sensitive to transpositions ("false" vs "fasle").
        n_chosen = cls._norm(chosen).rstrip('.,;:!?')
        if not n_chosen:
            return "0"
        min_score = 0.80
        if len(n_chosen) <= 5:
            min_score = 0.75

        best_val = "0"
        best_score = 0.0
        for choice_text, choice_val in zip(choices, choice_values):
            n_choice = cls._norm(choice_text).rstrip('.,;:!?')
            if not n_choice:
                continue
            score = difflib.SequenceMatcher(None, n_chosen, n_choice).ratio()
            if score > best_score:
                best_score = score
                best_val = choice_val

        # Accept only if sufficiently close (prevents random wrong picks).
        if best_score >= min_score:
            return best_val
        return "0"

    @staticmethod
    def _detect_cloze_option_case(html: str) -> str:
        """
        Detect whether Cloze options in the question use uppercase (A/B/C) or
        lowercase (a/b/c) letters. Used to normalize single-letter answers.

        Returns 'upper' if options are uppercase, 'lower' if lowercase,
        'unknown' if unclear.
        """
        import re
        letters = re.findall(r'<strong>([A-Z])</strong>', html)
        if letters:
            return "upper"
        lower_strong = re.findall(r'<strong>([a-z])</strong>', html)
        if lower_strong:
            return "lower"
        plain_upper = re.findall(r'>([A-Z])\s+</span>', html)
        if plain_upper:
            return "upper"
        plain_lower = re.findall(r'>([a-z])\s+</span>', html)
        if plain_lower:
            return "lower"
        lower_para = re.findall(r'<span[^>]*>\s*([a-g])\)', html)
        if lower_para:
            return "lower"
        upper_para = re.findall(r'<span[^>]*>\s*([A-G])\)', html)
        if upper_para:
            return "upper"
        write_letter = re.findall(r'write\s+[A-G]\s+in\s+the\s+gaps', html, re.IGNORECASE)
        if write_letter:
            return "upper"
        opts_section = re.findall(r'write one letter \(([A-Za-z])', html)
        if opts_section:
            return opts_section[0].casefold()
        return "unknown"

    def _build_answer_data(
        self,
        question: "Question",
        answer: str | list[str] | dict[str, str],
    ) -> dict:
        """
        Build the form data dict that Moodle expects for a given question type.
        Always includes the sequencecheck token when available.
        """
        data: dict = {}
        idx = 0

        # Always include sequencecheck — Moodle rejects submissions without it
        if question.sequencecheck_name and question.sequencecheck_value is not None:
            data[f"data[{idx}][name]"] = question.sequencecheck_name
            data[f"data[{idx}][value]"] = question.sequencecheck_value
            idx += 1

        prefix = f"q{question.question_id}:"

        if question.question_type in (QuestionType.MCQ, QuestionType.TRUE_FALSE):
            chosen = answer if isinstance(answer, str) else (answer[0] if answer else "")
            for opt in question.options:
                if opt.key.upper() == chosen.upper() or self._match_text(opt.label, chosen):
                    data[f"data[{idx}][name]"] = opt.input_name
                    data[f"data[{idx}][value]"] = opt.input_value
                    return data
            data[f"data[{idx}][name]"] = f"{prefix}answer"
            data[f"data[{idx}][value]"] = chosen
            return data

        elif question.question_type == QuestionType.MULTI_SELECT:
            keys = answer if isinstance(answer, list) else [answer]
            for opt in question.options:
                if opt.key.upper() in [k.upper() for k in keys]:
                    data[f"data[{idx}][name]"] = opt.input_name
                    data[f"data[{idx}][value]"] = opt.input_value
                    idx += 1
            return data

        elif question.question_type in (QuestionType.SHORT_ANSWER, QuestionType.ESSAY):
            text = answer if isinstance(answer, str) else str(answer)
            input_name = question.text_input_name or f"{prefix}answer"
            data[f"data[{idx}][name]"] = input_name
            data[f"data[{idx}][value]"] = text
            return data

        elif question.question_type == QuestionType.MATCHING:
            pairs_answer: dict = answer if isinstance(answer, dict) else {}
            for pair in question.match_pairs:
                # Fuzzy stem lookup tolerates apostrophe variants and trailing commas
                chosen_text = self._fuzzy_lookup(pairs_answer, pair.stem)
                if not chosen_text:
                    continue
                numeric_val = self._find_choice_value(
                    pair.choices,
                    pair.choice_values,
                    chosen_text,
                    fuzzy=self.fuzzy_choice_match,
                )
                data[f"data[{idx}][name]"] = pair.select_name
                data[f"data[{idx}][value]"] = numeric_val
                idx += 1
            return data

        elif question.question_type == QuestionType.GAP_SELECT:
            gap_answers: dict = {}
            if isinstance(answer, dict):
                gap_answers = answer
            elif isinstance(answer, list):
                gap_answers = {str(i + 1): v for i, v in enumerate(answer)}
            for i, gap in enumerate(question.gap_items):
                chosen_text = gap_answers.get(str(i + 1), "")
                if not chosen_text:
                    continue
                numeric_val = self._find_choice_value(
                    gap.choices,
                    gap.choice_values,
                    chosen_text,
                    fuzzy=self.fuzzy_choice_match,
                )
                data[f"data[{idx}][name]"] = gap.input_name
                data[f"data[{idx}][value]"] = numeric_val
                idx += 1
            return data

        elif question.question_type == QuestionType.DRAG_DROP:
            drag_answers: dict = answer if isinstance(answer, dict) else {}
            # Word list is stored in choices of every gap item (all identical)
            drag_words = question.gap_items[0].choices if question.gap_items else []
            for i, gap in enumerate(question.gap_items):
                val = drag_answers.get(str(i + 1)) or drag_answers.get(gap.input_name, "")
                if val == "" or val is None:
                    val = "0"
                # If the AI gave a word string instead of an index, convert it
                if val and not str(val).isdigit():
                    word_text = str(val)
                    matched_idx = "0"
                    for j, word in enumerate(drag_words, 1):
                        if self._match_text(word_text, word):
                            matched_idx = str(j)
                            break
                    if matched_idx == "0":
                        # Partial / substring fallback
                        for j, word in enumerate(drag_words, 1):
                            n_word = self._norm(word)
                            n_val = self._norm(word_text)
                            if n_val in n_word or n_word in n_val:
                                matched_idx = str(j)
                                break
                    val = matched_idx
                data[f"data[{idx}][name]"] = gap.input_name
                data[f"data[{idx}][value]"] = str(val)
                idx += 1
            return data

        elif question.question_type == QuestionType.CLOZE:
            cloze_answers: dict = {}
            if isinstance(answer, dict):
                cloze_answers = answer
            elif isinstance(answer, list):
                cloze_answers = {str(i + 1): v for i, v in enumerate(answer)}
            for i, gap in enumerate(question.gap_items):
                val = cloze_answers.get(str(i + 1), "")
                if not val:
                    val = ""
                val = str(val).strip()
                if gap.is_text and len(val) == 1 and val.isalpha() and question.raw_html:
                    case = self._detect_cloze_option_case(question.raw_html)
                    if case == "upper":
                        val = val.upper()
                    elif case == "lower":
                        val = val.lower()
                if not gap.is_text and gap.choices:
                    numeric_val = self._find_choice_value(
                        gap.choices, gap.choice_values, val
                    )
                    val = numeric_val
                data[f"data[{idx}][name]"] = gap.input_name
                data[f"data[{idx}][value]"] = val
                idx += 1
            return data

        # UNKNOWN — return just the sequencecheck so the question is "touched"
        return data

    # ------------------------------------------------------------------
    # Question parsing
    # ------------------------------------------------------------------

    def _parse_question(self, q_data: dict) -> Optional[Question]:
        """Parse a raw API question dict into a Question dataclass."""
        q_type = self._detect_type(q_data.get("type", ""))
        html = q_data.get("html", "")
        # Use full HTML as primary text source — preserves tables, lists, all context.
        # questionsummary is often truncated; only fall back to it when HTML is absent.
        text = self._strip_html(html or q_data.get("questionsummary", ""))
        annotated = self._annotated_text(html) if html else text
        question_id = str(q_data.get("number") or q_data.get("id") or "0")
        slot = int(q_data.get("slot", 0))

        question = Question(
            question_id=question_id,
            slot=slot,
            question_type=q_type,
            text=text,
            raw_html=html,
            annotated_text=annotated,
        )

        # Parse answer options from the HTML embedded in the API response
        if html and q_type in (QuestionType.MCQ, QuestionType.MULTI_SELECT,
                                QuestionType.TRUE_FALSE):
            question.options = self._parse_options_from_html(html)

        if html and q_type == QuestionType.MATCHING:
            question.match_pairs = self._parse_match_pairs_from_html(html)

        if html and q_type in (QuestionType.SHORT_ANSWER, QuestionType.ESSAY):
            question.text_input_name = self._find_text_input_name(html)

        if html and q_type == QuestionType.GAP_SELECT:
            question.gap_items = self._parse_gapselect_from_html(html)

        if html and q_type == QuestionType.DRAG_DROP:
            question.gap_items = self._parse_ddwtos_from_html(html)

        if html and q_type == QuestionType.CLOZE:
            question.gap_items = self._parse_cloze_from_html(html)

        if html:
            sc_name, sc_value = self._find_sequencecheck(html)
            question.sequencecheck_name = sc_name
            question.sequencecheck_value = sc_value

        return question

    @staticmethod
    def _detect_type(type_str: str) -> QuestionType:
        mapping = {
            "multichoice": QuestionType.MCQ,
            "truefalse": QuestionType.TRUE_FALSE,
            "shortanswer": QuestionType.SHORT_ANSWER,
            "essay": QuestionType.ESSAY,
            "match": QuestionType.MATCHING,
            "matching": QuestionType.MATCHING,
            "gapselect": QuestionType.GAP_SELECT,
            "ddwtos": QuestionType.DRAG_DROP,
            "multianswer": QuestionType.CLOZE,
        }
        return mapping.get(type_str.lower(), QuestionType.UNKNOWN)

    @staticmethod
    def _parse_options_from_html(html: str) -> list[AnswerOption]:
        soup = BeautifulSoup(html, "html.parser")
        options: list[AnswerOption] = []
        labels = ["a", "b", "c", "d", "e", "f", "g", "h"]

        def clean_label(text: str) -> str:
            """
            Moodle injects UI strings like "Clear my choice" / "Flag question" into
            the answer area. Strip them so option labels contain only real content.
            """
            t = (text or "").strip()
            if not t:
                return ""
            # Common UI strings (case-insensitive)
            t = re.sub(r"\bClear my choice\b", "", t, flags=re.IGNORECASE)
            t = re.sub(r"\bFlag question\b", "", t, flags=re.IGNORECASE)
            # Collapse whitespace after removals
            t = re.sub(r"\s{2,}", " ", t).strip()
            return t

        # Restrict to the answer container to avoid picking up unrelated inputs
        # (navigation, hidden tokens, theme UI, etc.) which can shift option ordering.
        container = soup.find("div", class_="answer") or soup

        inputs = container.find_all("input", {"type": ["radio", "checkbox"]})
        for i, inp in enumerate(inputs):
            name = inp.get("name", "")
            value = inp.get("value", "")
            if not name:
                continue
            # Prefer explicit <label for="{id}"> association
            input_id = inp.get("id", "")
            label_tag = container.find("label", {"for": input_id}) if input_id else None
            if not label_tag:
                # Fallbacks: sibling/parent label (depends on Moodle theme)
                label_tag = inp.find_next_sibling("label") or inp.find_parent("label")

            label_text = clean_label(label_tag.get_text(separator=" ", strip=True) if label_tag else "")

            # Last resort: use the closest "row" container text, excluding UI strings.
            if not label_text or label_text.lower() in ("choose...", "choose…"):
                row = inp.find_parent(
                    ["div", "li"],
                    class_=lambda c: c
                    and (
                        (isinstance(c, str) and ("r0" in c or "r1" in c))
                        or (isinstance(c, list) and any(("r0" in x or "r1" in x) for x in c))
                    ),
                )
                if row:
                    label_text = clean_label(row.get_text(separator=" ", strip=True))

            if not label_text:
                label_text = clean_label(value) or value

            key = labels[i] if i < len(labels) else str(i + 1)
            options.append(
                AnswerOption(
                    key=key,
                    label=label_text,
                    input_name=name,
                    input_value=value,
                )
            )
        return options

    @staticmethod
    @staticmethod
    def _parse_match_pairs_from_html(html: str) -> list[MatchPair]:
        soup = BeautifulSoup(html, "html.parser")
        pairs: list[MatchPair] = []

        rows = soup.find_all("tr", class_="r0") + soup.find_all("tr", class_="r1")
        for row in rows:
            stem_cell = row.find("td", class_="text")
            select_tag = row.find("select")
            if not stem_cell or not select_tag:
                continue
            stem = stem_cell.get_text(strip=True)
            select_name = select_tag.get("name", "")
            choices = []
            choice_values = []
            for opt in select_tag.find_all("option"):
                val = opt.get("value", "")
                if val and val != "0":  # skip blank placeholder (value='0')
                    choices.append(opt.get_text(strip=True))
                    choice_values.append(val)
            pairs.append(MatchPair(
                stem=stem,
                select_name=select_name,
                choices=choices,
                choice_values=choice_values,
            ))
        return pairs

    @staticmethod
    def _parse_gapselect_from_html(html: str) -> list[GapItem]:
        """Parse gap-select (select missing words) blanks from HTML."""
        soup = BeautifulSoup(html, "html.parser")
        items = []
        for sel in soup.find_all("select"):
            name = sel.get("name", "")
            choices = []
            choice_values = []
            for opt in sel.find_all("option"):
                val = opt.get("value", "")
                if val and val != "0":
                    choices.append(opt.get_text(strip=True))
                    choice_values.append(val)
            items.append(GapItem(input_name=name, choices=choices, choice_values=choice_values))
        return items

    @staticmethod
    def _parse_ddwtos_from_html(html: str) -> list[GapItem]:
        """Parse drag-and-drop onto text: extract blank slots AND the word list."""
        soup = BeautifulSoup(html, "html.parser")

        # Extract draggable words — Moodle renders them inside spans/li with class
        # containing 'draghome' or 'draggable'. Preserve duplicates: if a word
        # appears twice in the pool it means there are two separate items with
        # that text (one may be the intentional "extra" unused word).
        drag_words: list[str] = []
        for tag in soup.find_all(['span', 'li', 'div']):
            css = ' '.join(tag.get('class', []))
            if 'draghome' in css or 'draggable' in css:
                word = tag.get_text(strip=True)
                if word:
                    drag_words.append(word)

        # Fallback: words listed inside a container div with class 'dragitems'
        if not drag_words:
            for container in soup.find_all('div', class_=lambda c: c and 'dragitems' in c):
                for li in container.find_all('li'):
                    word = li.get_text(strip=True)
                    if word:
                        drag_words.append(word)

        # Extract blank positions (hidden inputs ending _p1, _p2, ...)
        items = []
        for inp in soup.find_all("input", {"type": "hidden"}):
            name = inp.get("name", "")
            if re.search(r"_p\d+$", name):
                # Every blank gets the full word list so prompts can show choices
                items.append(GapItem(input_name=name, is_text=False, choices=list(drag_words)))

        return items

    @staticmethod
    def _parse_cloze_from_html(html: str) -> list[GapItem]:
        """
        Parse cloze (multianswer) sub-answer inputs.
        Also handles `<select>` sub-questions (MULTICHOICE embedded type).
        Detects labeled options from three possible layouts:
        1. Table: rows with [blank_num] | A | word | B | word | ...
        2. Inline span: "12   A   conservative   B   concerned   C   convinced   D   contained"
        3. Inline paragraphs: "(a) disuse  (b) misuse  (c) overuse  (d) reuse"
        """
        soup = BeautifulSoup(html, "html.parser")
        items: list[GapItem] = []

        # Collect all sub-answer inputs in document order (text inputs + selects)
        for tag in soup.find_all(["input", "select"]):
            tag_type = tag.get("type", "")
            name = tag.get("name", "")
            if not name:
                continue

            if tag.name == "select":
                choices, choice_values = [], []
                for opt in tag.find_all("option"):
                    val = opt.get("value", "")
                    text = opt.get_text(strip=True)
                    if text and val != "":
                        choices.append(text)
                        choice_values.append(val)
                items.append(GapItem(
                    input_name=name,
                    choices=choices,
                    choice_values=choice_values,
                    is_text=False,
                ))
            elif tag_type == "text":
                items.append(GapItem(input_name=name, is_text=True))

        # Extract labeled options from annotated text (same text used in AI prompts)
        annotated = MoodleAPI._annotated_text(html)

        def _parse_row(cells: list[str], blank_num: int) -> dict[str, str]:
            """Parse letter-word pairs from cells, return dict of options."""
            opts: dict[str, str] = {}
            pending_letter: Optional[str] = None
            for cell in cells[1:]:
                if re.fullmatch(r"[A-Ha-h]", cell):
                    pending_letter = cell.upper()
                elif pending_letter and len(cell) <= 25 and re.match(r"^[A-Za-z][A-Za-z'/-]*$", cell):
                    if pending_letter not in opts:
                        opts[pending_letter] = cell
                    pending_letter = None
            return opts

        # Source 1: tables
        blank_options: dict[int, dict[str, str]] = {}
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                if cells and re.fullmatch(r"\d+", cells[0]):
                    bn = int(cells[0])
                    blank_options[bn] = _parse_row(cells, bn)

        # Source 2: inline spans (e.g. row 12 outside the table)
        for span in soup.find_all("span"):
            text = span.get_text(separator=" ", strip=True).replace('\xa0', ' ')
            if re.fullmatch(r"\d+\s+[A-Ha-h]\s+[A-Za-z]", text.strip()):
                cells = re.split(r"\s{2,}", text.strip())
                if cells and re.fullmatch(r"\d+", cells[0]):
                    bn = int(cells[0])
                    blank_options[bn] = _parse_row(cells, bn)

        # Source 3: inline "(a) word (b) word (c) word (d) word" paragraphs in annotated text.
        # Extract per-blank option blocks: look for Answer N marker + options that follow.
        # Pattern: "Answer N ... (a) word (b) word (c) word (d) word"
        # Use lettered option pattern with single space: (a) word
        for m in re.finditer(r'Answer\s+(\d+)\s+Question', annotated):
            blank_num = int(m.group(1))
            # Find the options block that follows this Answer marker (up to next Answer or 500 chars)
            start = m.end()
            end = min(start + 500, len(annotated))
            block = annotated[start:end]
            # Extract "(a) word" style pairs
            letter_word: dict[str, str] = {}
            for opt_m in re.finditer(r'\b([a-d])\)\s*([A-Za-z][A-Za-z \'/-]{0,30}?)(?=\s*\([a-d]\)|$)', block):
                letter = opt_m.group(1).upper()
                word = opt_m.group(2).strip()
                if letter not in letter_word:
                    letter_word[letter] = word
            if len(letter_word) >= 2:
                blank_options[blank_num] = letter_word

        # Source 4: scan entire annotated text for options embedded in passage text
        # (not in per-blank blocks). Handles three distinct layouts:
        #   - q002: "<strong>A </strong>Factory canteen" (uppercase A-H, multi-word labels)
        #   - q011: "b Everybody who..." (lowercase a-h, first-word labels)
        #   - q018: "a) In the past..." (lowercase a-g, first-word labels)
        # Only apply if no per-blank options were found (blank_options is still empty).
        if not blank_options:
            global_opts: dict[str, str] = {}

            # Pattern 1: uppercase A-H followed by whitespace and a single word (q002 style)
            # e.g., "A Factory" → A → "Factory" (only first word, no multi-line greediness)
            for m in re.finditer(r'\b([A-H])\s+([A-Z][A-Za-z]+)', annotated):
                letter = m.group(1)
                label = m.group(2)
                if len(label) <= 15 and letter not in global_opts:
                    global_opts[letter] = label

            # Pattern 2: lowercase a-h followed by space and word (q011 style)
            # e.g., "b Everybody who..." → b → "Everybody"
            # Also handles "'d", "n't" with [A-Z][a-z']* (apostrophe allowed)
            for m in re.finditer(r"\b([a-h])\s+([A-Z][a-z']*)\b", annotated):
                letter = m.group(1).upper()
                label = m.group(2)
                if len(label) <= 20 and letter not in global_opts:
                    global_opts[letter] = label
            # Handle "h 70%" style (number starting options)
            for m in re.finditer(r'\b([a-h])\s+(\d+%)', annotated):
                letter = m.group(1).upper()
                label = m.group(2)
                if letter not in global_opts:
                    global_opts[letter] = label

            # Pattern 3: lowercase a-g followed by ")" (q018 style)
            # e.g., "a) In the past..." → a → "In"
            # Handles ellipsis-prefixed options: "d) …more and more..." → d → "More"
            # Also handles "(a) word" inline format; allow apostrophes
            # Use a custom function to find the first viable label word in the window
            # after ")", handling non-letter prefixes (ellipsis, quotes).
            skip_words_p3 = {'to', 'from', 'a'}

            def _find_label_word(text: str) -> Optional[str]:
                """Find first word that can serve as a label in text."""
                words = text.split()
                first_tok = (words[0] or '').lower() if words else ''
                # Only skip if first token is exactly a short question-text word (lowercase only)
                if first_tok in skip_words_p3:
                    return None
                for i, c in enumerate(text):
                    if c.isupper() and i + 1 < len(text) and text[i+1].islower():
                        end = i + 1
                        while end < len(text) and text[end].isalpha():
                            end += 1
                        return text[i:end]
                for w in words:
                    wm = re.search(r"([A-Za-z]+)", w)
                    if wm:
                        return wm.group(1).capitalize()
                return None

            seen_letters_p3: set[str] = set()
            for m in re.finditer(r"\b([a-g])\)", annotated):
                letter = m.group(1).upper()
                if letter in seen_letters_p3:
                    continue
                window = annotated[m.end():m.end()+25]
                word = _find_label_word(window)
                if word and letter not in global_opts:
                    global_opts[letter] = word
                    seen_letters_p3.add(letter)

            if len(global_opts) >= 2:
                for idx in range(len(items)):
                    items[idx].labeled_options = global_opts

        # Assign labeled_options to items
        for idx, item in enumerate(items):
            num = idx + 1
            if num in blank_options:
                item.labeled_options = blank_options[num]

        return items

    @staticmethod
    def _find_sequencecheck(html: str) -> tuple[Optional[str], Optional[str]]:
        """Extract the sequencecheck hidden input name and value."""
        soup = BeautifulSoup(html, "html.parser")
        for inp in soup.find_all("input", {"type": "hidden"}):
            name = inp.get("name", "")
            if name.endswith(":sequencecheck"):
                return name, inp.get("value", "1")
        return None, None

    @staticmethod
    def _find_text_input_name(html: str) -> Optional[str]:
        soup = BeautifulSoup(html, "html.parser")
        textarea = soup.find("textarea")
        if textarea:
            return textarea.get("name")
        text_input = soup.find("input", {"type": "text"})
        if text_input:
            return text_input.get("name")
        return None

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_id_from_url(url: str, param: str = "id") -> Optional[str]:
        match = re.search(rf"[?&]{param}=(\d+)", url)
        return match.group(1) if match else None
