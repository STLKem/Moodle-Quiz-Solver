from __future__ import annotations

from textwrap import dedent

from moodle.api import MoodleAPI


def main() -> None:
    # A minimal Moodle-like MCQ HTML snippet:
    # - "Select one."
    # - UI strings "Clear my choice" / "Flag question" mixed in
    # - Real options in <label for="..."> associated with radio inputs
    html = dedent(
        """
        <div class="que multichoice deferredfeedback">
          <div class="info">
            <h3 class="no">Question 39</h3>
            <div class="state">Not yet answered</div>
            <a class="flag" href="#">Flag question</a>
          </div>
          <div class="content">
            <div class="qtext">
              <p>Typowa szerokość widmowa diody LED wynosi:</p>
              <p><em>Select one.</em></p>
              <a href="#" class="clearchoice">Clear my choice</a>
            </div>
            <div class="answer">
              <div class="r0">
                <input type="radio" name="q39:answer" id="q39_answer0" value="0"/>
                <label for="q39_answer0"><span class="answernumber">a.</span> Poniżej 0,1 nm</label>
              </div>
              <div class="r1">
                <input type="radio" name="q39:answer" id="q39_answer1" value="1"/>
                <label for="q39_answer1"><span class="answernumber">b.</span> Kilkadziesiąt nanometrów (np. 20–60 nm)</label>
              </div>
              <div class="r0">
                <input type="radio" name="q39:answer" id="q39_answer2" value="2"/>
                <label for="q39_answer2"><span class="answernumber">c.</span> Około 1 pm</label>
              </div>
              <div class="r1">
                <input type="radio" name="q39:answer" id="q39_answer3" value="3"/>
                <label for="q39_answer3"><span class="answernumber">d.</span> Kilka mikrometrów</label>
              </div>
              <!-- Some themes may repeat this UI action inside the answer div -->
              <div class="other">
                <a href="#" class="clearchoice">Clear my choice</a>
              </div>
            </div>
          </div>
        </div>
        """
    ).strip()

    print("=== annotated_text (QUESTION prompt_text) ===")
    print(MoodleAPI._annotated_text(html))

    print("\n=== parsed options (MCQ) ===")
    opts = MoodleAPI._parse_options_from_html(html)
    for o in opts:
        print(f"{o.key.upper()}) {o.label}   [name={o.input_name} value={o.input_value}]")


if __name__ == "__main__":
    main()

