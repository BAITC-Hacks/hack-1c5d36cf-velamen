"""One configured provider, bounded retries, validated caches and run accounting."""
import asyncio
import json
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from openai import AsyncOpenAI, APIConnectionError, APIStatusError, APITimeoutError, LengthFinishReasonError
from pydantic import BaseModel, ValidationError

from .config import Settings, load_settings
from .models import Model, SCHEMA_VERSION
from .storage import cache_key, read_json, write_json

PROMPT_VERSION = "2"
PROMPTS = Path(__file__).parent / "prompts"


class ModelError(RuntimeError):
    pass


class TruncatedOutput(ModelError):
    pass


def error_ru(exc: Exception) -> str:
    if isinstance(exc, APITimeoutError):
        return "OpenAI: превышено время ожидания. Повторите запрос."
    if isinstance(exc, APIConnectionError):
        return "OpenAI: не удалось подключиться; проверьте сеть и OPENAI_BASE_URL."
    if isinstance(exc, APIStatusError):
        code = getattr(exc, "code", None)
        if exc.status_code == 401:
            return "OpenAI: ошибка авторизации (401); проверьте OPENAI_API_KEY."
        if code in {"insufficient_quota", "billing_hard_limit_reached", "billing_not_active"}:
            return "OpenAI: недостаточно квоты или средств; проверьте оплату и лимиты проекта."
        if exc.status_code in {403, 404}:
            return "OpenAI: модель недоступна; проверьте доступ проекта и настроенный ID модели."
        return f"OpenAI: ошибка HTTP {exc.status_code}; код {code or 'не указан'}."
    # Never echo provider request bodies or credentials.
    if isinstance(exc, (ModelError, ValueError)):
        return str(exc)
    return f"Ошибка обработки: {type(exc).__name__}."


class Runtime:
    def __init__(self, settings: Settings | None = None, client=None):
        self.settings = settings or load_settings()
        self.client = client
        self.semaphore = asyncio.Semaphore(self.settings.llm_concurrency)
        self.embedding_lock = asyncio.Lock()
        self.usage = defaultdict(lambda: dict(api_calls=0, cache_hits=0, input_tokens=0, output_tokens=0, errors=0))

    def api(self):
        if self.client is None:
            self.settings.require_key()
            self.client = AsyncOpenAI(api_key=self.settings.openai_api_key.get_secret_value(),
                                     base_url=self.settings.openai_base_url,
                                     timeout=self.settings.llm_timeout_seconds, max_retries=0)
        return self.client

    async def close(self):
        if self.client is not None and hasattr(self.client, "close"):
            await self.client.close()

    async def request(self, stage, operation):
        for attempt in range(3):
            try:
                async with self.semaphore:
                    self.usage[stage]["api_calls"] += 1
                    result = await operation()
                usage = getattr(result, "usage", None)
                if usage:
                    self.usage[stage]["input_tokens"] += getattr(usage, "input_tokens", getattr(usage, "prompt_tokens", 0)) or 0
                    self.usage[stage]["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
                return result
            except (APIStatusError, APIConnectionError) as exc:
                self.usage[stage]["errors"] += 1
                quota = getattr(exc, "code", None) in {"insufficient_quota", "billing_hard_limit_reached", "billing_not_active"}
                transient = isinstance(exc, APIConnectionError) or exc.status_code == 429 or exc.status_code >= 500
                if quota or not transient or attempt == 2:
                    raise ModelError(error_ru(exc)) from exc
                await asyncio.sleep(0.5 * 2 ** attempt)


_runtime = ContextVar("orgdiff_runtime", default=None)


def runtime() -> Runtime:
    current = _runtime.get()
    if current is None:
        current = Runtime()
        _runtime.set(current)
    return current


@contextmanager
def use_runtime(value: Runtime):
    token = _runtime.set(value)
    try:
        yield value
    finally:
        _runtime.reset(token)


async def generate(task: str, payload: dict, output_type: type[BaseModel], *, validate=None, cache=True) -> BaseModel:
    run = runtime()
    settings = run.settings
    prompt = (PROMPTS / f"{task}.md").read_text(encoding="utf-8") if task != "doctor" else "Верни ok=true."
    key = cache_key(SCHEMA_VERSION, PROMPT_VERSION, settings.model_dump(mode="json"), prompt, payload, output_type.model_json_schema())
    path = settings.artifact_dir / "cache" / "responses" / f"{key}.json"
    if cache and path.exists():
        try:
            result = output_type.model_validate(read_json(path)["parsed"])
            if validate:
                validate(result)
            run.usage[task]["cache_hits"] += 1
            return result
        except (ValueError, KeyError, OSError):
            pass
    correction = None
    for attempt in range(2):
        request_payload = dict(payload)
        if correction:
            request_payload["validation_errors"] = correction
        try:
            # Inspect the envelope before the SDK parses JSON: a truncated JSON
            # body otherwise raises ValidationError and hides the incomplete status.
            raw = await run.request(task, lambda: run.api().responses.with_raw_response.parse(
                model=settings.openai_model, instructions=prompt,
                input=json.dumps(request_payload, ensure_ascii=False), text_format=output_type,
                reasoning={"effort": settings.openai_reasoning_effort}, service_tier="default",
                store=False, max_output_tokens=settings.llm_max_output_tokens))
            envelope = raw.json()
            usage = envelope.get("usage") or {}
            run.usage[task]["input_tokens"] += usage.get("input_tokens", 0)
            run.usage[task]["output_tokens"] += usage.get("output_tokens", 0)
            if envelope.get("status") == "incomplete":
                reason = (envelope.get("incomplete_details") or {}).get("reason")
                if reason == "max_output_tokens":
                    raise TruncatedOutput("Ответ обрезан по лимиту выходных токенов.")
                raise ModelError(f"Неполный ответ модели: {reason}.")
            response = raw.parse()
            if any(getattr(part, "type", "") == "refusal" for item in response.output for part in getattr(item, "content", [])):
                raise ModelError("Модель отказалась обрабатывать фрагмент.")
            if response.status != "completed" or response.output_parsed is None:
                raise ModelError("Модель не вернула структурированный результат.")
            result = response.output_parsed
            if validate:
                validate(result)
            if cache:
                write_json(path, {"parsed": result, "raw": response.model_dump(mode="json")})
            return result
        except LengthFinishReasonError as exc:
            raise TruncatedOutput("Ответ обрезан по лимиту выходных токенов.") from exc
        except (ValidationError, ValueError) as exc:
            run.usage[task]["errors"] += 1
            correction = str(exc)
            if attempt:
                raise ModelError(f"Ответ не прошёл проверку схемы/источников: {correction}") from exc
        except ModelError:
            run.usage[task]["errors"] += 1
            raise


class DoctorOutput(Model):
    ok: bool


async def doctor() -> None:
    run = runtime()
    run.settings.require_key()
    print("OPENAI_API_KEY: задан")
    result = await generate("doctor", {"message": "Проверка соединения"}, DoctorOutput, cache=False)
    if not result.ok:
        raise ModelError("LLM: проверка не подтверждена.")
    print(f"LLM: OK ({run.settings.openai_model})")
    response = await run.request("embeddings", lambda: run.api().embeddings.create(
        model=run.settings.openai_embedding_model, input=["Проверка"], encoding_format="float"))
    if not response.data or not response.data[0].embedding:
        raise ModelError("Embeddings: пустой вектор.")
    print(f"Embeddings: OK ({run.settings.openai_embedding_model})")
    print(json.dumps(dict(run.usage), ensure_ascii=False))
