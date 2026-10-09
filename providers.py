import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

base_dir = (
    Path(sys.executable).parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
env_path = base_dir / ".env"
if env_path.exists():
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

PROVIDER_MODELS = {
    "groq": "openai/gpt-oss-20b",
    "gemini": "gemini-3.5-flash-lite",
    "openai": "gpt-4o-mini",
    "claude": "claude-sonnet-4-5",
    "openrouter": "openrouter/free",
}

LAST_CALL = {"provider": None, "model": None, "chain": []}


def get_last_call_info() -> dict:
    return dict(LAST_CALL)


def _to_messages(prompt) -> list:
    if isinstance(prompt, list):
        return prompt
    return [{"role": "user", "content": str(prompt)}]


def _call_openai_compatible(
    url: str, api_key: str, model: str, prompt, extra_headers: dict = None
) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "PersonalLearner/1.0 (Python CLI)",
    }
    if extra_headers:
        headers.update(extra_headers)

    payload = {
        "model": model,
        "messages": _to_messages(prompt),
        "temperature": 0.2,
        "max_tokens": 4096,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        choice = data["choices"][0]
        content = choice["message"]["content"].strip()
        if choice.get("finish_reason") == "length":
            content += (
                "\n\n[⚠️ Note: Output was truncated by the model's token limit]"
            )
        return content
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} ({e.reason}): {error_body}") from e


def call_groq(prompt) -> str:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise ValueError("GROQ_API_KEY missing")
    return _call_openai_compatible(
        "https://api.groq.com/openai/v1/chat/completions",
        key,
        PROVIDER_MODELS["groq"],
        prompt,
    )


def call_openai(prompt) -> str:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise ValueError("OPENAI_API_KEY missing")
    return _call_openai_compatible(
        "https://api.openai.com/v1/chat/completions",
        key,
        PROVIDER_MODELS["openai"],
        prompt,
    )


def call_claude(prompt) -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY missing")

    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": PROVIDER_MODELS["claude"],
        "max_tokens": 4096,
        "messages": _to_messages(prompt),
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return data["content"][0]["text"].strip()


def call_gemini(prompt) -> str:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise ValueError("GEMINI_API_KEY missing")

    model = PROVIDER_MODELS["gemini"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "PersonalLearner/1.0 (Python CLI)",
    }

    # Format multi-turn messages for Gemini REST API
    raw_msgs = _to_messages(prompt)
    contents = []
    for m in raw_msgs:
        role = "model" if m["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})

    payload = {
        "contents": contents,
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096},
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API HTTP {e.code}: {error_body}") from e


def call_openrouter(prompt) -> str:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("OPENROUTER_API_KEY missing")
    return _call_openai_compatible(
        "https://openrouter.ai/api/v1/chat/completions",
        key,
        PROVIDER_MODELS["openrouter"],
        prompt,
        extra_headers={"HTTP-Referer": "https://github.com/Recursive-Prodigy/learner"},
    )


PROVIDERS = {
    "groq": call_groq,
    "gemini": call_gemini,
    "openai": call_openai,
    "claude": call_claude,
    "openrouter": call_openrouter,
}

FALLBACK_ORDER = ["groq", "gemini", "openai", "claude", "openrouter"]


def call_llm(prompt, provider: str = None) -> str:
    target = (provider or os.getenv("LLM_PROVIDER", "auto")).lower().strip()

    if target in PROVIDERS:
        model = PROVIDER_MODELS.get(target, "custom")
        LAST_CALL["provider"] = target
        LAST_CALL["model"] = model
        LAST_CALL["chain"] = []
        return PROVIDERS[target](prompt)

    failed_attempts = []
    for candidate in FALLBACK_ORDER:
        model = PROVIDER_MODELS.get(candidate, "custom")
        try:
            response = PROVIDERS[candidate](prompt)
            LAST_CALL["provider"] = candidate
            LAST_CALL["model"] = model
            LAST_CALL["chain"] = failed_attempts
            return response
        except Exception as e:
            failed_attempts.append((candidate, str(e)))
            continue

    error_summary = "\n".join(f"- {p}: {err}" for p, err in failed_attempts)
    raise RuntimeError(f"All LLM providers failed:\n{error_summary}")