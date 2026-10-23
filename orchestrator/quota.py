"""Persistent per-agent usage counters with daily resets and per-minute windows."""
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path


def _empty_usage():
    return {"requests": 0, "tokens": 0, "minute": [], "last_request": 0, "cooldown_until": 0,
            "exhausted": None, "errors": 0, "current_task": None}


class QuotaTracker:
    def __init__(self, state_path):
        self.path = Path(state_path)
        self.state = {"date": date.today().isoformat(), "agents": {}}
        if self.path.is_file():
            self.state = json.loads(self.path.read_text())
        self._roll_day()

    def _roll_day(self):
        today = date.today().isoformat()
        if self.state.get("date") != today:
            self.state = {"date": today, "agents": {}}
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        os.replace(tmp, self.path)

    def usage(self, name):
        return self.state["agents"].setdefault(name, _empty_usage())

    def check(self, agent, est_tokens, now=None):
        """Return (ok, reason, wait_seconds). wait_seconds is None when the agent is out for the day."""
        self._roll_day()
        now = now or time.time()
        u = self.usage(agent.name)
        if u["exhausted"]:
            return False, f"exhausted for today ({u['exhausted']})", None
        rpd = agent.limit("requests_per_day")
        if rpd is not None and u["requests"] >= rpd:
            return False, f"daily request limit reached ({rpd})", None
        tpd = agent.limit("tokens_per_day")
        if tpd is not None and u["tokens"] + est_tokens > tpd:
            return False, f"not enough daily tokens left ({tpd - u['tokens']} < ~{est_tokens})", None
        if u["cooldown_until"] > now:
            return False, "cooling down", u["cooldown_until"] - now
        gap = agent.limit("min_interval_seconds")
        since = now - u.get("last_request", 0)
        if gap and since < gap:
            return False, f"min interval ({gap}s)", gap - since
        rpm = agent.limit("requests_per_minute")
        window = [t for t in u["minute"] if now - t < 60]
        u["minute"] = window
        if rpm is not None and len(window) >= rpm:
            return False, f"per-minute limit ({rpm})", 60 - (now - min(window))
        return True, "ok", 0

    def record_request(self, agent, task_id=None):
        u = self.usage(agent.name)
        u["requests"] += 1
        u["minute"].append(time.time())
        u["last_request"] = time.time()
        u["current_task"] = task_id
        self.save()

    def record_tokens(self, agent, tokens):
        u = self.usage(agent.name)
        u["tokens"] += int(tokens)
        u["current_task"] = None
        self.save()

    def record_error(self, agent):
        u = self.usage(agent.name)
        u["errors"] += 1
        u["current_task"] = None
        self.save()

    def cooldown(self, agent, seconds):
        u = self.usage(agent.name)
        u["cooldown_until"] = max(u["cooldown_until"], time.time() + seconds)
        u["current_task"] = None
        self.save()

    def exhaust(self, agent, reason):
        u = self.usage(agent.name)
        u["exhausted"] = reason
        u["current_task"] = None
        self.save()

    @staticmethod
    def seconds_until_reset():
        now = datetime.now()
        midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        return int((midnight - now).total_seconds())

    def snapshot(self, agents):
        self._roll_day()
        now = time.time()
        rows = []
        for a in agents:
            u = self.usage(a.name)
            rpd, tpd = a.limit("requests_per_day"), a.limit("tokens_per_day")
            if not a.is_configured():
                state = "disabled" if not a.enabled else "no api key"
            elif u["exhausted"] or (rpd is not None and u["requests"] >= rpd):
                state = "exhausted"
            elif u["current_task"]:
                state = f"working: {u['current_task']}"
            elif u["cooldown_until"] > now:
                state = f"cooldown {int(u['cooldown_until'] - now)}s"
            else:
                state = "idle"
            rows.append({
                "name": a.name, "model": a.model, "capabilities": a.capabilities,
                "requests": u["requests"], "requests_per_day": rpd,
                "requests_left": None if rpd is None else max(rpd - u["requests"], 0),
                "tokens": u["tokens"], "tokens_per_day": tpd,
                "tokens_left": None if tpd is None else max(tpd - u["tokens"], 0),
                "requests_per_minute": a.limit("requests_per_minute"),
                "errors": u["errors"], "state": state,
            })
        return rows
