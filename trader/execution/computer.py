"""Computer-use executor: places an order on an exchange that has no API by
driving its website or desktop app with the mouse and keyboard, guided by Claude.

How it works
------------
1. The engine calls ``place(symbol, side, qty)`` with a concrete order.
2. We screenshot the screen, send it to Claude with the ``computer_toolset_20260801``
   toolset and the user's notes about where the exchange is on screen.
3. Claude asks for clicks / typing / scrolling; we perform them with pyautogui,
   screenshot again, and loop until it says the order is submitted or gives up.
4. Before the click that SUBMITS the order, the model must call ``request_confirm``;
   if ``confirm_before_submit`` is on, a human has to approve in the GUI.

The model never sees the API keys and never decides the size - the engine did that
through the risk manager. What it decides is where to click.
"""
from __future__ import annotations

import base64
import io
import sys
import time
from typing import Any, Callable

from .base import Broker, Fill
from ..config import Settings

SYSTEM = """You are operating a trading website or desktop app on the user's own Windows computer to place ONE order.
You control the mouse and keyboard through the computer tools. Rules:
- Place exactly the order you are given: symbol, side, quantity. Never change the quantity or the side.
- Prefer a MARKET order. If the site only has limit orders, use the current best price.
- Before the final click that submits the order, call the `request_confirm` tool with a one-line summary
  of what you see in the order form. Only click submit after it returns "approved".
- After submitting, read the confirmation on screen and call `report_result` with the filled price if visible.
- If the order form cannot be found, the session is logged out, a captcha appears, or anything looks
  wrong, STOP and call `report_result` with status "failed" and what you saw. Do not guess.
- Do not open other websites, do not change settings, do not touch anything unrelated to this order.
- Coordinates are in the pixel space of the screenshots you receive."""

CONFIRM_TOOL = {
    "name": "request_confirm",
    "description": "Ask the human to approve the order as it currently appears in the form.",
    "input_schema": {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    },
    "strict": True,
}
RESULT_TOOL = {
    "name": "report_result",
    "description": "Report the outcome of the order attempt and stop.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["filled", "failed"]},
            "price": {"type": "number", "description": "fill price if shown on screen, else 0"},
            "note": {"type": "string"},
        },
        "required": ["status", "price", "note"],
        "additionalProperties": False,
    },
    "strict": True,
}


class Screen:
    """Screenshots and input on the local machine. pyautogui/mss are only importable on a desktop."""

    def __init__(self, max_width: int):
        self.max_width = max_width
        self.scale = 1.0
        self._pg = None
        self._mss = None

    def _load(self):
        if self._pg is None:
            import pyautogui  # type: ignore
            import mss  # type: ignore
            pyautogui.FAILSAFE = True   # slam the mouse into a corner to abort
            pyautogui.PAUSE = 0.15
            self._pg, self._mss = pyautogui, mss

    def screenshot_png(self) -> bytes:
        self._load()
        from PIL import Image  # type: ignore
        with self._mss.mss() as sct:
            mon = sct.monitors[1]
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if img.width > self.max_width:
            self.scale = img.width / self.max_width
            img = img.resize((self.max_width, int(img.height / self.scale)))
        else:
            self.scale = 1.0
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _xy(self, coord) -> tuple[int, int]:
        return int(coord[0] * self.scale), int(coord[1] * self.scale)

    def act(self, name: str, inp: dict[str, Any]) -> str:
        self._load()
        pg = self._pg
        mods = inp.get("text") if name.endswith("click") or name == "scroll" else None
        held = [m for m in (mods or "").split("+") if m] if mods else []
        for m in held:
            pg.keyDown(m)
        try:
            if name in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
                if inp.get("coordinate"):
                    x, y = self._xy(inp["coordinate"])
                    pg.moveTo(x, y, duration=0.1)
                button = {"left_click": "left", "right_click": "right", "middle_click": "middle"}.get(name, "left")
                clicks = {"double_click": 2, "triple_click": 3}.get(name, 1)
                pg.click(button=button, clicks=clicks, interval=0.08)
            elif name == "mouse_move":
                pg.moveTo(*self._xy(inp["coordinate"]), duration=0.1)
            elif name == "left_click_drag":
                pg.moveTo(*self._xy(inp["start_coordinate"]), duration=0.1)
                pg.dragTo(*self._xy(inp["coordinate"]), duration=0.4, button="left")
            elif name == "left_mouse_down":
                pg.mouseDown()
            elif name == "left_mouse_up":
                pg.mouseUp()
            elif name == "cursor_position":
                x, y = pg.position()
                return f"[{int(x / self.scale)}, {int(y / self.scale)}]"
            elif name == "scroll":
                if inp.get("coordinate"):
                    pg.moveTo(*self._xy(inp["coordinate"]), duration=0.1)
                amt = int(inp.get("scroll_amount", 3))
                d = inp.get("scroll_direction", "down")
                if d in ("up", "down"):
                    pg.scroll(amt if d == "up" else -amt)
                else:
                    pg.hscroll(amt if d == "right" else -amt)
            elif name == "type":
                pg.write(inp["text"], interval=0.02)
            elif name == "key":
                keys = [k.strip().lower() for k in inp["text"].split("+")]
                for _ in range(int(inp.get("repeat", 1))):
                    pg.hotkey(*keys) if len(keys) > 1 else pg.press(keys[0])
            elif name == "hold_key":
                pg.keyDown(inp["text"]); time.sleep(min(float(inp.get("duration", 1)), 300)); pg.keyUp(inp["text"])
            elif name == "wait":
                time.sleep(min(float(inp.get("duration", 1)), 300))
            else:
                return f"unsupported action {name}"
        finally:
            for m in reversed(held):
                pg.keyUp(m)
        return "OK"


