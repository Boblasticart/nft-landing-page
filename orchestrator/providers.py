"""HTTP adapters for the supported provider types: OpenAI-compatible, Gemini, and a mock for dry runs."""
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass


class ProviderError(Exception):
    pass


class RateLimited(ProviderError):
    def __init__(self, message, retry_after=None, daily=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.daily = daily


class AuthError(ProviderError):
    pass


@dataclass
class Completion:
    text: str
    tokens: int


def estimate_tokens(text):
    return max(1, len(text) // 4)


def _post(url, headers, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        if e.code == 429:
            retry = e.headers.get("retry-after")
            try:
                retry = float(retry) if retry else None
            except ValueError:
                retry = None
            daily = bool(re.search(r"per[ _-]?day|daily|quota exceeded|RESOURCE_EXHAUSTED", detail, re.I))
            raise RateLimited(f"HTTP 429: {detail}", retry_after=retry, daily=daily) from None
        if e.code in (401, 403):
            raise AuthError(f"HTTP {e.code}: {detail}") from None
        raise ProviderError(f"HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ProviderError(f"unreachable: {e}") from None


def _openai(agent, system, prompt, timeout):
    headers = {"Authorization": f"Bearer {agent.api_key}"} if agent.api_key else {}
    body = {"model": agent.model, "max_tokens": agent.max_output_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
    data = _post(agent.base_url.rstrip("/") + "/chat/completions", headers, body, timeout)
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError):
        raise ProviderError(f"unexpected response: {str(data)[:300]}") from None
    tokens = (data.get("usage") or {}).get("total_tokens") or estimate_tokens(system + prompt + text)
    return Completion(text, tokens)


def _gemini(agent, system, prompt, timeout):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{agent.model}:generateContent"
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": agent.max_output_tokens}}
    data = _post(url, {"x-goog-api-key": agent.api_key}, body, timeout)
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        raise ProviderError(f"unexpected response: {str(data)[:300]}") from None
    text = "".join(p.get("text", "") for p in parts)
    tokens = (data.get("usageMetadata") or {}).get("totalTokenCount") or estimate_tokens(system + prompt + text)
    return Completion(text, tokens)


def _mock(agent, system, prompt, timeout):
    """Offline stand-in used by --dry-run so the full pipeline can be exercised without API keys."""
    cap = re.search(r"^CAPABILITY: (\w+)", prompt, re.M).group(1)
    task = re.search(r"^USER TASK: (.+)$", prompt, re.M).group(1)
    if cap == "planning":
        from .planner import heuristic_plan
        text = json.dumps(heuristic_plan(task))
    elif cap in ("design", "copy"):
        text = f"## {cap.title()} notes from {agent.name}\n\n- Mock {cap} output for: {task}\n"
    else:
        sub = re.search(r"^SUBTASK: (.+)$", prompt, re.M).group(1)
        text = (f"Mock {cap} output from {agent.name}.\n\n"
                f"=== FILE: index.html ===\n<!doctype html>\n<html lang=\"en\">\n<head>\n"
                f"<meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
                f"<title>{task}</title><link rel=\"stylesheet\" href=\"style.css\">\n</head>\n<body>\n"
                f"<h1>{task}</h1>\n<p>Last touched by {agent.name}: {sub}</p>\n"
                f"<script src=\"script.js\"></script>\n</body>\n</html>\n=== END FILE ===\n"
                f"=== FILE: style.css ===\nbody {{ font-family: system-ui, sans-serif; margin: 2rem; }}\n=== END FILE ===\n"
                f"=== FILE: script.js ===\nconsole.log('built by the orchestrator');\n=== END FILE ===\n")
    return Completion(text, estimate_tokens(system + prompt + text))


ADAPTERS = {"openai": _openai, "gemini": _gemini, "mock": _mock}


def complete(agent, system, prompt, timeout=180):
    try:
        adapter = ADAPTERS[agent.type]
    except KeyError:
        raise ProviderError(f"unknown provider type '{agent.type}'") from None
    return adapter(agent, system, prompt, timeout)
