import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any, Optional, Tuple

try:
    from openai import AsyncOpenAI
except ImportError as e:
    raise ImportError(f"Missing dependency: {e.name}. Please install it via pip.") from e


@dataclass(frozen=True)
class ModelConfig:
    model_id: str
    temperature: float
    base_url: str
    api_key: str
    extra_body: Optional[dict[str, Any]]
    max_tokens: int


def expand_env_vars(value: str) -> str:
    return re.sub(
        r"\$\{([A-Z0-9_]+)\}",
        lambda m: os.environ.get(m.group(1)) or (_ for _ in ()).throw(RuntimeError(f"Env var {m.group(1)} not set")),
        value
    )


async def call_chat_completion(
    *, model_cfg: ModelConfig, prompt: str, max_tokens: int, retries: int
) -> Tuple[str, dict[str, int]]:
    client = AsyncOpenAI(
        api_key=model_cfg.api_key, base_url=model_cfg.base_url, max_retries=0, timeout=120.0
    )
    
    last_err = None
    for attempt in range(retries):
        try:
            stream = await client.chat.completions.create(
                model=model_cfg.model_id,
                messages=[{"role": "user", "content": prompt}],
                temperature=model_cfg.temperature,
                max_tokens=max_tokens,
                extra_body=model_cfg.extra_body,
                stream=True,
                stream_options={"include_usage": True},
            )
            
            content_parts = []
            usage = {"prompt_tokens": 0, "completion_tokens": 0}
            
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    content_parts.append(chunk.choices[0].delta.content)
                if chunk.usage:
                    usage["prompt_tokens"] = chunk.usage.prompt_tokens or 0
                    usage["completion_tokens"] = chunk.usage.completion_tokens or 0
            
            content = "".join(content_parts)
            return content, usage
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))

    raise RuntimeError(f"Chat completion failed after {retries} retries: {last_err}") from last_err
