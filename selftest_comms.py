"""Check the Telegram text handling without touching the network.

Long messages are where sends used to disappear: send() cut the text every
4000 characters to fit Telegram's 4096 cap, and a cut landing inside a tag
(or between a <b> and its close) makes Telegram reject that chunk outright
with a parse error — so part of a weekly summary or a long chat answer
simply never arrived, and nothing said so.

_chunks, _plain and md are pure, and comms imports only requests + config
at module level, so this runs in milliseconds:

    python selftest_comms.py
"""
import sys

import comms


def main():
    fails = []

    def check(label, got, want):
        ok = want(got) if callable(want) else got == want
        print(f"{'ok  ' if ok else 'FAIL'} {label}: {str(got)[:70]}")
        if not ok:
            fails.append(label)

    # --- splitting -------------------------------------------------------
    check("a short message stays one chunk",
          comms._chunks("hello"), ["hello"])
    check("nothing in, nothing out", comms._chunks(""), [])

    lines = "\n".join(f"<b>line {i}</b> some narration text here"
                      for i in range(300))
    parts = comms._chunks(lines)
    check("a long message is split", len(parts), lambda n: n >= 2)
    check("every chunk fits the cap",
          max(len(p) for p in parts), lambda n: n <= 4000)
    # the whole point: a chunk boundary must not land inside a tag, so no
    # chunk may end with an unclosed '<' or open a tag it never closes
    check("no chunk is cut inside a tag",
          [p for p in parts if p.count("<") != p.count(">")], [])
    check("no chunk splits a bold pair",
          [p for p in parts if p.count("<b>") != p.count("</b>")], [])
    check("nothing is lost but the newlines at the cuts",
          "".join(parts).replace("\n", ""), lines.replace("\n", ""))

    # a single line longer than the cap still has to be cut — and must not
    # loop forever looking for a break that isn't there
    huge = comms._chunks("x" * 9000)
    check("an unbreakable line is hard-cut", [len(p) for p in huge],
          [4000, 4000, 1000])

    # a break too early in the window is ignored: cutting at 100 to avoid a
    # tag would waste 3900 characters of a 4096 message
    early = comms._chunks("a\n" + "b" * 5000)
    check("a useless early break is not used", len(early[0]), 4000)

    # --- the plain-text fallback -----------------------------------------
    check("tags come out",
          comms._plain("<b>Daily report</b> — <i>3 subs</i>"),
          "Daily report — 3 subs")
    check("entities come back",
          comms._plain("Q&amp;A &lt;draft&gt;"), "Q&A <draft>")
    check("a bare less-than survives",
          comms._plain("subs < 10"), "subs < 10")
    check("a link keeps its text",
          comms._plain('<a href="https://youtu.be/x">Watch it</a>'),
          "Watch it")

    # --- md(): escape first, convert second ------------------------------
    # an LLM writes the script titles and chat answers, so its output must
    # not be able to inject markup of its own
    check("model markup cannot get through",
          comms.md("<script>alert(1)</script>"),
          lambda s: "<script>" not in s and "&lt;script&gt;" in s)
    check("bold converts", comms.md("**loud**"), "<b>loud</b>")
    check("a heading converts", comms.md("## Title"), "<b>Title</b>")
    check("an ampersand stays escaped for HTML",
          comms.md("Q&A"), "Q&amp;A")

    print()
    if fails:
        print(f"{len(fails)} FAILED: {', '.join(fails)}")
        return 1
    print("all telegram-text checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
