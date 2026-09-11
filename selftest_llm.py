"""Self-test the LLM fallback chain wiring offline — no keys, no network.

Monkeypatches requests.post/get inside llm and flips the config flags,
then proves the ordering rules: every provider is tried only after the
one above it fails, Cloudflare is skipped without its account id, its
URL carries the account id, and a provider failure never poisons the
chain. The search path gets the same treatment: gemini grounding fails
-> the KEYLESS duckduckgo fallback answers (real results embedded in
the extraction prompt), and compound only fires when the scrape is
down. Script-grade generation can also be restricted to the big gemini
so a 503 falls to groq instead of a lite model.

    python selftest_llm.py
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import llm  # noqa: E402

FAILS = []
CALLS = []


def check(label, ok):
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        FAILS.append(label)


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.text = str(body or "")

    def json(self):
        if self.status_code == 200:
            return {"choices": [{"message": {"content": "ANSWER"}}]}
        return {"error": {"message": self.text}}


def fake_post(url, headers=None, json=None, timeout=None):
    CALLS.append({"url": url, "headers": headers or {}, "json": json or {}})
    # cloudflare deliberately 500s to prove the chain keeps falling
    if "api.cloudflare.com" in url:
        return FakeResp(500, "neuron budget blown")
    return FakeResp(200)


# --- canned duckduckgo pages (the keyless search fallback) --------------
DDG_HTML = """
<div class="links_main links_deep result__body">
<h2 class="result__title">
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FMary_Celeste&amp;rut=xyz">The <b>Mary Celeste</b> mystery &amp; facts</a>
</h2>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FMary_Celeste">The ship was found <b>abandoned</b> in 1872 &mdash; the crew was never found.</a>
</div>
<div class="links_main links_deep result__body">
<h2 class="result__title">
<a rel="nofollow" class="result__a" href="https://www.britannica.com/topic/Mary-Celeste">Mary Celeste | History &amp; Crew</a>
</h2>
<a class="result__snippet" href="https://www.britannica.com/topic/Mary-Celeste">brigantine found derelict at sea</a>
</div>
<div class="links_main links_deep result__body">
<h2 class="result__title">
<a rel="nofollow" class="result__a" href="/y.js?ad_domain=x">Sponsored result</a>
</h2>
<a class="result__snippet" href="/x">buy things</a>
</div>
"""

DDG_LITE = """<tr><td><a rel="nofollow" href="https://lite.example/story" class='result-link'>Lite Story</a></td></tr>
<tr><td class="result-snippet">A snippet from lite.</td></tr>"""


class FakeGet:
    def __init__(self, status=200, text=DDG_HTML):
        self.status_code = status
        self.text = text


# per-test stand-in: None = canned page; FakeGet/Exception = that;
# callable = f(url) -> FakeGet/Exception
GET_MODE = {"resp": None}


def fake_get(url, headers=None, timeout=None):
    got = GET_MODE["resp"]
    if callable(got):
        got = got(url)
    if isinstance(got, Exception):
        raise got
    return got if got is not None else FakeGet()


def reset(**attrs):
    CALLS.clear()
    for k, v in attrs.items():
        setattr(llm.config, k, v)


def main():
    orig_post = llm.requests.post
    orig_get = llm.requests.get
    llm.requests.post = fake_post
    llm.requests.get = fake_get
    try:
        # 1. cloudflare fires when it is the only provider — and the URL
        #    must carry the account id (the per-account endpoint)
        reset(GEMINI_API_KEY="", GEMINI_API_KEYS=[], GROQ_API_KEY="",
              OPENROUTER_API_KEY="", CF_API_TOKEN="cftok",
              CF_ACCOUNT_ID="acc123")
        try:
            llm.complete("hi")
            check("cloudflare reached when last provider standing", False)
        except RuntimeError as e:
            cf_calls = [c for c in CALLS if "cloudflare" in c["url"]]
            check("cloudflare reached when last provider standing",
                  len(cf_calls) == 1)
            check("cloudflare url carries the account id",
                  cf_calls and "/accounts/acc123/ai/v1/chat/completions"
                  in cf_calls[0]["url"])
            check("cloudflare error surfaces in the summary",
                  "cloudflare" in str(e))

        # 2. token without account id -> provider skipped entirely
        CALLS.clear()
        reset(GEMINI_API_KEY="", GEMINI_API_KEYS=[], GROQ_API_KEY="",
              OPENROUTER_API_KEY="", CF_API_TOKEN="cftok",
              CF_ACCOUNT_ID="")
        try:
            llm.complete("hi")
            check("no account id -> chain skips cloudflare, fails clean",
                  False)
        except RuntimeError as e:
            check("no account id -> chain skips cloudflare, fails clean",
                  not any("cloudflare" in c["url"] for c in CALLS)
                  and "cloudflare" not in str(e))

        # 3. ordering: groq answers, cloudflare never gets a call
        CALLS.clear()
        reset(GEMINI_API_KEY="", GEMINI_API_KEYS=[], GROQ_API_KEY="groqk",
              OPENROUTER_API_KEY="", CF_API_TOKEN="cftok",
              CF_ACCOUNT_ID="acc123")
        out = llm.complete("hi")
        check("groq answers when live", out == "ANSWER")
        check("cloudflare untouched when groq succeeds",
              not any("cloudflare" in c["url"] for c in CALLS))

        # 4. auth header and model name ride along to cloudflare
        CALLS.clear()
        reset(GEMINI_API_KEY="", GEMINI_API_KEYS=[], GROQ_API_KEY="",
              OPENROUTER_API_KEY="", CF_API_TOKEN="cftok",
              CF_ACCOUNT_ID="acc456",
              CF_MODEL="@cf/meta/llama-3.3-70b-instruct-fp8-fast")
        try:
            llm.complete("hi")
        except RuntimeError:
            pass
        cf = [c for c in CALLS if "cloudflare" in c["url"]]
        check("bearer token sent to cloudflare",
              cf and cf[0]["headers"].get("Authorization") == "Bearer cftok")
        check("model name sent to cloudflare",
              cf and cf[0]["json"].get("model")
              == "@cf/meta/llama-3.3-70b-instruct-fp8-fast")

        # 5. diagnose() reports the provider (and the missing-id hint)
        lines = llm.diagnose().splitlines()
        cf_line = next((l for l in lines if "cloudflare" in l), "")
        check("diagnose shows cloudflare attempt",
              cf_line.startswith("❌ cloudflare"))
        ddg_line = next((l for l in lines if "duckduckgo" in l), "")
        check("diagnose shows the keyless search fallback armed",
              ddg_line.startswith("✅ duckduckgo"))
        reset(CF_ACCOUNT_ID="")
        GET_MODE["resp"] = FakeGet(403, "anomaly")
        lines = llm.diagnose().splitlines()
        ddg_line = next((l for l in lines if "duckduckgo" in l), "")
        check("diagnose reports a dead keyless fallback",
              ddg_line.startswith("❌ duckduckgo"))
        cf_line = next((l for l in lines if "cloudflare" in l), "")
        check("diagnose hints at the missing account id",
              "CF_ACCOUNT_ID missing" in cf_line)
        GET_MODE["resp"] = None

        # 5b. the ddg parser: uddg unwrap, entity unescape, tag strip,
        #     non-http filtering, the lite endpoint, endpoint fallback
        res = llm.ddg_search("mary celeste")
        check("ddg unwraps the uddg redirect",
              res and res[0][1]
              == "https://en.wikipedia.org/wiki/Mary_Celeste")
        check("ddg strips tags and unescapes entities",
              res and res[0][0] == "The Mary Celeste mystery & facts")
        check("ddg keeps the snippet text",
              res and "abandoned" in res[0][2]
              and "1872" in res[0][2])
        check("ddg filters non-http results",
              res and all(u.startswith("http") for _, u, _ in res))
        GET_MODE["resp"] = FakeGet(200, DDG_LITE)
        res = llm.ddg_search("q")
        check("ddg parses the lite endpoint too",
              res and res[0][0] == "Lite Story"
              and res[0][2] == "A snippet from lite."
              and res[0][1] == "https://lite.example/story")
        GET_MODE["resp"] = lambda url: (
            FakeGet(403, "anomaly") if "html.duckduckgo" in url
            else FakeGet(200, DDG_LITE))
        res = llm.ddg_search("q")
        check("ddg falls to lite when the html endpoint is blocked",
              res and res[0][0] == "Lite Story")
        GET_MODE["resp"] = FakeGet(403, "anomaly")
        try:
            llm.ddg_search("q")
            check("ddg raises when both endpoints fail", False)
        except RuntimeError as e:
            check("ddg raises when both endpoints fail",
                  "duckduckgo search failed" in str(e))
        GET_MODE["resp"] = None

        # 5c. gemini_models restriction: flash-latest 503s -> groq,
        #     and NO lite gemini is ever asked. Unrestricted, the lite
        #     gemini answers as before (groq untouched).
        import types
        fake_google = types.ModuleType("google")
        fake_genai = types.ModuleType("google.genai")
        tried = []

        class FiftyThree(Exception):
            pass

        class FakeModels:
            def generate_content(self, model, contents, config=None):
                tried.append(model)
                if model == "gemini-flash-latest":
                    raise FiftyThree("503 UNAVAILABLE model overloaded")
                class R:
                    text = "LITE ANSWER"
                return R()

        class FakeClient:
            def __init__(self, api_key=None):
                pass

            models = FakeModels()

        fake_genai.Client = FakeClient
        fake_google.genai = fake_genai
        real_google = sys.modules.get("google")
        sys.modules["google"] = fake_google
        sys.modules["google.genai"] = fake_genai
        try:
            CALLS.clear()
            reset(GEMINI_API_KEY="gk1", GEMINI_API_KEYS=["gk1"],
                  GROQ_API_KEY="groqk", GROQ_MODEL="openai/gpt-oss-120b")
            out = llm.complete("hi", gemini_models=["gemini-flash-latest"])
            check("restricted chain falls to groq on a flash-latest 503",
                  out == "ANSWER")
            check("no lite gemini touched when restricted",
                  tried == ["gemini-flash-latest"])
            tried.clear()
            CALLS.clear()
            out = llm.complete("hi")
            check("unrestricted chain still uses the lite gemini",
                  out == "LITE ANSWER")
            check("groq untouched when a gemini answers",
                  not any("api.groq.com" in c["url"] for c in CALLS))
        finally:
            if real_google is None:
                sys.modules.pop("google", None)
                sys.modules.pop("google.genai", None)
            else:
                sys.modules["google"] = real_google
            llm._key_cooldown.clear()

        # 6. search_complete: no gemini keys -> the KEYLESS ddg path
        #    answers (query call + extraction call ride the chain);
        #    compound stays untouched while ddg works
        CALLS.clear()
        llm._key_cooldown.clear()
        reset(GEMINI_API_KEY="", GEMINI_API_KEYS=[], GROQ_API_KEY="groqk",
              GROQ_MODEL="openai/gpt-oss-120b",
              GROQ_SEARCH_MODEL="groq/compound")
        out = llm.search_complete("research this")
        check("search falls to keyless ddg without gemini keys",
              out == "ANSWER")
        gq = [c for c in CALLS if "api.groq.com" in c["url"]]
        check("both ddg calls ride the working chain (query + extract)",
              len(gq) == 2)
        check("no compound call while ddg works",
              all(c["json"].get("model") != "groq/compound" for c in gq))
        check("query call asks for a bare search query",
              gq and "output ONLY the search query"
              in gq[0]["json"]["messages"][-1]["content"])
        extract = gq[1]["json"]["messages"][-1]["content"]
        check("extraction call embeds the real results",
              "REAL web-search results" in extract
              and "https://en.wikipedia.org/wiki/Mary_Celeste" in extract)
        check("extraction call carries the original prompt",
              "research this" in extract)

        # 7. search_complete: every gemini key 429s -> still ddg, and
        #    the quota-hit key still gets its cooldown
        import types
        fake_google = types.ModuleType("google")
        fake_genai = types.ModuleType("google.genai")

        class QuotaErr(Exception):
            pass

        class FakeModels:
            def generate_content(self, model, contents, config=None):
                raise QuotaErr("429 RESOURCE_EXHAUSTED quota")

        class FakeClient:
            def __init__(self, api_key=None):
                pass

            models = FakeModels()

        fake_genai.Client = FakeClient
        fake_google.genai = fake_genai
        real_google = sys.modules.get("google")
        sys.modules["google"] = fake_google
        sys.modules["google.genai"] = fake_genai
        try:
            CALLS.clear()
            llm._key_cooldown.clear()
            reset(GEMINI_API_KEY="gk1", GEMINI_API_KEYS=["gk1"],
                  GROQ_API_KEY="groqk", GROQ_MODEL="openai/gpt-oss-120b",
                  GROQ_SEARCH_MODEL="groq/compound")
            out = llm.search_complete("research this")
            check("gemini 429s -> search still answers via ddg",
                  out == "ANSWER")
            check("quota-hit gemini key got a cooldown",
                  llm._key_cooldown.get("gk1", 0) > 0)
        finally:
            if real_google is None:
                sys.modules.pop("google", None)
                sys.modules.pop("google.genai", None)
            else:
                sys.modules["google"] = real_google
            llm._key_cooldown.clear()

        # 7b. search_complete: ddg scrape down -> groq compound fires as
        #     the last resort (kept for the day its search stops 413ing)
        GET_MODE["resp"] = FakeGet(403, "anomaly")
        CALLS.clear()
        reset(GEMINI_API_KEY="", GEMINI_API_KEYS=[], GROQ_API_KEY="groqk",
              GROQ_MODEL="openai/gpt-oss-120b",
              GROQ_SEARCH_MODEL="groq/compound")
        out = llm.search_complete("research this")
        comp = [c for c in CALLS if c["json"].get("model") == "groq/compound"]
        check("ddg down -> compound answers as last resort",
              out == "ANSWER" and len(comp) == 1)
        check("compound call is a plain chat completion",
              comp and comp[0]["url"]
              == "https://api.groq.com/openai/v1/chat/completions")
        GET_MODE["resp"] = None

        # 8. search_complete: nothing configured / everything dead
        CALLS.clear()
        reset(GEMINI_API_KEY="", GEMINI_API_KEYS=[], GROQ_API_KEY="")
        try:
            llm.search_complete("research this")
            check("no search provider -> clean error", False)
        except RuntimeError as e:
            check("no search provider -> clean error",
                  "no search provider" in str(e))
        reset(GROQ_API_KEY="groqk")
        GET_MODE["resp"] = FakeGet(403, "anomaly")
        llm.requests.post = lambda *a, **k: FakeResp(500, "groq down")
        try:
            llm.search_complete("research this")
            check("all search providers dead -> raises", False)
        except RuntimeError as e:
            check("all search providers dead -> raises",
                  "search grounding failed" in str(e))
    finally:
        llm.requests.post = orig_post
        llm.requests.get = orig_get

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all llm-chain checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
