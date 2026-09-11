"""OpenAI backend with the same interface as brain/claude.py (decide / extract_skills / teach_chat / ping).

Uses chat completions with strict JSON-schema structured outputs, so every reply is
already the shape the engine expects. Screen control (computer use) stays on Claude.
"""
from __future__ import annotations

import json
import os
from typing import Any

from ..config import Settings
from .claude import DECISION_SCHEMA, SKILLS_SCHEMA, DECISION_SYSTEM, TEACH_SYSTEM

EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def _client(settings: Settings):
    from openai import OpenAI
    key = settings.openai_api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OpenAI API key is not set (Settings -> AI)")
    from ..net import openai_http_client
    hc = openai_http_client(settings)
    # Called from the trading loop: a long retry ladder here blocks every other symbol's stop.
    kw = {"api_key": key, "timeout": 120.0, "max_retries": 2}
    return OpenAI(http_client=hc, **kw) if hc else OpenAI(**kw)


class OpenAIBrain:
    name = "openai"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = _client(settings)
        self.model = settings.openai_model or "gpt-5"

    def _json(self, system: str, user_messages: list[dict[str, str]], schema: dict[str, Any], name: str,
              max_tokens: int = 4000, effort: str | None = None,
              timeout: float | None = None) -> dict[str, Any]:
        kw: dict[str, Any] = dict(
            model=self.model,
            messages=[{"role": "system", "content": system}, *user_messages],
            response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
            max_completion_tokens=max_tokens,
        )
        if timeout:
            kw["timeout"] = timeout
        eff = EFFORT_MAP.get(effort or self.settings.effort)
        if eff and (self.model.startswith("gpt-5") or self.model.startswith("o")):
            kw["reasoning_effort"] = eff
        resp = self.client.chat.completions.create(**kw)
        choice = resp.choices[0]
        if getattr(choice.message, "refusal", None):
            raise RuntimeError(f"model refused: {choice.message.refusal}")
        if choice.finish_reason == "length":
            # On a reasoning model max_completion_tokens covers the reasoning too, so a short
            # budget returns an empty answer. Returning "{}" here made that look like a decision.
            raise RuntimeError("the model ran out of output budget before finishing its answer "
                               "(raise max_completion_tokens or lower the effort level)")
        content = (choice.message.content or "").strip()
        if not content:
            raise RuntimeError(f"the model returned no text (finish_reason={choice.finish_reason})")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"the model's answer was not valid JSON: {exc}") from exc

    # ------------------------------------------------------------ same surface as Brain
    def decide(self, symbol, snapshot, regime, signals, position, skills, knowledge) -> dict[str, Any]:
        skill_text = "\n".join(f"- [{s['category']}] {s['name']}: {s['rule']}" for s in skills) or "(none yet)"
        payload = {"symbol": symbol, "timeframe": self.settings.timeframe, "regime": regime, "snapshot": snapshot,
                   "rule_signals": signals, "open_position": position}
        user = (f"SKILLS:\n{skill_text}\n\n"
                + ("RELEVANT NOTES FROM THE LIBRARY:\n" + "\n---\n".join(knowledge) + "\n\n" if knowledge else "")
                + f"MARKET:\n{json.dumps(payload, ensure_ascii=False)}")
        out = self._json(DECISION_SYSTEM, [{"role": "user", "content": user}], DECISION_SCHEMA, "decision", 8000, timeout=90.0)
        out["confidence"] = max(0.0, min(1.0, float(out.get("confidence", 0))))
        out["stop_distance_atr"] = max(1.0, min(4.0, float(out.get("stop_distance_atr", 2))))
        return out

    def extract_skills(self, title: str, text: str, max_chars: int = 120_000) -> list[dict[str, Any]]:
        chunks = [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or [""]
        found, seen = [], set()
        for i, chunk in enumerate(chunks):
            prompt = (f"Source: {title} (part {i + 1}/{len(chunks)}).\n"
                      "Extract every actionable trading rule in this text - entries, exits, risk, regime, psychology.\n"
                      "Skip generic advice with no concrete condition. Keep the author's thresholds.\n\n" + chunk)
            out = self._json("You extract trading rules from books and articles for a rule-following trading bot.",
                             [{"role": "user", "content": prompt}], SKILLS_SCHEMA, "skills", 16000, timeout=600.0)
            for s in out.get("skills", []):
                k = s["name"].strip().lower()
                if k not in seen:
                    seen.add(k); found.append(s)
        return found

    def teach_chat(self, history, skills):
        skill_text = "\n".join(f"- {s['name']}: {s['rule']}" for s in skills[:80]) or "(none)"
        schema = {"type": "object", "properties": {"reply": {"type": "string"}, "skills": SKILLS_SCHEMA["properties"]["skills"]},
                  "required": ["reply", "skills"], "additionalProperties": False}
        out = self._json(TEACH_SYSTEM + f"\n\nCurrent skills:\n{skill_text}",
                         [{"role": m["role"], "content": m["content"]} for m in history], schema, "teach", 3000, effort="medium")
        return out.get("reply", ""), out.get("skills", [])

    def ping(self) -> str:
        """Prove the key works AND that the model answers. Returning "" was worse than useless:
        the self-test printed "OpenAI (gpt-5) replied: " with nothing after it and called that a
        pass, so a broken key or a model that never produces content reported green.

        max_completion_tokens covers the REASONING on a gpt-5-class model, so 20 was never
        enough to leave room for a word - the budget went entirely on thinking."""
        kw = dict(model=self.model,
                  messages=[{"role": "user", "content": "Reply with the single word OK."}],
                  max_completion_tokens=2000)
        if self.model.startswith("gpt-5") or self.model.startswith("o"):
            kw["reasoning_effort"] = "low"      # a ping needs no thinking; keep it cheap
        resp = self.client.chat.completions.create(**kw)
        choice = resp.choices[0]
        text = (choice.message.content or "").strip()
        if not text:
            raise RuntimeError(
                f"{self.model} returned no text (finish_reason={choice.finish_reason}). "
                "On a reasoning model this usually means the token budget went entirely on "
                "reasoning - or the key is valid but the model is refusing.")
        return text
