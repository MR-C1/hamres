"""Self-test the LLM fallback chain wiring offline — no keys, no network.

Monkeypatches requests.post inside llm and flips the config flags, then
proves the ordering rules: every provider is tried only after the one
above it fails, Cloudflare is skipped without its account id, its URL
carries the account id, and a provider failure never poisons the chain.

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


def reset(**attrs):
    CALLS.clear()
    for k, v in attrs.items():
        setattr(llm.config, k, v)


def main():
    orig_post = llm.requests.post
    llm.requests.post = fake_post
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
        reset(CF_ACCOUNT_ID="")
        lines = llm.diagnose().splitlines()
        cf_line = next((l for l in lines if "cloudflare" in l), "")
        check("diagnose hints at the missing account id",
              "CF_ACCOUNT_ID missing" in cf_line)

        # 6. search_complete: no gemini keys -> groq compound answers
        CALLS.clear()
        llm._key_cooldown.clear()
        reset(GEMINI_API_KEY="", GEMINI_API_KEYS=[], GROQ_API_KEY="groqk",
              GROQ_SEARCH_MODEL="groq/compound")
        out = llm.search_complete("research this")
        check("search falls to groq without gemini keys", out == "ANSWER")
        gq = [c for c in CALLS if "api.groq.com" in c["url"]]
        check("search fallback hits groq /chat/completions",
              gq and gq[0]["url"]
              == "https://api.groq.com/openai/v1/chat/completions")
        check("search fallback model is the compound system",
              gq and gq[0]["json"].get("model") == "groq/compound")

        # 7. search_complete: every gemini key 429s -> still groq compound
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
                  GROQ_API_KEY="groqk", GROQ_SEARCH_MODEL="groq/compound")
            out = llm.search_complete("research this")
            check("gemini 429s -> search still answers via groq",
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
        llm.requests.post = lambda *a, **k: FakeResp(500, "groq down")
        try:
            llm.search_complete("research this")
            check("all search providers dead -> raises", False)
        except RuntimeError as e:
            check("all search providers dead -> raises",
                  "search grounding failed" in str(e))
    finally:
        llm.requests.post = orig_post

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all llm-chain checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
