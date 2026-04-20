"""Unified LLM client built on LiteLLM.

One gateway so the rest of the codebase stops talking to `openai.OpenAI`
directly. Handles:
  - provider routing (openai/ anthropic/ gemini/ deepseek/ ollama/ ...) via
    LiteLLM's model-name prefix convention
  - param translation for OpenAI reasoning models (o1/o3/o4/gpt-5): rename
    max_tokens -> max_completion_tokens, drop temperature/top_p
  - timeout + retry defaults (so a hung local Ollama can't wedge the job)
  - robust JSON extraction that survives markdown fences, <think> blocks,
    single-quote Python-style dicts, and prose around the JSON
  - response_format={"type":"json_object"} when the provider supports it
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

import litellm
from loguru import logger
from omegaconf import DictConfig, OmegaConf


# Providers known to accept response_format={"type":"json_object"} on the
# OpenAI-compatible /chat/completions endpoint. Conservative — false
# negatives here just mean we fall back to prompt-only JSON coaxing, which
# is safe. LiteLLM itself will also translate this param for Gemini/
# Anthropic when routing through native SDKs.
_JSON_MODE_PROVIDER_PREFIXES = (
    "openai/",
    "azure/",
    "deepseek/",
    "mistral/",
    "together_ai/",
    "groq/",
    "fireworks_ai/",
    "openrouter/",
    "anthropic/",
    "gemini/",
    "vertex_ai/",
)

# Model-name patterns that require the reasoning-model param shape:
# max_tokens -> max_completion_tokens, and no temperature/top_p. Covers
# OpenAI o-series / gpt-5, Moonshot Kimi thinking, DeepSeek-R1, Qwen QwQ,
# and any provider that puts "-thinking" / "-reasoning" in the model name.
_REASONING_MODEL_RE = re.compile(
    r"^(?:[\w.-]+/)?("
    r"o\d[-_]?"                    # openai o1 / o3 / o4
    r"|gpt-5"                      # openai gpt-5 family
    r"|kimi-thinking"              # moonshot kimi-thinking-preview
    r"|kimi-k2-thinking"           # moonshot kimi-k2-thinking
    r"|deepseek-reasoner"          # deepseek R1 via chat.deepseek.com
    r"|deepseek-r1"                # deepseek R1 (other routes)
    r"|qwq"                        # alibaba QwQ
    r"|[\w.:-]*-thinking"          # generic *-thinking suffix
    r"|[\w.:-]*-reasoning"         # generic *-reasoning suffix
    r")",
    re.IGNORECASE,
)

# Runtime cache populated when a provider rejects the temperature param at
# call time (e.g. "invalid temperature: only 1 is allowed for this model").
# Once a model lands here we treat it as a reasoning model for the rest of
# the process's lifetime, so the next call skips temperature from the start.
_TEMPERATURE_BLOCKED_MODELS: set[str] = set()


def _is_reasoning_model(model: str) -> bool:
    if not model:
        return False
    if model in _TEMPERATURE_BLOCKED_MODELS:
        return True
    return bool(_REASONING_MODEL_RE.match(model))


def _looks_like_temperature_rejection(exc: BaseException) -> bool:
    """True when the provider rejected the request because temperature is
    fixed on this model (reasoning models on OpenAI / Moonshot / DeepSeek /
    Qwen / any OpenAI-compatible gateway). Markers span multiple provider
    phrasings, so detection works regardless of which base_url the user
    configured."""
    msg = str(exc).lower()
    if "temperature" not in msg:
        return False
    return any(
        marker in msg
        for marker in (
            "only 1",                # "only 1 is allowed" / "only 1.0"
            "does not support",
            "not supported",
            "unsupported",
            "must be 1",
            "invalid temperature",
            "not allowed",
            "is fixed",
        )
    )


def _supports_json_mode(model: str) -> bool:
    m = (model or "").lower()
    return any(m.startswith(p) for p in _JSON_MODE_PROVIDER_PREFIXES)


def _extract_json_blob(text: str, expect: str = "object") -> str | None:
    """Pull the outermost balanced JSON object/array out of free-form text.

    Survives: <think>...</think> reasoning leakage, ```json fences, a short
    prose preface, and trailing commentary. expect="object" returns {..},
    expect="array" returns [..]. Returns None if nothing balanced is found.
    """
    if not text:
        return None

    # Strip chain-of-thought style blocks first.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # Strip markdown fences (``` or ```json). Keep inner content.
    text = re.sub(r"```(?:json|JSON)?\s*\n?", "", text)
    text = text.replace("```", "")

    open_ch, close_ch = ("{", "}") if expect == "object" else ("[", "]")
    start = text.find(open_ch)
    if start == -1:
        return None

    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _loads_tolerant(blob: str) -> Any:
    """json.loads with a single-quote → double-quote retry for Python-style
    dicts that Qwen/DeepSeek sometimes emit."""
    try:
        return json.loads(blob)
    except Exception:
        # Only swap quotes that aren't already escaped or adjacent to a
        # quote char — heuristic but good enough for the common failure
        # mode ({'key': 'value'} style).
        swapped = re.sub(r"(?<!\\)'", '"', blob)
        return json.loads(swapped)


