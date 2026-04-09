from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from deep_researcher.config import AgentConfig


def make_openai_model(config: AgentConfig) -> OpenAIModel:
    provider = OpenAIProvider(base_url=config.base_url, api_key=config.api_key)
    return OpenAIModel(config.model, provider=provider)


def make_text_agent(config: AgentConfig, system_prompt: str) -> Agent[None, str]:
    return Agent(
        model=make_openai_model(config),
        output_type=str,
        system_prompt=system_prompt,
    )


def make_structured_agent[T: BaseModel](
    config: AgentConfig, result_type: type[T], system_prompt: str
) -> Agent[None, T]:
    return Agent(
        model=make_openai_model(config),
        output_type=result_type,
        system_prompt=system_prompt,
    )
