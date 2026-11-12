"""Self-hosted LLM connection: UI-editable config + an OpenAI-compatible client.

Any server that speaks ``POST {base_url}/chat/completions`` works - Ollama
(``http://localhost:11434/v1``), LM Studio (``:1234/v1``), vLLM, llama.cpp
``server``, LocalAI, or a hosted OpenAI-compatible endpoint.

Config lives in ``app_settings`` (key/value, edited on the Settings page), not
in ``.env``: it is user-facing configuration, and the CLI, web app and nightly
job all read the same rows.  The API key is optional (most local servers
ignore it) and is never rendered back to the browser.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy.orm import Session

from app.db.models import AppSetting


class LLMError(RuntimeError):
    """The model could not produce a usable answer (bad reply, bad JSON...)."""


class LLMUnreachable(LLMError):
    """Transport-level failure: connection refused, timeout, HTTP 5xx/401."""


class LLMTruncated(LLMError):
    """The model hit its output budget (``finish_reason=length``) mid-answer.

    A per-request problem, not an unreachable server - so it must not stop
    the rest of a screen from using the model.
    """


#: Ceiling for ``max_tokens`` (form input and the automatic retry).
MAX_TOKENS_CEILING = 32000


@dataclass(frozen=True)
class LLMConfig:
    enabled: bool = False
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: Decimal = Decimal("0.2")
    #: Output budget per answer.  Generous on purpose: reasoning models spend
    #: it on thinking before the JSON, and 1500 truncated them
    #: (``finish_reason=length``).  Only the answer's real length is generated.
    max_tokens: int = 16000
    timeout_seconds: int = 180
    #: Also use the model for every ticker in the nightly screen (one call each).
    use_in_screen: bool = True
    #: Wall-clock AI time the nightly screen may spend.  Names are read in
    #: priority order (held/watched, emerging, shortlist) until it runs out;
    #: the rest get the rules report.  See ``screener.screen_universe``.
    ai_minutes_per_night: int = 60

    @property
    def ready(self) -> bool:
        """Enabled *and* pointed at something - the only state that calls out."""
        return self.enabled and bool(self.base_url) and bool(self.model)


#: ``app_settings`` key for each config field.
KEYS = {
    "enabled": "llm_enabled",
    "base_url": "llm_base_url",
    "model": "llm_model",
    "api_key": "llm_api_key",
    "temperature": "llm_temperature",
    "max_tokens": "llm_max_tokens",
    "timeout_seconds": "llm_timeout_seconds",
    "use_in_screen": "llm_use_in_screen",
    "ai_minutes_per_night": "llm_ai_minutes_per_night",
}


def load_llm_config(session: Session) -> LLMConfig:
    rows = {s.key: s.value for s in session.query(AppSetting)
            .filter(AppSetting.key.in_(KEYS.values()))}
    default = LLMConfig()

    def get(field: str) -> str | None:
        return rows.get(KEYS[field])

    return LLMConfig(
        enabled=get("enabled") == "1",
        base_url=(get("base_url") or "").strip().rstrip("/"),
        model=(get("model") or "").strip(),
        api_key=get("api_key") or "",
        temperature=_parse_decimal(get("temperature"), default.temperature,
                                   Decimal("0"), Decimal("2")),
        max_tokens=_parse_int(get("max_tokens"), default.max_tokens, 64, 32000),
        timeout_seconds=_parse_int(get("timeout_seconds"), default.timeout_seconds, 5, 1800),
        use_in_screen=get("use_in_screen") != "0",
        ai_minutes_per_night=_parse_int(get("ai_minutes_per_night"),
                                        default.ai_minutes_per_night, 1, 24 * 60),
    )


def save_llm_config(session: Session, config: LLMConfig) -> None:
    """Upsert every field (caller commits)."""
    values = {
        "enabled": "1" if config.enabled else "0",
        "base_url": config.base_url.strip().rstrip("/"),
        "model": config.model.strip(),
        "api_key": config.api_key,
        "temperature": str(config.temperature),
        "max_tokens": str(config.max_tokens),
        "timeout_seconds": str(config.timeout_seconds),
        "use_in_screen": "1" if config.use_in_screen else "0",
        "ai_minutes_per_night": str(config.ai_minutes_per_night),
    }
    now = dt.datetime.now(dt.timezone.utc)
    for field, value in values.items():
        row = session.query(AppSetting).filter_by(key=KEYS[field]).first()
        if row is None:
            row = AppSetting(key=KEYS[field], value=value)
            session.add(row)
        row.value = value
        row.updated_at = now


def config_from_form(form: dict, stored: LLMConfig) -> LLMConfig:
    """Build a config from Settings-form fields.

    A blank API key keeps the stored one (the field is never pre-filled, so
    blank means "unchanged"); ``llm_clear_api_key`` removes it.
    """
    api_key = (form.get("llm_api_key") or "").strip() or stored.api_key
    if form.get("llm_clear_api_key"):
        api_key = ""
    return replace(
        stored,
        enabled=bool(form.get("llm_enabled")),
        base_url=(form.get("llm_base_url") or "").strip().rstrip("/"),
        model=(form.get("llm_model") or "").strip(),
        api_key=api_key,
        temperature=_parse_decimal(form.get("llm_temperature"), stored.temperature,
                                   Decimal("0"), Decimal("2")),
        max_tokens=_parse_int(form.get("llm_max_tokens"), stored.max_tokens, 64,
                              MAX_TOKENS_CEILING),
        timeout_seconds=_parse_int(form.get("llm_timeout_seconds"), stored.timeout_seconds,
                                   5, 1800),
        use_in_screen=bool(form.get("llm_use_in_screen")),
        ai_minutes_per_night=_parse_int(form.get("llm_ai_minutes_per_night"),
                                        stored.ai_minutes_per_night, 1, 24 * 60),
    )


class LLMClient:
    """Minimal OpenAI-compatible chat client (sync; httpx)."""

    def __init__(self, config: LLMConfig, *, transport: httpx.BaseTransport | None = None):
        self.config = config
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._http = httpx.Client(base_url=config.base_url, headers=headers,
                                  timeout=float(config.timeout_seconds), transport=transport)

    def close(self) -> None:
        self._http.close()

    def chat(self, messages: list[dict], *, json_mode: bool = True) -> str:
        """Return the assistant message text.

        Asks for ``response_format: json_object`` first; servers that reject
        it (older LM Studio, some llama.cpp builds) get one retry without.
        An answer cut off by the output budget (reasoning models spend it on
        thinking first) gets one retry with double the budget.
        """
        budget = self.config.max_tokens
        try:
            return self._chat_once(messages, json_mode=json_mode, max_tokens=budget)
        except LLMTruncated:
            bigger = min(budget * 2, MAX_TOKENS_CEILING)
            if bigger <= budget:
                raise
            try:
                return self._chat_once(messages, json_mode=json_mode, max_tokens=bigger)
            except LLMTruncated as exc:
                raise LLMTruncated(
                    f"model ran out of output budget even at {bigger} tokens "
                    f"(max tokens is {budget}) - raise 'max tokens' under "
                    f"Settings -> AI analyst"
                ) from exc

    def _chat_once(self, messages: list[dict], *, json_mode: bool, max_tokens: int) -> str:
        body: dict = {
            "model": self.config.model,
            "messages": messages,
            "temperature": float(self.config.temperature),
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        response = self._post("/chat/completions", body)
        if json_mode and response.status_code in (400, 422):
            body.pop("response_format")
            response = self._post("/chat/completions", body)
        if response.status_code >= 400:
            raise _http_error(response)
        try:
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response shape from {self.config.base_url}") from exc
        if choice.get("finish_reason") == "length":
            raise LLMTruncated(f"answer cut off at max_tokens={max_tokens}")
        if not isinstance(content, str) or not content.strip():
            raise LLMError("model returned an empty message")
        return content

    def list_models(self) -> list[str]:
        """``GET /models`` ids - used by the Settings "test connection" button."""
        try:
            response = self._http.get("/models")
        except httpx.HTTPError as exc:
            raise LLMUnreachable(f"cannot reach {self.config.base_url}: {exc}") from exc
        if response.status_code >= 400:
            raise _http_error(response)
        try:
            return sorted(str(m["id"]) for m in response.json().get("data", []))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise LLMError("server answered /models with an unexpected shape") from exc

    def _post(self, path: str, body: dict) -> httpx.Response:
        try:
            return self._http.post(path, json=body)
        except httpx.TimeoutException as exc:
            raise LLMUnreachable(
                f"model did not answer within {self.config.timeout_seconds}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMUnreachable(f"cannot reach {self.config.base_url}: {exc}") from exc


def extract_json(text: str) -> dict:
    """Pull the JSON object out of a model reply.

    Tolerates what local models actually emit: ``<think>...</think>``
    reasoning preambles, ```json fences, and chatter around the object.
    """
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise LLMError("model reply contained no JSON object")
    try:
        parsed = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"model reply was not valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise LLMError("model reply JSON was not an object")
    return parsed


#: Error bodies that mean "the output budget ran out", whatever the status:
#: some gateways (e.g. ones enforcing structured output) report it as a 5xx.
_TRUNCATION_MARKERS = ("finish_reason=length", "finish_reason: length",
                       "structured_output_failed", "increase the output budget")


def _http_error(response: httpx.Response) -> LLMError:
    detail = response.text[:200].strip()
    message = f"HTTP {response.status_code} from {response.request.url}: {detail}"
    if any(marker in response.text for marker in _TRUNCATION_MARKERS):
        return LLMTruncated(message)
    # Auth / server faults won't fix themselves on the next ticker; bad
    # requests (e.g. unknown model name) are also config problems.
    if response.status_code in (401, 403, 404) or response.status_code >= 500:
        return LLMUnreachable(message)
    return LLMError(message)


def _parse_int(raw: object, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(raw).strip())))
    except (TypeError, ValueError):
        return default


def _parse_decimal(raw: object, default: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    try:
        return max(lo, min(hi, Decimal(str(raw).strip())))
    except (InvalidOperation, TypeError, ValueError):
        return default
