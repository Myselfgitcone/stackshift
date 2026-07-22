"""
Multi-provider LLM client.

Supports four providers, selectable per request from the UI:

  openai      -> OpenAI SDK, default endpoint
  openrouter  -> OpenAI SDK, base_url = https://openrouter.ai/api/v1
  kimi        -> OpenAI SDK, base_url = https://api.moonshot.ai/v1  (Moonshot / Kimi)
  anthropic   -> Anthropic SDK (Claude)

Every provider except anthropic is OpenAI-compatible, so they share one code
path. Keys/models can come from the request (UI) or from environment variables.
"""
import json
import os

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------
PROVIDERS = {
    "openai": {
        "label": "OpenAI",
        "kind": "openai",
        "base_url": None,  # SDK default
        "default_model": "gpt-4o-mini",
        "env_key": "OPENAI_API_KEY",
        "models": [
            "gpt-4o-mini",
            "gpt-4o",
            "gpt-4.1",
            "gpt-4.1-mini",
            "o4-mini",
            "o3",
            "o3-mini",
        ],
    },
    "openrouter": {
        "label": "OpenRouter",
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "openai/gpt-4o-mini",
        "env_key": "OPENROUTER_API_KEY",
        "models": [
            "openai/gpt-4o-mini",
            "openai/gpt-4o",
            "anthropic/claude-sonnet-5",
            "anthropic/claude-opus-4.8",
            "google/gemini-2.0-flash-001",
            "google/gemini-2.5-pro",
            "meta-llama/llama-3.3-70b-instruct",
            "moonshotai/kimi-k2",
            "deepseek/deepseek-chat",
            "mistralai/mistral-large",
        ],
    },
    "kimi": {
        "label": "Kimi (Moonshot)",
        "kind": "openai",
        "base_url": "https://api.moonshot.ai/v1",
        "default_model": "kimi-k2-0711-preview",
        "env_key": "KIMI_API_KEY",
        "models": [
            "kimi-k2-0711-preview",
            "kimi-latest",
            "moonshot-v1-8k",
            "moonshot-v1-32k",
            "moonshot-v1-128k",
            "moonshot-v1-auto",
        ],
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "kind": "anthropic",
        "base_url": None,
        "default_model": "claude-sonnet-5",
        "env_key": "ANTHROPIC_API_KEY",
        "models": [
            "claude-opus-4-8",
            "claude-sonnet-5",
            "claude-haiku-4-5-20251001",
            "claude-fable-5",
            "claude-3-7-sonnet-latest",
            "claude-3-5-haiku-latest",
        ],
    },
}


# Cheap model per provider for the mechanical clean-up pass (same API key).
CHEAP_MODELS = {
    "openai": "gpt-4o-mini",
    "openrouter": "openai/gpt-4o-mini",
    "kimi": "moonshot-v1-8k",
    "anthropic": "claude-haiku-4-5-20251001",
}


def cheap_model(provider: str) -> str:
    return CHEAP_MODELS.get(provider, PROVIDERS.get(provider, {}).get("default_model", ""))


# Preference order of cheap-model keywords to look for in the live model list.
_CHEAP_HINTS = ["haiku", "gpt-4o-mini", "4o-mini", "flash", "mini", "moonshot-v1-8k", "8k", "nano"]


def pick_cheap_model(provider: str, api_key: str | None) -> str | None:
    """Find a genuinely-available cheap model on THIS account by querying the live
    model list and matching known cheap-model keywords. Returns None if none found
    (caller should then fall back to the user's main model)."""
    info = list_models(provider, api_key)
    if info.get("source") != "live":
        # No live list -> trust the static default only if it's plausibly present.
        return CHEAP_MODELS.get(provider)
    models = [m.lower() for m in info.get("models", [])]
    originals = info.get("models", [])
    for hint in _CHEAP_HINTS:
        for orig, low in zip(originals, models):
            if hint in low:
                return orig
    return None


