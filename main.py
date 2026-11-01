"""Say a phrase on a timer. Reference module for the Python form API."""

from __future__ import annotations

import time

from miraspeak.modules.api import (
    LIVE,
    HostBusyError,
    Module,
    ModuleContext,
    ModuleError,
    has_unclosed_marker,
    ui,
)

# How often to look again while other lines are still queued.
_QUEUE_POLL_S = 0.25
# Speak's clock shows tenths, so the bar moves once per tenth.
_PROGRESS_TICK_S = 0.1
# Shortest gap between phrases. Stops a 0-second interval from speaking in a tight loop.
_MIN_INTERVAL_S = 5


class TimerModule(Module):
    def page(self) -> ui.Page:
        return ui.Page(
            intro=(
                "Say a phrase on a timer, using the voice bar below. "
                "Shortest interval is 5 seconds. The phrase cannot be edited "
                "while the timer is running."
            ),
            sections=(
                ui.Section(
                    "Phrase",
                    (
                        ui.Text("phrase", "Phrase", default="Time check."),
                        ui.Number(
                            "minutes", "Every", default=5, minimum=0, maximum=180, suffix=" min"
                        ),
                        ui.Number(
                            "seconds", "Seconds", default=0, minimum=0, maximum=59, suffix=" sec"
                        ),
                    ),
                ),
                ui.Section(
                    "Run",
                    (
                        ui.Button("run", "Start"),
                        ui.Progress("wait"),
                        ui.Status("next", "Press Start."),
                    ),
                ),
            ),
        )

    def on_start(self, ctx: ModuleContext) -> None:
        self.ctx = ctx
        self._holding = False
        self._generation = 0
        self._timer = None
        self._progress = None
        self._cycle_s = float(self._interval())
        self._cycle_start = time.monotonic()
        self._set_running(False)
        self.ctx.ui.set_progress("wait", self._cycle_s, self._cycle_s)

    def on_stop(self) -> None:
        self._holding = False
        self._generation += 1
        self._cancel_clock()
        self._set_running(False)

    def mcp_instructions(self) -> str:
        return (
            "Timer repeats a phrase on a schedule. To start or change it, "
            "call_module with module_id 'timer' and tool 'start' "
            "(phrase, minutes, seconds). To stop the schedule, use tool 'stop'. "
            "Stop does not cancel lines already queued. Do not use the speak tool "
            "for a repeating phrase."
        )

    def mcp_tools(self) -> dict:
        return {"start": self.mcp_start, "stop": self.mcp_stop}

    def mcp_start(self, phrase: str, minutes: int = 5, seconds: int = 0) -> dict:
        """Start repeating a phrase. The shortest interval is 5 seconds."""
        problem = _phrase_problem(phrase)
        if problem:
            return {"ok": False, "message": problem}
        self.ctx.ui.set("phrase", phrase)
        self.ctx.ui.set("minutes", minutes)
        self.ctx.ui.set("seconds", seconds)
        if not self._running:
            self._set_running(True)
        self._arm()
        return {"ok": True, "running": True, "interval_s": self._interval()}

    def mcp_stop(self) -> dict:
        """Stop the schedule. Lines already queued keep playing."""
        if self._running:
            self._stop_schedule()
        return {"ok": True, "running": False}

    def on_action(self, action: str, row: int | None = None) -> None:
        del row
        if action != "run":
            return
        if self._running:
            self._stop_schedule()
            return
        problem = _phrase_problem(self.ctx.values.get("phrase", ""))
        if problem:
            self.ctx.ui.set_status("next", problem, tone=ui.TONE_WARNING)
            return
        self._set_running(True)
        self._arm()

    def _stop_schedule(self) -> None:
        self._holding = False
        self._generation += 1
        self._cancel_clock()
        self._set_running(False)
        interval = float(self._interval())
        self.ctx.ui.set_progress("wait", interval, interval)
        self.ctx.ui.set_status("next", "Stopped.")

    def on_value(self, key: str, value: object) -> None:
        del value
        if key in {"minutes", "seconds"} and self._running:
            self._arm()

    def _arm(self) -> None:
        """Start the countdown over from now."""
        self._generation += 1
        generation = self._generation
        self._cancel_clock()
        interval = self._interval()
        self._holding = False
        self._cycle_s = float(interval)
        self._cycle_start = time.monotonic()
        self._timer = self.ctx.every(interval, lambda: self._due(generation))
        self._progress = self.ctx.every(_PROGRESS_TICK_S, lambda: self._paint(generation))
        self.ctx.ui.set_progress("wait", self._cycle_s, self._cycle_s)
        self.ctx.ui.set_status("next", _next_text(interval))

    def _cancel_clock(self) -> None:
        for timer in (self._timer, self._progress):
            if timer is not None:
                timer.cancel()
        self._timer = None
        self._progress = None

    def _paint(self, generation: int) -> None:
        if generation != self._generation or not self._running:
            return
        total = self._cycle_s
        if self._holding or total <= 0:
            self.ctx.ui.set_progress("wait", 0.0, total)
            return
        elapsed = min(total, max(0.0, time.monotonic() - self._cycle_start))
        self.ctx.ui.set_progress("wait", total - elapsed, total)

    def _restart_bar(self) -> None:
        self._holding = False
        self._cycle_s = float(self._interval())
        self._cycle_start = time.monotonic()
        self.ctx.ui.set_progress("wait", self._cycle_s, self._cycle_s)

    def _due(self, generation: int) -> None:
        if generation != self._generation or not self._running:
            return
        self._speak_when_clear(generation)

    def _speak_when_clear(self, generation: int) -> None:
        if generation != self._generation or not self._running:
            return
        try:
            busy = self.ctx.queue_depth() > 0
        except HostBusyError:
            self.ctx.call_later(_QUEUE_POLL_S, lambda: self._speak_when_clear(generation))
            return
        if busy:
            self._holding = True
            self.ctx.ui.set_progress("wait", self._cycle_s, self._cycle_s)
            self.ctx.ui.set_status("next", "Waiting for other lines.")
            self.ctx.call_later(_QUEUE_POLL_S, lambda: self._speak_when_clear(generation))
            return
        phrase = str(self.ctx.values["phrase"]).strip()
        problem = _phrase_problem(phrase)
        if problem:
            self.ctx.ui.set_status("next", problem, tone=ui.TONE_WARNING)
            self._restart_bar()
            return
        try:
            self.ctx.speak(phrase, voice=LIVE)
        except ModuleError as e:
            self.ctx.ui.set_status("next", str(e), tone=ui.TONE_WARNING)
            self._restart_bar()
            return
        self._restart_bar()
        self.ctx.ui.set_status("next", _next_text(self._interval()))

    def _interval(self) -> int:
        total = _part(self.ctx, "minutes", 180) * 60 + _part(self.ctx, "seconds", 59)
        return max(_MIN_INTERVAL_S, total)

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.ctx.ui.set_enabled("phrase", not running)
        self.ctx.ui.set_label("run", "Stop" if running else "Start")


def _phrase_problem(phrase: object) -> str | None:
    text = str(phrase).strip()
    if not text:
        return "Write a phrase first."
    if has_unclosed_marker(text):
        return "Finish or remove the incomplete [ marker."
    return None


def _part(ctx: ModuleContext, key: str, maximum: int) -> int:
    value = ctx.values.get(key, 0)
    if isinstance(value, int) and 0 <= value <= maximum:
        return value
    return 0


def _next_text(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    parts: list[str] = []
    if minutes:
        parts.append(f"{minutes} min")
    if secs or not parts:
        parts.append(f"{secs} sec")
    return f"Next in {' '.join(parts)}."
