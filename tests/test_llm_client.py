"""Unit tests for LLMClient param translation + JSON extraction."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from auto_read_paper.llm_client import (
    LLMClient,
    _extract_json_blob,
    _is_reasoning_model,
    _loads_tolerant,
    _normalize_model_name,
    _supports_json_mode,
)


# ---- reasoning-model detection -----------------------------------------

@pytest.mark.parametrize("model,expected", [
    ("openai/o1-mini", True),
    ("openai/o3-mini", True),
    ("openai/o4-mini-high", True),
    ("o1", True),
    ("o3", True),
    ("gpt-5", True),
    ("openai/gpt-5-turbo", True),
    # Moonshot / Kimi thinking variants.
    ("openai/kimi-thinking-preview", True),
    ("kimi-thinking-preview", True),
    ("kimi-k2-thinking", True),
    # DeepSeek R1 / reasoner.
    ("deepseek/deepseek-reasoner", True),
    ("openai/deepseek-r1", True),
    # Alibaba QwQ.
    ("openai/qwq-32b-preview", True),
    # Generic *-thinking / *-reasoning suffix (covers future models).
    ("openai/some-future-thinking", True),
    ("openai/minimax-m2-reasoning", True),
    # Non-reasoning chat models stay False.
    ("openai/gpt-4o-mini", False),
    ("openai/gpt-4.5", False),
    ("openai/kimi-latest", False),
    ("openai/moonshot-v1-8k", False),
    ("anthropic/claude-sonnet-4-6", False),
    ("gemini/gemini-2.0-flash", False),
    ("deepseek/deepseek-chat", False),
    ("ollama/qwen2.5:7b-instruct", False),
    ("", False),
])
def test_is_reasoning_model(model, expected):
    assert _is_reasoning_model(model) is expected


@pytest.mark.parametrize("model,expected", [
    ("openai/gpt-4o-mini", True),
    ("anthropic/claude-sonnet-4-6", True),
    ("gemini/gemini-2.0-flash", True),
    ("deepseek/deepseek-chat", True),
    ("ollama/qwen2.5:7b-instruct", False),
    ("huggingface/Qwen/Qwen2.5-32B", False),
    ("", False),
])
def test_supports_json_mode(model, expected):
    assert _supports_json_mode(model) is expected


# ---- model-name normalization -----------------------------------------

@pytest.mark.parametrize("model,base_url,expected", [
    # Already-prefixed names pass through.
    ("openai/gpt-4o-mini", None, "openai/gpt-4o-mini"),
    ("anthropic/claude-sonnet-4-6", None, "anthropic/claude-sonnet-4-6"),
    ("deepseek/deepseek-chat", "https://api.deepseek.com/v1", "deepseek/deepseek-chat"),
    ("ollama/qwen2.5:7b-instruct", "http://localhost:11434/v1", "ollama/qwen2.5:7b-instruct"),
    # Bare names get openai/ prefix (OpenAI-compatible endpoints).
    ("MiniMax-M2.7", "https://api.minimax.chat/v1", "openai/MiniMax-M2.7"),
    ("qwen2.5-72b-instruct", "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "openai/qwen2.5-72b-instruct"),
    ("gpt-4o-mini", None, "openai/gpt-4o-mini"),
    # Whitespace tolerated.
    ("  gpt-4o  ", None, "openai/gpt-4o"),
    # Empty stays empty.
    ("", None, ""),
])
def test_normalize_model_name(model, base_url, expected):
    assert _normalize_model_name(model, base_url) == expected


# ---- JSON extraction ---------------------------------------------------

def test_extract_json_simple_object():
    assert _extract_json_blob('{"a": 1}') == '{"a": 1}'


def test_extract_json_with_prose_preamble():
    raw = 'Sure, here is the answer:\n\n{"key": "value"}\n\nThanks!'
    assert _extract_json_blob(raw) == '{"key": "value"}'


def test_extract_json_with_markdown_fence():
    raw = "```json\n{\"a\": [1, 2]}\n```"
    assert _extract_json_blob(raw) == '{"a": [1, 2]}'


def test_extract_json_with_think_block():
    raw = "<think>let me think about this...</think>\n{\"result\": 42}"
    assert _extract_json_blob(raw) == '{"result": 42}'


def test_extract_json_nested_object():
    raw = '{"outer": {"inner": {"v": 1}}, "b": [1,2,3]}'
    assert _extract_json_blob(raw) == raw


def test_extract_json_ignores_braces_in_strings():
    raw = '{"k": "value with } close brace", "n": 1}'
    assert _extract_json_blob(raw) == raw


def test_extract_json_no_match():
    assert _extract_json_blob("no json here at all") is None


def test_extract_json_array_mode():
    raw = 'Here you go: ["TsingHua", "Peking"]'
    assert _extract_json_blob(raw, expect="array") == '["TsingHua", "Peking"]'


def test_extract_json_unbalanced_returns_none():
    # Truncated — missing closing brace.
    assert _extract_json_blob('{"a": {"b": 1}') is None


def test_loads_tolerant_handles_single_quotes():
    # Qwen / DeepSeek sometimes emit Python-style dicts.
    parsed = _loads_tolerant("{'key': 'value', 'n': 1}")
    assert parsed == {"key": "value", "n": 1}


def test_loads_tolerant_plain_json():
    assert _loads_tolerant('{"a": 1}') == {"a": 1}


# ---- param translation -------------------------------------------------

def test_reasoning_model_maps_max_tokens_and_drops_temperature():
    client = LLMClient(
        model="openai/o3-mini",
        api_key="sk-fake",
        max_tokens=2048,
        temperature=0.7,
    )
    kwargs = client._build_kwargs(json_mode=False)
    assert kwargs["max_completion_tokens"] == 2048
    assert "max_tokens" not in kwargs
    assert "temperature" not in kwargs


def test_non_reasoning_model_keeps_temperature_and_max_tokens():
    client = LLMClient(
        model="openai/gpt-4o-mini",
        api_key="sk-fake",
        max_tokens=2048,
        temperature=0.3,
    )
    kwargs = client._build_kwargs(json_mode=False)
    assert kwargs["max_tokens"] == 2048
    assert kwargs["temperature"] == 0.3
    assert "max_completion_tokens" not in kwargs


def test_json_mode_added_only_for_supported_providers():
    c_deep = LLMClient(model="deepseek/deepseek-chat", api_key="sk-fake")
    c_ollama = LLMClient(model="ollama/qwen2.5:7b", api_key=None)

    assert c_deep._build_kwargs(json_mode=True).get("response_format") == {"type": "json_object"}
    assert "response_format" not in c_ollama._build_kwargs(json_mode=True)


def test_timeout_and_retries_forwarded():
    c = LLMClient(model="openai/gpt-4o-mini", api_key="sk", timeout=42.0, max_retries=5)
    kw = c._build_kwargs(json_mode=False)
    assert kw["timeout"] == 42.0
    assert kw["num_retries"] == 5


def test_base_url_forwarded_as_api_base():
    c = LLMClient(model="openai/gpt-4o-mini", api_key="sk", base_url="http://localhost:11434/v1")
    kw = c._build_kwargs(json_mode=False)
    assert kw["api_base"] == "http://localhost:11434/v1"


def test_seed_forwarded_when_set():
    c = LLMClient(model="openai/gpt-4o-mini", api_key="sk", seed=123)
    kw = c._build_kwargs(json_mode=False)
    assert kw["seed"] == 123


def test_seed_not_forwarded_when_unset():
    c = LLMClient(model="openai/gpt-4o-mini", api_key="sk")
    kw = c._build_kwargs(json_mode=False)
    assert "seed" not in kw


# ---- from_config -------------------------------------------------------

def test_from_config_new_schema():
    cfg = {
        "api": {"key": "sk-new", "base_url": "https://api.deepseek.com/v1"},
        "model": "deepseek/deepseek-chat",
        "max_tokens": 8192,
        "temperature": 0.2,
        "timeout": 90,
        "max_retries": 5,
    }
    c = LLMClient.from_config(cfg)
    assert c.model == "deepseek/deepseek-chat"
    assert c.api_key == "sk-new"
    assert c.base_url == "https://api.deepseek.com/v1"
    assert c.max_tokens == 8192
    assert c.temperature == 0.2
    assert c.timeout == 90.0
    assert c.max_retries == 5


def test_from_config_missing_model_raises():
    with pytest.raises(ValueError, match="config.llm.model is required"):
        LLMClient.from_config({"api": {"key": "sk"}})


# ---- complete_json dispatch --------------------------------------------

def _mock_completion(content: str) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def test_complete_json_parses_clean_output():
    c = LLMClient(model="openai/gpt-4o-mini", api_key="sk")
    with patch("auto_read_paper.llm_client.litellm.completion",
               return_value=_mock_completion('{"a": 1}')):
        result = c.complete_json(system="sys", user="usr")
    assert result == {"a": 1}


def test_complete_json_handles_markdown_fenced_output():
    c = LLMClient(model="openai/gpt-4o-mini", api_key="sk")
    wrapped = "Here is my answer:\n```json\n{\"score\": 8}\n```"
    with patch("auto_read_paper.llm_client.litellm.completion",
               return_value=_mock_completion(wrapped)):
        result = c.complete_json(system="sys", user="usr")
    assert result == {"score": 8}


def test_complete_json_returns_none_on_garbage():
    c = LLMClient(model="openai/gpt-4o-mini", api_key="sk")
    with patch("auto_read_paper.llm_client.litellm.completion",
               return_value=_mock_completion("I'm sorry, I cannot answer this.")):
        result = c.complete_json(system="sys", user="usr")
    assert result is None


def test_complete_json_handles_array_expect():
    c = LLMClient(model="openai/gpt-4o-mini", api_key="sk")
    with patch("auto_read_paper.llm_client.litellm.completion",
               return_value=_mock_completion('["A", "B"]')):
        result = c.complete_json(system="sys", user="usr", expect="array")
    assert result == ["A", "B"]


# ---- runtime reasoning-model auto-detection ---------------------------

def test_temperature_rejection_triggers_retry_and_caches_model():
    """Unknown model that rejects temperature at call time should: retry
    without temperature, succeed, and stay cached so subsequent calls skip
    it from the start."""
    from auto_read_paper.llm_client import _TEMPERATURE_BLOCKED_MODELS

    model_name = "openai/mystery-model-xyz-42"
    _TEMPERATURE_BLOCKED_MODELS.discard(model_name)  # clean slate

    c = LLMClient(model=model_name, api_key="sk", temperature=0.3, max_tokens=1024)

    # First call: fail with the Moonshot-style 400, then succeed on retry.
    rejection = Exception(
        "litellm.BadRequestError: OpenAIException - "
        "invalid temperature: only 1 is allowed for this model"
    )
    success = _mock_completion('{"ok": true}')

    with patch(
        "auto_read_paper.llm_client.litellm.completion",
        side_effect=[rejection, success],
    ) as mock_completion:
        result = c.complete_json(system="sys", user="usr")

    assert result == {"ok": True}
    assert mock_completion.call_count == 2

    # First call had temperature; second (retry) must have dropped it.
    first_kwargs = mock_completion.call_args_list[0].kwargs
    second_kwargs = mock_completion.call_args_list[1].kwargs
    assert first_kwargs.get("temperature") == 0.3
    assert "temperature" not in second_kwargs
    # And switched to reasoning-style max_completion_tokens.
    assert second_kwargs.get("max_completion_tokens") == 1024
    assert "max_tokens" not in second_kwargs

    # Model is now cached — the next client for the same model should not
    # send temperature in the first place.
    assert model_name in _TEMPERATURE_BLOCKED_MODELS
    c2 = LLMClient(model=model_name, api_key="sk", temperature=0.3, max_tokens=1024)
    kw = c2._build_kwargs(json_mode=False)
    assert "temperature" not in kw
    assert kw.get("max_completion_tokens") == 1024

    _TEMPERATURE_BLOCKED_MODELS.discard(model_name)  # cleanup


def test_non_temperature_errors_are_not_swallowed():
    """Errors unrelated to the temperature param must propagate as-is."""
    from auto_read_paper.llm_client import _TEMPERATURE_BLOCKED_MODELS

    model_name = "openai/some-other-model-abc"
    _TEMPERATURE_BLOCKED_MODELS.discard(model_name)

    c = LLMClient(model=model_name, api_key="sk", temperature=0.3)

    boom = RuntimeError("503 upstream unavailable")
    with patch(
        "auto_read_paper.llm_client.litellm.completion",
        side_effect=boom,
    ) as mock_completion:
        with pytest.raises(RuntimeError, match="upstream unavailable"):
            c.complete(system="sys", user="usr")

    assert mock_completion.call_count == 1  # no retry
    assert model_name not in _TEMPERATURE_BLOCKED_MODELS