def provider_meta() -> list:
    """Public metadata for the frontend to render provider windows."""
    return [
        {
            "id": pid,
            "label": p["label"],
            "default_model": p["default_model"],
            "models": p.get("models", [p["default_model"]]),
            "env_key": p["env_key"],
            "has_env_key": bool(os.getenv(p["env_key"])),
        }
        for pid, p in PROVIDERS.items()
    ]


def list_models(provider: str, api_key: str | None = None) -> dict:
    """Fetch the CURRENT model list straight from the provider's API.

    Always live — never a stale hardcoded list. Falls back to the static
    `models` seed only when there's no key or the API call fails (offline).

    Returns {"models": [...], "source": "live"|"fallback", "note": str}.
    """
    if provider not in PROVIDERS:
        return {"models": [], "source": "fallback", "note": f"unknown provider '{provider}'"}
    cfg = PROVIDERS[provider]
    seed = cfg.get("models", [cfg["default_model"]])

    try:
        key = _resolve_key(provider, api_key)
    except RuntimeError:
        # OpenRouter's model list is public — try it even without a key.
        if provider == "openrouter":
            key = "sk-or-public"
        else:
            return {"models": seed, "source": "fallback", "note": "no API key — showing seed list"}

    try:
        if cfg["kind"] == "anthropic":
            from anthropic import Anthropic

            client = Anthropic(api_key=key)
            ids = [m.id for m in client.models.list(limit=100).data]
        else:
            from openai import OpenAI

            client = OpenAI(api_key=key, base_url=cfg["base_url"])
            ids = [m.id for m in client.models.list().data]
    except Exception as exc:  # noqa: BLE001 — any network/auth failure -> fallback
        return {"models": seed, "source": "fallback", "note": f"live fetch failed: {exc}"}

    ids = sorted(set(i for i in ids if i))
    if not ids:
        return {"models": seed, "source": "fallback", "note": "provider returned no models"}
    return {"models": ids, "source": "live", "note": f"{len(ids)} models fetched live"}


def _resolve_key(provider: str, override: str | None) -> str:
    if override and override.strip():
        return override.strip()
    env_key = PROVIDERS[provider]["env_key"]
    key = os.getenv(env_key)
    if not key:
        raise RuntimeError(
            f"No API key for '{provider}'. Enter one in its window, or set {env_key} in .env."
        )
    return key


# ---------------------------------------------------------------------------
# OpenAI-compatible path (openai / openrouter / kimi)
# ---------------------------------------------------------------------------
def _openai_chat(cfg, key, model, system, user, json_mode, temperature) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=cfg["base_url"])
    kwargs = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Anthropic path (claude)
# ---------------------------------------------------------------------------
def _anthropic_chat(key, model, system, user, json_mode, temperature) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=key)
    sys_prompt = system
    if json_mode:
        sys_prompt += "\n\nRespond with ONLY a single valid JSON object. No prose, no code fences."
    msg = client.messages.create(
        model=model,
        max_tokens=4096,
        temperature=temperature,
        system=sys_prompt,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(block.text for block in msg.content if block.type == "text")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def chat(
    system: str,
    user: str,
    *,
    provider: str = "openai",
    api_key: str | None = None,
    model: str | None = None,
    json_mode: bool = False,
    temperature: float = 0.4,
) -> str:
    if provider not in PROVIDERS:
        raise RuntimeError(f"Unknown provider '{provider}'.")
    cfg = PROVIDERS[provider]
    key = _resolve_key(provider, api_key)
    model = (model or "").strip() or cfg["default_model"]

    if cfg["kind"] == "anthropic":
        return _anthropic_chat(key, model, system, user, json_mode, temperature)
    return _openai_chat(cfg, key, model, system, user, json_mode, temperature)


def chat_json(
    system: str,
    user: str,
    *,
    provider: str = "openai",
    api_key: str | None = None,
    model: str | None = None,
    temperature: float = 0.2,
) -> dict:
    raw = chat(
        system,
        user,
        provider=provider,
        api_key=api_key,
        model=model,
        json_mode=True,
        temperature=temperature,
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                pass
        return {}
