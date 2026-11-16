from __future__ import annotations

from typing import Any

from register_core.verify.base import Verifier
from register_core.verify.noop import NoopVerifier


def get_verifier(name: str, **kwargs: Any) -> Verifier:
    key = (name or "noop").strip().lower()
    if key in ("", "noop", "none", "skip"):
        return NoopVerifier()
    if key in ("mimo", "mimo_tts", "tts"):
        from register_core.verify.mimo_tts import MimoTtsVerifier

        return MimoTtsVerifier(**kwargs)
    if key in ("grok", "grok_chat", "chat"):
        from register_core.verify.grok_chat import GrokChatVerifier

        return GrokChatVerifier(**kwargs)
    if key in ("chatgpt", "openai", "chatgpt_token", "openai_token"):
        from register_core.verify.chatgpt_token import ChatGPTTokenVerifier

        return ChatGPTTokenVerifier(**kwargs)
    if key in ("typesafe", "jev", "typesafe-ai", "typesafe.ai", "typesafe_key"):
        from register_core.verify.typesafe_key import TypesafeKeyVerifier

        return TypesafeKeyVerifier(**kwargs)
    if key in ("nace", "nace-ai", "nace.ai", "drex", "nace_key"):
        from register_core.verify.nace_key import NaceKeyVerifier

        return NaceKeyVerifier(**kwargs)
    if key in ("zai", "z-ai", "z.ai", "zcode", "zai_hub", "zai-hub"):
        from register_core.verify.zai_hub import ZaiHubVerifier

        return ZaiHubVerifier(**kwargs)
    raise KeyError(f"unknown verifier: {name!r}")
