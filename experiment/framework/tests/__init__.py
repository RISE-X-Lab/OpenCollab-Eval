from opencollab.application.event_bus import EventBus
from opencollab.bootstrap.context_builder import ContextBuilder, SpawnConfig
from opencollab.bootstrap.team_config import TeamConfig


def prompt_context(team: TeamConfig) -> ContextBuilder:
    return ContextBuilder(
        team,
        SpawnConfig(
            model="m", provider="openai", api_key="k", base_url=None,
            llm_timeout=600.0, tracer=None, event_bus=EventBus(None),
            permission_policy=None,
        ),
    )