# Known LiteLLM provider prefixes. If a model name starts with any of these
# followed by "/", we assume the user already routed it explicitly.
_KNOWN_PROVIDER_PREFIXES = (
    "openai/", "azure/", "anthropic/", "gemini/", "vertex_ai/",
    "deepseek/", "mistral/", "together_ai/", "groq/", "fireworks_ai/",
    "openrouter/", "ollama/", "huggingface/", "bedrock/", "cohere/",
    "replicate/", "perplexity/", "xai/", "cerebras/", "sambanova/",
    "nvidia_nim/", "nscale/", "watsonx/", "ai21/", "palm/",
)


def _normalize_model_name(model: str, base_url: str | None) -> str:
    """Auto-prepend ``openai/`` when the model has no provider prefix.

    LiteLLM requires a ``provider/`` prefix to know how to route. A bare
    ``MiniMax-M2.7`` or ``qwen2.5-72b-instruct`` raises BadRequestError
    ("LLM Provider NOT provided"). When the user has configured a custom
    ``base_url`` they almost certainly want the OpenAI-compatible
    ``/v1/chat/completions`` shape (MiniMax / Qwen / Kimi / DeepSeek
    custom endpoints / local vLLM / LM Studio all fit), so default the
    prefix to ``openai/``. If the name already carries a known prefix,
    leave it untouched.
    """
    if not model:
        return model
    m = model.strip()
    lower = m.lower()
    if any(lower.startswith(p) for p in _KNOWN_PROVIDER_PREFIXES):
        return m
    # Bare name. Auto-prefix so LiteLLM routes via the OpenAI-compatible
    # path. This is correct for any custom base_url and harmless for the
    # OpenAI native endpoint (same path).
    normalized = f"openai/{m}"
    if base_url:
        logger.info(
            f"LLMClient: model {m!r} has no provider prefix; treating as "
            f"{normalized!r} (OpenAI-compatible endpoint at {base_url})."
        )
    else:
        logger.info(
            f"LLMClient: model {m!r} has no provider prefix; treating as {normalized!r}."
        )
    return normalized


