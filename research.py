"""Research toolkit: free web search, page reading, quick answers (/ask)
and deep research (/deep).
"""

import json

import requests

import comms
import config
import llm

_UA = {"User-Agent": "Mozilla/5.0"}


def web_search(query, max_results=5):
    """Free web search, no API key. → [{'title','href','body'}, …]"""
    from ddgs import DDGS

    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results))


def extract_text(html):
    """HTML → readable text, stdlib only (script/style/head dropped)."""
    from html.parser import HTMLParser

    class _T(HTMLParser):
        SKIP = {"script", "style", "noscript", "head", "svg"}

        def __init__(self):
            super().__init__()
            self.parts = []
            self._skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in self.SKIP:
                self._skip += 1

        def handle_endtag(self, tag):
            if tag in self.SKIP and self._skip:
                self._skip -= 1

        def handle_data(self, data):
            if not self._skip and data.strip():
                self.parts.append(data.strip())

    p = _T()
    p.feed(html)
    return " ".join(p.parts)


def _fetch(url, cap=900 * 1024):
    r = requests.get(url, timeout=20, headers=_UA, stream=True)
    r.raise_for_status()
    chunks, size = [], 0
    for chunk in r.iter_content(chunk_size=64 * 1024):
        chunks.append(chunk)
        size += len(chunk)
        if size >= cap:
            break
    return b"".join(chunks).decode("utf-8", "ignore")


# --- quick research (/ask) — 1 request -----------------------------------------

def ask(question):
    try:
        results = web_search(question, max_results=5)
    except Exception as e:
        return (f"Web search failed ({e}). Knowledge-only answer:\n\n"
                + llm.complete([{"role": "user", "content": question}]))
    context = "\n\n".join(
        f"[{i+1}] {r['title']}\n{r['href']}\n{r['body']}"
        for i, r in enumerate(results))
    answer = llm.complete([
        {"role": "system",
         "content": "Research assistant. Answer the user's question using "
                    "the web results. Cite sources as [1], [2]. Be concise "
                    "and concrete. If results don't answer it, say so."},
        {"role": "user",
         "content": f"Question: {question}\n\nWeb results:\n{context}"},
    ])
    return answer + "\n\nSources:\n" + "\n".join(
        f"[{i+1}] {r['href']}" for i, r in enumerate(results))


# --- link reader (router 'summarize' action) — 1 request ------------------------

def summarize_url(url):
    if not url.startswith("http"):
        comms.send("Send a link that starts with http(s)://")
        return
    comms.send(f"🔎 Reading {url}…")
    try:
        page = extract_text(_fetch(url))[:6000]
    except Exception as e:
        comms.send(f"Couldn't fetch that page: {e}")
        return
    if len(page) < 200:
        comms.send("That page has barely any text (a login wall or a pure "
                   "app page?) — nothing to summarize.")
        return
    comms.typing()
    try:
        summary = llm.complete([
            {"role": "system",
             "content": "Summarize this web page for the user in 5-10 short "
                        "bullet points. Lead with what the page is. "
                        "Bangla/Banglish pages: answer in the same style."},
            {"role": "user", "content": f"URL: {url}\n\n{page}"},
        ], max_tokens=500)
        comms.send(f"📄 {summary}")
    except Exception as e:
        comms.send(f"Read the page but summarizing failed: {e}")


# --- deep research (/deep, router 'deep' action) — 2 requests --------------------

def deep_research(question):
    if not question:
        comms.send("Ask me something: /deep <question>")
        return
    comms.send(f"🔬 <b>Deep research</b>\n{comms.esc(question)}\n"
               f"Searching and reading sources…", html=True)
    try:
        results = web_search(question, max_results=6)
    except Exception as e:
        comms.send(f"Search failed: {e}")
        return
    if not results:
        comms.send("No search results came back — try rephrasing?")
        return

    # call 1: pick the 3 most promising URLs
    listed = "\n".join(f"{i+1}. {r['title']} — {r['href']}"
                       for i, r in enumerate(results))
    comms.typing()
    try:
        pick = llm.complete([
            {"role": "system",
             "content": "Pick the 3 URLs most likely to answer the "
                        "question. Reply ONLY a JSON array of index "
                        "numbers, e.g. [1,3,5]."},
            {"role": "user", "content": f"Question: {question}\n\n{listed}"},
        ], max_tokens=60)
        idxs = [i - 1 for i in json.loads(pick.strip())
                if isinstance(i, int) and 1 <= i <= len(results)][:3]
    except Exception:
        idxs = [0, 1, 2]

    # read the picked pages (free — no LLM)
    pages = []
    for i in idxs:
        try:
            page = extract_text(_fetch(results[i]["href"], 600 * 1024))[:2500]
            if len(page) > 150:
                pages.append({"url": results[i]["href"], "text": page})
        except Exception:
            pass

    context = "Search results:\n" + "\n\n".join(
        f"[{i+1}] {r['title']}\n{r['href']}\n{r['body']}"
        for i, r in enumerate(results))
    if pages:
        context += "\n\nRead pages:\n" + "\n\n".join(
            f"[P{i+1}] {p['url']}\n{p['text']}" for i, p in enumerate(pages))

    # call 2: synthesize
    comms.typing()
    answer = llm.complete([
        {"role": "system",
         "content": "Research assistant. Answer the question using the "
                    "search results and read pages. Cite sources as [1] or "
                    "[P1]. Be concrete and complete but concise. If sources "
                    "disagree or lack the answer, say so."},
        {"role": "user",
         "content": f"Question: {question}\n\n{context[:15000]}"},
    ], max_tokens=900)
    srcs = "\n".join(f"[{i+1}] {r['href']}" for i, r in enumerate(results))
    psrcs = "\n".join(f"[P{i+1}] {p['url']}" for i, p in enumerate(pages))
    comms.send(f"{answer}\n\nSources:\n{srcs}" + (f"\n{psrcs}" if psrcs else ""))
