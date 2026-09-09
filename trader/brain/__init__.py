"""AI backends. ``make_brain(settings)`` returns the provider the user picked."""
from __future__ import annotations

from ..config import Settings


def make_brain(settings: Settings):
    if settings.ai_provider == "openai":
        from .openai_brain import OpenAIBrain
        return OpenAIBrain(settings)
    from .claude import Brain
    return Brain(settings)


def claude_client(settings: Settings):
    """The Anthropic client for screen control, whichever provider makes trading decisions."""
    from .claude import _client
    return _client(settings)