class LLMClient:
    """Thin wrapper around `litellm.completion`.

    Construct once per Executor / Reranker and reuse; the underlying
    LiteLLM calls are stateless so this is just a config bundle.
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        seed: int | None = None,
        extra: Mapping[str, Any] | None = None,
    ):
        if not model:
            raise ValueError("LLMClient requires a non-empty model name")
        self.model = _normalize_model_name(model, base_url)
        self.api_key = api_key or None
        self.base_url = base_url or None
        self.max_tokens = int(max_tokens) if max_tokens else None
        self.temperature = float(temperature) if temperature is not None else None
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.seed = int(seed) if seed is not None else None
        self.extra = dict(extra or {})

    @classmethod
    def from_config(cls, llm_cfg: DictConfig | Mapping) -> "LLMClient":
        """Build from the `config.llm` section.

        Schema is flat: `llm.model`, `llm.max_tokens`, `llm.temperature`, etc.
        """
        if isinstance(llm_cfg, DictConfig):
            cfg = OmegaConf.to_container(llm_cfg, resolve=True) or {}
        else:
            cfg = dict(llm_cfg)

        api = cfg.get("api") or {}

        model = cfg.get("model")
        if not model:
            raise ValueError(
                "config.llm.model is required — set it to a LiteLLM-style "
                "identifier like 'openai/gpt-4o-mini', 'anthropic/claude-sonnet-4-6', "
                "'gemini/gemini-2.0-flash', 'deepseek/deepseek-chat', or "
                "'ollama/qwen2.5:7b-instruct'."
            )

        max_tokens = cfg.get("max_tokens")
        try:
            max_tokens = int(max_tokens) if max_tokens not in (None, "") else None
        except (TypeError, ValueError):
            max_tokens = None

        temperature = cfg.get("temperature")
        try:
            temperature = float(temperature) if temperature not in (None, "") else None
        except (TypeError, ValueError):
            temperature = None

        timeout = float(cfg.get("timeout", 60.0) or 60.0)
        max_retries = int(cfg.get("max_retries", 3) or 3)
        seed = cfg.get("seed")

        extra = {}
        for k in ("top_p", "frequency_penalty", "presence_penalty"):
            if k in cfg:
                extra[k] = cfg[k]

        return cls(
            model=str(model),
            api_key=api.get("key") or cfg.get("api_key"),
            base_url=api.get("base_url") or cfg.get("base_url"),
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            max_retries=max_retries,
            seed=seed,
            extra=extra,
        )

    # ---- low-level ------------------------------------------------------

    def _build_kwargs(self, *, json_mode: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "timeout": self.timeout,
            "num_retries": self.max_retries,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["api_base"] = self.base_url
        if self.seed is not None:
            kwargs["seed"] = self.seed

        # Reasoning models have different param names and reject the usual
        # temperature/top_p knobs.
        if _is_reasoning_model(self.model):
            if self.max_tokens:
                kwargs["max_completion_tokens"] = self.max_tokens
            # intentionally skip temperature / top_p
        else:
            if self.max_tokens:
                kwargs["max_tokens"] = self.max_tokens
            if self.temperature is not None:
                kwargs["temperature"] = self.temperature
            for k, v in self.extra.items():
                if v is not None:
                    kwargs[k] = v

        if json_mode and _supports_json_mode(self.model):
            kwargs["response_format"] = {"type": "json_object"}

        return kwargs

    def complete(
        self,
        *,
        system: str,
        user: str,
        json_mode: bool = False,
    ) -> str:
        """Single-turn completion. Returns the assistant message text."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        try:
            resp = litellm.completion(
                messages=messages,
                **self._build_kwargs(json_mode=json_mode),
            )
        except Exception as exc:
            # Runtime auto-detection: some providers silently expose
            # reasoning models under arbitrary names (kimi-thinking,
            # grok-4, minimax-m2-reasoning, a local finetune, ...) and
            # reject temperature / top_p at call time. Cache the model
            # once, retry without those knobs, and every subsequent call
            # goes through the reasoning path from the start.
            if (
                self.model not in _TEMPERATURE_BLOCKED_MODELS
                and _looks_like_temperature_rejection(exc)
            ):
                logger.warning(
                    f"LLMClient: {self.model!r} rejected temperature — "
                    f"treating as a reasoning model for the rest of this run."
                )
                _TEMPERATURE_BLOCKED_MODELS.add(self.model)
                resp = litellm.completion(
                    messages=messages,
                    **self._build_kwargs(json_mode=json_mode),
                )
            else:
                raise
        # LiteLLM normalises the response to the OpenAI shape.
        try:
            return resp.choices[0].message.content or ""
        except (AttributeError, IndexError):
            return ""

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        expect: str = "object",
    ) -> Any | None:
        """Completion + robust JSON extraction. Returns parsed JSON
        (dict/list) or None on any failure — the caller handles fallback."""
        raw = self.complete(system=system, user=user, json_mode=True)
        if not raw:
            return None
        blob = _extract_json_blob(raw, expect=expect)
        if blob is None:
            # Sometimes the model skips the outer braces entirely (e.g.
            # returns just a bare list when asked for an object). Try
            # the opposite shape once.
            other = "array" if expect == "object" else "object"
            blob = _extract_json_blob(raw, expect=other)
        if blob is None:
            logger.debug(f"LLMClient: no JSON blob found in output: {raw[:200]!r}")
            return None
        try:
            return _loads_tolerant(blob)
        except Exception as exc:
            logger.debug(f"LLMClient: JSON parse failed ({exc}): {blob[:200]!r}")
            return None

    # ---- tokenisation ---------------------------------------------------

    def token_count(self, text: str) -> int:
        """Provider-aware token count. Falls back to a tiktoken-compatible
        cl100k approximation if LiteLLM can't resolve the model."""
        if not text:
            return 0
        try:
            return litellm.token_counter(model=self.model, text=text)
        except Exception:
            # Last-resort: 4 chars ≈ 1 token. Only used for budget slicing,
            # so a rough estimate is acceptable.
            return max(1, len(text) // 4)

    def truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """Best-effort token-based truncation. Uses tiktoken via LiteLLM
        when possible for accurate slicing; otherwise falls back to a
        character-count estimate."""
        if not text or max_tokens <= 0:
            return ""
        try:
            import tiktoken

            enc = tiktoken.get_encoding("cl100k_base")
            toks = enc.encode(text)
            if len(toks) <= max_tokens:
                return text
            return enc.decode(toks[:max_tokens])
        except Exception:
            # Character-level fallback.
            approx_chars = max_tokens * 4
            return text[:approx_chars]