class ComputerBroker(Broker):
    """A Broker whose market_order is executed by driving the screen.

    ``confirm`` is called with the model's summary and must return True/False
    (the GUI shows a dialog; the CLI prints and reads stdin).
    ``on_step`` receives a short progress line for the log.
    """
    name = "computer"

    def __init__(self, settings: Settings, client, confirm: Callable[[str], bool],
                 on_step: Callable[[str], None] | None = None, cash_balance: float = 0.0):
        self.settings = settings
        self.client = client
        self.confirm = confirm
        self.on_step = on_step or (lambda s: None)
        self.screen = Screen(settings.computer.max_screenshot_width)
        self._cash = cash_balance or settings.risk.capital_limit
        self._positions: dict[str, dict] = {}

    # The site is the source of truth for balances but we cannot read it programmatically,
    # so we track what we sent and let the user correct the figure in settings.
    def cash(self) -> float:
        return self._cash

    def equity(self, prices: dict[str, float]) -> float:
        eq = self._cash
        for sym, p in self._positions.items():
            eq += p["qty"] * prices.get(sym, p["price"])
        return eq

    def market_order(self, symbol: str, side: str, qty: float, price_hint: float) -> Fill:
        if not sys.platform.startswith("win") and not sys.platform.startswith("darwin") and not sys.platform.startswith("linux"):
            raise RuntimeError("computer executor needs a desktop session")
        result = self._drive(symbol, side, qty, price_hint)
        if result["status"] != "filled":
            raise RuntimeError(f"computer executor: {result['note']}")
        price = float(result["price"] or price_hint)
        if side == "buy":
            self._cash -= qty * price
            self._positions[symbol] = {"qty": qty, "price": price}
        else:
            self._cash += qty * price
            self._positions.pop(symbol, None)
        return Fill(symbol, side, qty, price, 0.0, order_id="screen")

    # ------------------------------------------------------------ the loop
    def _drive(self, symbol: str, side: str, qty: float, price_hint: float) -> dict[str, Any]:
        cs = self.settings.computer
        task = (f"Place a {side.upper()} MARKET order for {qty:g} {symbol} (about {price_hint:g} per unit).\n"
                f"Notes from the user about this exchange:\n{cs.exchange_notes or '(none)'}\n"
                f"Take a screenshot first.")
        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        tools = [{"type": "computer_toolset_20260801"}, CONFIRM_TOOL, RESULT_TOOL]
        approved = False
        for step in range(cs.max_steps):
            resp = self.client.messages.create(
                model=self.settings.model, max_tokens=4096, system=SYSTEM, tools=tools, messages=messages,
                output_config={"effort": "medium"},
            )
            if resp.stop_reason == "refusal":
                return {"status": "failed", "price": 0, "note": "model refused"}
            messages.append({"role": "assistant", "content": resp.content})
            results: list[dict[str, Any]] = []
            done: dict[str, Any] | None = None
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                name = block.name
                inp = dict(block.input or {})
                toolset = getattr(block, "toolset_name", None)
                res: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id}
                if toolset:
                    res["toolset_name"] = toolset
                if name == "request_confirm":
                    self.on_step(f"confirm requested: {inp.get('summary','')}")
                    ok = True if not cs.confirm_before_submit else bool(self.confirm(inp.get("summary", "")))
                    approved = ok
                    res["content"] = "approved" if ok else "REJECTED by the user. Do not submit. Call report_result with status failed."
                elif name == "report_result":
                    done = {"status": inp.get("status", "failed"), "price": inp.get("price", 0), "note": inp.get("note", "")}
                    res["content"] = "noted"
                elif name in ("screenshot", "zoom"):
                    png = self.screen.screenshot_png()
                    res["content"] = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                    "data": base64.b64encode(png).decode()}}]
                else:
                    self.on_step(f"step {step}: {name} {inp}")
                    try:
                        text = self.screen.act(name, inp)
                    except Exception as exc:
                        text, res["is_error"] = f"action failed: {exc}", True
                    res["content"] = text
                results.append(res)
            if done:
                if done["status"] == "filled" and cs.confirm_before_submit and not approved:
                    done = {"status": "failed", "price": 0, "note": "model reported a fill without asking for confirmation"}
                return done
            if not results:
                # plain text answer without tools - treat as failure to keep the loop honest
                text = " ".join(b.text for b in resp.content if b.type == "text")
                return {"status": "failed", "price": 0, "note": text[:300] or "no action"}
            messages.append({"role": "user", "content": results})
        return {"status": "failed", "price": 0, "note": f"gave up after {cs.max_steps} steps"}
