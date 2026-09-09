"""Claude wrapper: market decisions, skill extraction from documents, and a teaching chat.

Every call goes through one client, one model (settings.model, default claude-opus-5),
adaptive thinking (the default on that model), and the server-side refusal fallback so
a refused request is retried on the fallback route instead of silently returning nothing.
"""
from __future__ import annotations

import json
import os
from typing import Any

import anthropic

from ..config import Settings

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["buy", "sell", "hold", "close"]},
        "confidence": {"type": "number", "description": "0 to 1"},
        "reason": {"type": "string", "description": "two or three sentences, naming the skills applied"},
        "stop_distance_atr": {"type": "number", "description": "stop distance in ATR multiples, 1 to 4"},
        "skills_used": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action", "confidence", "reason", "stop_distance_atr", "skills_used"],
    "additionalProperties": False,
}

SKILLS_SCHEMA = {
    "type": "object",
    "properties": {
        "skills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "short, unique, 3-8 words"},
                    "category": {"type": "string", "enum": ["risk", "entry", "exit", "regime", "psychology", "market"]},
                    "rule": {"type": "string", "description": "the rule as an instruction a trading bot can apply, with concrete thresholds where the source gives them"},
                    "evidence": {"type": "string", "description": "the sentence or two from the source this comes from"},
                },
                "required": ["name", "category", "rule", "evidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["skills"],
    "additionalProperties": False,
}

DECISION_SYSTEM = """You are the decision layer of a personal trading bot. You are given a market snapshot
(indicators on the trading timeframe), the detected regime, the rule-based signals the bot's own strategies
produced, the open position on this symbol if any, and the bot's SKILLS - rules learned from books, articles
and the owner. Apply the skills. Be conservative: 'hold' is the right answer most of the time, and a trade
is only worth taking when several independent things line up. Never suggest a size - the risk manager sizes
every trade. Answer only through the JSON schema."""

TEACH_SYSTEM = """You are the learning side of a personal trading bot. The owner teaches you in plain
language (Persian or English). Reply in the owner's language. When they state a rule you should trade by,
restate it precisely as one or more skills and ask a clarifying question only if a threshold is genuinely
missing. Keep answers short."""


def _client(settings: Settings) -> anthropic.Anthropic:
    key = settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Anthropic API key is not set (Settings -> Claude)")
    return anthropic.Anthropic(api_key=key, timeout=180.0)


class Brain:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = _client(settings)

    # ------------------------------------------------------------ helpers
    def _create(self, **kw) -> Any:
        """messages.create with refusal fallback; fall back to the plain endpoint if the beta is rejected."""
        try:
            return self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
        except anthropic.BadRequestError:
            return self.client.messages.create(**kw)

    @staticmethod
    def _json(resp) -> dict[str, Any]:
        if resp.stop_reason == "refusal":
            raise RuntimeError("model refused the request")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return json.loads(text)

    # ------------------------------------------------------------ decisions
    def decide(self, symbol: str, snapshot: dict[str, Any], regime: str, signals: list[dict[str, Any]],
               position: dict[str, Any] | None, skills: list[dict[str, Any]], knowledge: list[str]) -> dict[str, Any]:
        skill_text = "\n".join(f"- [{s['category']}] {s['name']}: {s['rule']}" for s in skills) or "(none yet)"
        payload = {
            "symbol": symbol,
            "timeframe": self.settings.timeframe,
            "regime": regime,
            "snapshot": snapshot,
            "rule_signals": signals,
            "open_position": position,
        }
        user = (f"SKILLS:\n{skill_text}\n\n"
                + ("RELEVANT NOTES FROM THE LIBRARY:\n" + "\n---\n".join(knowledge) + "\n\n" if knowledge else "")
                + f"MARKET:\n{json.dumps(payload, ensure_ascii=False)}")
        resp = self._create(
            model=self.settings.model, max_tokens=2000,
            system=[{"type": "text", "text": DECISION_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.settings.effort, "format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
        )
        out = self._json(resp)
        out["confidence"] = max(0.0, min(1.0, float(out.get("confidence", 0))))
        out["stop_distance_atr"] = max(1.0, min(4.0, float(out.get("stop_distance_atr", 2))))
        return out

    # ------------------------------------------------------------ learning
    def extract_skills(self, title: str, text: str, max_chars: int = 120_000) -> list[dict[str, Any]]:
        """Turn a chapter/article into concrete rules. Long texts are split and merged."""
        chunks = [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or [""]
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for i, chunk in enumerate(chunks):
            prompt = (f"Source: {title} (part {i + 1}/{len(chunks)}).\n"
                      "Extract every actionable trading rule in this text - entries, exits, risk, regime, psychology.\n"
                      "Skip generic advice with no concrete condition. Keep the author's thresholds.\n\n" + chunk)
            resp = self._create(
                model=self.settings.model, max_tokens=8000,
                system="You extract trading rules from books and articles for a rule-following trading bot.",
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.settings.effort, "format": {"type": "json_schema", "schema": SKILLS_SCHEMA}},
            )
            for s in self._json(resp).get("skills", []):
                key = s["name"].strip().lower()
                if key not in seen:
                    seen.add(key)
                    found.append(s)
        return found

    def teach_chat(self, history: list[dict[str, str]], skills: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        """One turn of the teaching conversation. Returns (reply, proposed skills)."""
        skill_text = "\n".join(f"- {s['name']}: {s['rule']}" for s in skills[:80]) or "(none)"
        schema = {
            "type": "object",
            "properties": {
                "reply": {"type": "string"},
                "skills": SKILLS_SCHEMA["properties"]["skills"],
            },
            "required": ["reply", "skills"],
            "additionalProperties": False,
        }
        resp = self._create(
            model=self.settings.model, max_tokens=3000,
            system=TEACH_SYSTEM + f"\n\nCurrent skills:\n{skill_text}",
            messages=[{"role": m["role"], "content": m["content"]} for m in history],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
        )
        out = self._json(resp)
        return out.get("reply", ""), out.get("skills", [])

    def ping(self) -> str:
        resp = self._create(model=self.settings.model, max_tokens=50,
                            messages=[{"role": "user", "content": "Reply with the single word OK."}],
                            output_config={"effort": "low"})
        return next((b.text for b in resp.content if b.type == "text"), "").strip()
