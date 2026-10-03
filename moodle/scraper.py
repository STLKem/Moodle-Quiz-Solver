"""
Playwright-based Moodle scraper.

Used as a fallback when the REST API is unavailable. Opens a headless
Chromium browser, logs in as a normal student, navigates to the quiz,
and fills answers programmatically.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser, Page, Playwright

from .models import (
    AnswerOption,
    CourseInfo,
    MatchPair,
    Question,
    QuestionType,
    Quiz,
)


class MoodleScraperError(Exception):
    """Raised on unrecoverable scraper errors."""


class MoodleScraper:
    """Headless browser automation for Moodle quiz navigation."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        headless: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.headless = headless

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None

        self._attempt_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "MoodleScraper":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        context = await self._browser.new_context()
        self._page = await context.new_page()
        await self.login()
        return self

    async def __aexit__(self, *_) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def login(self) -> None:
        """Log in via the standard Moodle login form."""
        login_url = urljoin(self.base_url, "/login/index.php")
        await self._page.goto(login_url, wait_until="domcontentloaded")

        await self._page.fill("#username", self.username)
        await self._page.fill("#password", self.password)
        await self._page.click("#loginbtn")
        await self._page.wait_for_load_state("domcontentloaded")

        # Verify login succeeded
        if "/login/index.php" in self._page.url:
            raise MoodleScraperError(
                "Login failed — check username/password and Moodle URL."
            )

    # ------------------------------------------------------------------
    # Course / quiz discovery
    # ------------------------------------------------------------------

    async def get_enrolled_courses(self) -> list[CourseInfo]:
        """Return courses the user is enrolled in (parsed from dashboard)."""
        await self._page.goto(
            urljoin(self.base_url, "/my/"), wait_until="domcontentloaded"
        )
        html = await self._page.content()
        soup = BeautifulSoup(html, "html.parser")

        courses: list[CourseInfo] = []
        for link in soup.select("a[href*='/course/view.php?id=']"):
            href = link.get("href", "")
            course_id = self._extract_param(href, "id")
            if not course_id:
                continue
            name = link.get_text(strip=True)
            if name and course_id not in [c.course_id for c in courses]:
                courses.append(
                    CourseInfo(
                        course_id=course_id,
                        full_name=name,
                        short_name=name[:20],
                    )
                )
        return courses

    async def get_quizzes_by_course(self, course_id: str) -> list[Quiz]:
        """Return all quizzes in a course by scraping the course page."""
        course_url = urljoin(self.base_url, f"/course/view.php?id={course_id}")
        await self._page.goto(course_url, wait_until="domcontentloaded")
        html = await self._page.content()
        soup = BeautifulSoup(html, "html.parser")

        quizzes: list[Quiz] = []
        for link in soup.select("a[href*='/mod/quiz/view.php?id=']"):
            href = link.get("href", "")
            quiz_id = self._extract_param(href, "id")
            if not quiz_id:
                continue
            name = link.get_text(strip=True)
            quizzes.append(
                Quiz(
                    quiz_id=quiz_id,
                    course_id=course_id,
                    cmid=quiz_id,
                    name=name,
                    description="",
                )
            )
        return quizzes

    # ------------------------------------------------------------------
    # Attempt management
    # ------------------------------------------------------------------

    async def start_attempt(self, quiz_id_or_url: str) -> str:
        """
        Navigate to the quiz view page and click "Start attempt" or
        "Continue the last attempt". Returns the attempt_id.
        """
        if quiz_id_or_url.startswith("http"):
            view_url = quiz_id_or_url
        else:
            view_url = urljoin(
                self.base_url, f"/mod/quiz/view.php?id={quiz_id_or_url}"
            )

        await self._page.goto(view_url, wait_until="domcontentloaded")

        # Click the attempt button (works for both start and resume)
        btn = (
            self._page.locator("button:has-text('Attempt quiz')")
            .or_(self._page.locator("button:has-text('Continue the last attempt')"))
            .or_(self._page.locator("input[value*='attempt']"))
            .first
        )
        await btn.click()
        await self._page.wait_for_load_state("domcontentloaded")

        # Confirm the start-attempt popup if present
        confirm = self._page.locator("button:has-text('Start attempt')").first
        if await confirm.count() > 0:
            await confirm.click()
            await self._page.wait_for_load_state("domcontentloaded")

        attempt_id = self._extract_param(self._page.url, "attempt")
        if not attempt_id:
            raise MoodleScraperError(
                f"Could not determine attempt ID from URL: {self._page.url}"
            )

        self._attempt_id = attempt_id
        return attempt_id

    async def get_all_questions(self) -> list[Question]:
        """
        Fetch all questions from the current attempt.
        Handles multi-page quizzes by iterating through all pages.
        """
        all_questions: list[Question] = []
        page_num = 0

        while True:
            page_questions = await self._parse_current_page()
            if not page_questions:
                break
            all_questions.extend(page_questions)

            # Check if there is a "Next page" button
            next_btn = self._page.locator(
                "input[name='next'], button:has-text('Next page')"
            ).first
            if await next_btn.count() == 0:
                break

            await next_btn.click()
            await self._page.wait_for_load_state("domcontentloaded")
            page_num += 1

        return all_questions

    # ------------------------------------------------------------------
    # Answer submission
    # ------------------------------------------------------------------

    async def fill_and_submit_answer(
        self,
        question: Question,
        answer: str | list[str] | dict[str, str],
    ) -> None:
        """
        Fill in the answer for one question on the current page.
        Does NOT navigate or submit the whole quiz.
        """
        if question.question_type in (QuestionType.MCQ, QuestionType.TRUE_FALSE):
            await self._fill_mcq(question, answer if isinstance(answer, str) else answer[0])

        elif question.question_type == QuestionType.MULTI_SELECT:
            keys = answer if isinstance(answer, list) else [answer]
            await self._fill_multi_select(question, keys)

        elif question.question_type in (QuestionType.SHORT_ANSWER, QuestionType.ESSAY):
            text = answer if isinstance(answer, str) else str(answer)
            await self._fill_text(question, text)

        elif question.question_type == QuestionType.MATCHING:
            pairs = answer if isinstance(answer, dict) else {}
            await self._fill_matching(question, pairs)

        elif question.question_type == QuestionType.CLOZE:
            # CLOZE (multianswer) has multiple text inputs — one per gap item
            # answer is a dict {gap_num: value} or {input_name: value}
            import re
            def _detect_case(html: str) -> str:
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
            def _norm_cloze(val: str) -> str:
                v = str(val).strip()
                if len(v) == 1 and v.isalpha() and question.raw_html:
                    case = _detect_case(question.raw_html)
                    if case == "upper":
                        v = v.upper()
                    elif case == "lower":
                        v = v.lower()
                return v
            if isinstance(answer, dict):
                for gap in question.gap_items:
                    val = answer.get(str(gap.input_name), answer.get(str(question.gap_items.index(gap) + 1), ""))
                    val = _norm_cloze(str(val)) if val else ""
                    locator = self._page.locator(f"input[name='{gap.input_name}']").first
                    await locator.fill(val)
            elif isinstance(answer, str):
                locator = self._page.locator(f"input[name='{question.gap_items[0].input_name}']").first
                await locator.fill(_norm_cloze(answer))

    async def finish_attempt(self) -> None:
        """Click the 'Finish attempt' button and confirm submission."""
        finish_btn = (
            self._page.locator("input[value*='Finish attempt']")
            .or_(self._page.locator("button:has-text('Finish attempt')"))
            .first
        )
        await finish_btn.click()
        await self._page.wait_for_load_state("domcontentloaded")

        # Confirm summary page submission
        submit_btn = (
            self._page.locator("input[value*='Submit all']")
            .or_(self._page.locator("button:has-text('Submit all')"))
            .first
        )
        if await submit_btn.count() > 0:
            await submit_btn.click()
            await self._page.wait_for_load_state("domcontentloaded")

        # Final confirmation dialog
        confirm_btn = (
            self._page.locator("button:has-text('Submit all and finish')")
            .first
        )
        if await confirm_btn.count() > 0:
            await confirm_btn.click()
            await self._page.wait_for_load_state("domcontentloaded")

    # ------------------------------------------------------------------
    # Page parsing
    # ------------------------------------------------------------------

    async def _parse_current_page(self) -> list[Question]:
        """Parse all question blocks on the currently loaded page."""
        html = await self._page.content()
        soup = BeautifulSoup(html, "html.parser")
        questions: list[Question] = []

        blocks = soup.find_all("div", class_=re.compile(r"\bque\b"))
        for block in blocks:
            question = self._parse_question_block(block)
            if question:
                questions.append(question)

        return questions

    def _parse_question_block(self, block) -> Optional[Question]:
        """Parse a single <div class="que ..."> block."""
        # Determine question type from CSS classes
        classes = block.get("class", [])
        q_type = QuestionType.UNKNOWN
        for cls in classes:
            if cls in ("multichoice",):
                q_type = QuestionType.MCQ
            elif cls in ("truefalse",):
                q_type = QuestionType.TRUE_FALSE
            elif cls in ("shortanswer",):
                q_type = QuestionType.SHORT_ANSWER
            elif cls in ("essay",):
                q_type = QuestionType.ESSAY
            elif cls in ("match", "matching"):
                q_type = QuestionType.MATCHING

        # Question text
        text_div = block.find("div", class_="qtext")
        question_text = text_div.get_text(separator=" ", strip=True) if text_div else ""

        # Slot/number
        info_div = block.find("div", class_="info")
        slot_text = info_div.get_text(strip=True) if info_div else "0"
        slot_match = re.search(r"\d+", slot_text)
        slot = int(slot_match.group()) if slot_match else 0

        question_id = block.get("id", f"q{slot}").replace("q", "")

        question = Question(
            question_id=question_id,
            slot=slot,
            question_type=q_type,
            text=question_text,
            raw_html=str(block),
        )

        answer_div = block.find("div", class_="answer")
        if not answer_div:
            answer_div = block

        if q_type in (QuestionType.MCQ, QuestionType.TRUE_FALSE):
            question.options = self._extract_options(answer_div, "radio")
            # Fallback: checkboxes treated as MCQ if only one expected
            if not question.options:
                question.options = self._extract_options(answer_div, "checkbox")

        elif q_type == QuestionType.MULTI_SELECT:
            question.options = self._extract_options(answer_div, "checkbox")

        elif q_type in (QuestionType.SHORT_ANSWER, QuestionType.ESSAY):
            inp = answer_div.find("input", {"type": "text"})
            if inp:
                question.text_input_name = inp.get("name")
            else:
                ta = answer_div.find("textarea")
                if ta:
                    question.text_input_name = ta.get("name")

        elif q_type == QuestionType.MATCHING:
            question.match_pairs = self._extract_match_pairs(answer_div)

        # Auto-detect multi-select from checkbox presence when type is unknown
        if q_type == QuestionType.UNKNOWN:
            checkboxes = answer_div.find_all("input", {"type": "checkbox"})
            radios = answer_div.find_all("input", {"type": "radio"})
            if checkboxes:
                question.question_type = QuestionType.MULTI_SELECT
                question.options = self._extract_options(answer_div, "checkbox")
            elif radios:
                question.question_type = QuestionType.MCQ
                question.options = self._extract_options(answer_div, "radio")

        return question

    @staticmethod
    def _extract_options(container, input_type: str) -> list[AnswerOption]:
        inputs = container.find_all("input", {"type": input_type})
        options: list[AnswerOption] = []
        labels = ["a", "b", "c", "d", "e", "f", "g", "h"]

        for i, inp in enumerate(inputs):
            name = inp.get("name", "")
            value = inp.get("value", "")
            label_for = inp.get("id", "")
            label_tag = container.find("label", {"for": label_for})
            if not label_tag:
                label_tag = inp.find_next_sibling("label") or inp.find_parent("label")
            label_text = label_tag.get_text(strip=True) if label_tag else value

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
    def _extract_match_pairs(container) -> list[MatchPair]:
        pairs: list[MatchPair] = []
        rows = container.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            stem = cells[0].get_text(strip=True)
            select = cells[1].find("select") if len(cells) > 1 else None
            if not select:
                continue
            select_name = select.get("name", "")
            choices = [
                opt.get_text(strip=True)
                for opt in select.find_all("option")
                if opt.get("value", "")
            ]
            pairs.append(MatchPair(stem=stem, select_name=select_name, choices=choices))
        return pairs

    # ------------------------------------------------------------------
    # Form filling helpers
    # ------------------------------------------------------------------

    async def _fill_mcq(self, question: Question, answer: str) -> None:
        """Click the radio button corresponding to the chosen answer key."""
        for opt in question.options:
            if opt.key.upper() == answer.upper() or opt.label.lower() == answer.lower():
                selector = f"input[name='{opt.input_name}'][value='{opt.input_value}']"
                await self._page.check(selector)
                return
        # Last resort — click by label text
        await self._page.locator(f"label:has-text('{answer}')").first.click()

    async def _fill_multi_select(
        self, question: Question, keys: list[str]
    ) -> None:
        """Check the checkboxes for all chosen answer keys."""
        keys_upper = [k.upper() for k in keys]
        for opt in question.options:
            selector = f"input[name='{opt.input_name}'][value='{opt.input_value}']"
            if opt.key.upper() in keys_upper:
                await self._page.check(selector)
            else:
                await self._page.uncheck(selector)

    async def _fill_text(self, question: Question, text: str) -> None:
        """Fill a text input or textarea."""
        if question.text_input_name:
            locator = self._page.locator(
                f"input[name='{question.text_input_name}'], "
                f"textarea[name='{question.text_input_name}']"
            ).first
        else:
            locator = self._page.locator(
                f"div#question-{question.question_id} textarea, "
                f"div#question-{question.question_id} input[type='text']"
            ).first
        await locator.fill(text)

    async def _fill_matching(
        self, question: Question, pairs: dict[str, str]
    ) -> None:
        """Select the correct option in each dropdown of a matching question."""
        for pair in question.match_pairs:
            if pair.stem in pairs:
                chosen_value = pairs[pair.stem]
                await self._page.select_option(
                    f"select[name='{pair.select_name}']", label=chosen_value
                )

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_param(url: str, param: str) -> Optional[str]:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        values = params.get(param, [])
        return values[0] if values else None
