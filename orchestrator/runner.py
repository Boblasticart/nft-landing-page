"""Core loop: plan, route each subtask to an agent with quota, assemble outputs, degrade gracefully."""
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import assembler, planner, providers
from .quota import QuotaTracker

SYSTEM_PROMPTS = {
    "planning": planner.PLAN_SYSTEM,
    "design": "You are a senior UI/UX designer. Produce a concise, concrete design spec in markdown: "
              "sections, layout, exact colors (hex), fonts, spacing, components, states, responsive rules. No code.",
    "copy": "You are an expert web copywriter. Write final, ready-to-ship copy in markdown, "
            "organised by section, matching the design spec. No placeholders like lorem ipsum.",
    "code": "You are a senior software engineer. Write complete, working, production-quality code that "
            "fits the existing project files and the design spec. Never leave TODOs.\n\n" + assembler.FILE_FORMAT,
    "debug": "You are a meticulous code reviewer and debugger. Find real bugs and integration problems in the "
             "project files and fix them. Return only files you changed; if nothing needs fixing, say so.\n\n"
             + assembler.FILE_FORMAT,
}

NOTE_LIMIT = 8000
MAX_CONTINUATIONS = 4
MAX_FIX_ROUNDS = 2
CONTINUATION_TAIL = 6000
CONTINUE_PROMPT = ("Your answer was cut off by a length limit. Continue from the exact character where it stopped. "
                   "Do not repeat anything or restart the file, and add no preamble. Close the open file with "
                   "=== END FILE ===, then write any remaining files.")


class NoCapacity(Exception):
    def __init__(self, capability, reasons):
        self.capability = capability
        self.reasons = reasons
        detail = "; ".join(f"{k}: {v}" for k, v in reasons.items()) or "no configured agent has this capability"
        super().__init__(f"no agent available for '{capability}' ({detail})")


class Logger:
    def __init__(self, log_path, run=None):
        self.log_path = Path(log_path)
        self.run = run

    def __call__(self, msg, level="info"):
        stamp = datetime.now().strftime("%H:%M:%S")
        prefix = {"warn": "⚠ ", "error": "✖ ", "ok": "✔ "}.get(level, "")
        line = f"[{stamp}] {prefix}{msg}"
        print(line, flush=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(line + "\n")
        if self.run is not None:
            self.run.data["events"] = (self.run.data["events"] + [{"t": stamp, "level": level, "msg": msg}])[-300:]
            self.run.save()


class Run:
    def __init__(self, path, data):
        self.path = Path(path)
        self.data = data

    @classmethod
    def create(cls, runs_dir, task, output_root, dry_run):
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        data = {"run_id": run_id, "task": task, "created": datetime.now().isoformat(timespec="seconds"),
                "output_root": str(Path(output_root).resolve()), "dry_run": dry_run,
                "plan": None, "status": {}, "events": []}
        run = cls(Path(runs_dir) / run_id / "run.json", data)
        run.save()
        return run

    @classmethod
    def load(cls, runs_dir, run_id):
        path = Path(runs_dir) / run_id / "run.json"
        if not path.is_file():
            raise FileNotFoundError(f"no run named {run_id} in {runs_dir}")
        return cls(path, json.loads(path.read_text()))

    @property
    def project_dir(self):
        return Path(self.data["output_root"]) / self.data["plan"]["project_name"]

    def notes_path(self, tid):
        return self.path.parent / "notes" / f"{tid}.md"

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.replace(tmp, self.path)


class Orchestrator:
    def __init__(self, agents, fallbacks, workdir, max_wait=90):
        self.agents = agents
        self.fallbacks = fallbacks
        self.workdir = Path(workdir)
        self.max_wait = max_wait
        self.session_disabled = set()
        self.tracker = None
        self.log = None

    # ---- routing -------------------------------------------------------------------------------
    def _candidates(self, capability):
        usable = [a for a in self.agents if a.is_configured() and a.name not in self.session_disabled]
        ordered, seen = [], set()
        for cap in [capability] + list(self.fallbacks.get(capability, [])):
            for a in sorted((a for a in usable if cap in a.capabilities), key=lambda a: a.priority):
                if a.name not in seen:
                    seen.add(a.name)
                    ordered.append((cap, a))
        return ordered

    def call(self, capability, system, prompt, task_id, exclude=(), history=()):
        reasons = {}
        for _ in range(20):
            reasons = {}
            soonest = None
            choice = None
            for cap, agent in self._candidates(capability):
                if agent.name in exclude:
                    reasons[agent.name] = "already failed this subtask"
                    continue
                context = system + prompt + "".join(text for _, text in history)
                est = providers.estimate_tokens(context) + agent.max_output_tokens // 2
                ok, reason, wait = self.tracker.check(agent, est)
                if ok:
                    choice = (cap, agent)
                    break
                reasons[agent.name] = reason
                if wait is not None:
                    soonest = wait if soonest is None else min(soonest, wait)
            if choice is None:
                if soonest is not None and soonest <= self.max_wait:
                    self.log(f"{task_id}: all '{capability}' agents rate-limited, waiting {soonest:.0f}s")
                    time.sleep(soonest + 0.5)
                    continue
                raise NoCapacity(capability, reasons)

            cap, agent = choice
            via = "" if cap == capability else f" (fallback via '{cap}' capability)"
            self.log(f"{task_id}: → {agent.name} [{agent.model}]{via}")
            self.tracker.record_request(agent, task_id)
            try:
                result = providers.complete(agent, system, prompt, history)
            except providers.RateLimited as e:
                self.tracker.record_error(agent)
                if e.daily:
                    self.tracker.exhaust(agent, "provider reported daily quota exhausted")
                    self.log(f"{agent.name} hit its daily quota, rerouting", "warn")
                else:
                    self.tracker.cooldown(agent, e.retry_after or 60)
                    self.log(f"{agent.name} rate-limited, cooling down {e.retry_after or 60:.0f}s, rerouting", "warn")
                continue
            except providers.AuthError as e:
                self.tracker.record_error(agent)
                self.session_disabled.add(agent.name)
                self.log(f"{agent.name} rejected credentials, disabled for this session: {e}", "error")
                continue
            except providers.ProviderError as e:
                self.tracker.record_error(agent)
                exclude = set(exclude) | {agent.name}
                self.log(f"{agent.name} failed: {e}; rerouting", "warn")
                continue
            self.tracker.record_tokens(agent, result.tokens)
            return result, agent
        raise NoCapacity(capability, reasons)

    # ---- run lifecycle -------------------------------------------------------------------------
    def _open(self, run):
        state_name = "state-dryrun.json" if run.data["dry_run"] else "state.json"
        self.tracker = QuotaTracker(self.workdir / state_name)
        self.log = Logger(self.workdir / "orchestrator.log", run)

    def start(self, task, output_root, dry_run=False):
        run = Run.create(self.workdir / "runs", task, output_root, dry_run)
        self._open(run)
        self.log(f"run {run.data['run_id']} started: {task}")
        return self.execute(run)

    def resume(self, run_id):
        run = Run.load(self.workdir / "runs", run_id)
        self._open(run)
        self.log(f"resuming run {run_id}")
        return self.execute(run)

    def _plan(self, run):
        task = run.data["task"]
        prompt = f"CAPABILITY: planning\nUSER TASK: {task}\n"
        try:
            result, agent = self.call("planning", SYSTEM_PROMPTS["planning"], prompt, "plan")
            plan = planner.plan_from_text(result.text)
            self.log(f"plan created by {agent.name}: {len(plan['subtasks'])} subtasks", "ok")
        except NoCapacity as e:
            self.log(f"{e}; using built-in website plan", "warn")
            plan = planner.heuristic_plan(task)
        except (ValueError, json.JSONDecodeError) as e:
            self.log(f"planner output unusable ({e}); using built-in website plan", "warn")
            plan = planner.heuristic_plan(task)
        run.data["plan"] = plan
        for st in plan["subtasks"]:
            run.data["status"][st["id"]] = {"state": "pending", "agent": None, "tokens": 0, "files": []}
        run.save()
        for st in plan["subtasks"]:
            deps = f" ← {', '.join(st['depends_on'])}" if st["depends_on"] else ""
            self.log(f"  {st['id']} [{st['capability']}] {st['title']}{deps}")

    def _build_prompt(self, run, st):
        plan, status = run.data["plan"], run.data["status"]
        lines = [f"CAPABILITY: {st['capability']}", f"USER TASK: {run.data['task']}",
                 f"SUBTASK: {st['title']}", f"INSTRUCTIONS: {st['instructions']}"]
        if st["files"]:
            lines.append(f"FILES TO PRODUCE: {', '.join(st['files'])}")
        lines.append("\nPROJECT PLAN:")
        for other in plan["subtasks"]:
            lines.append(f"- {other['id']} [{other['capability']}] {other['title']} ({status[other['id']]['state']})")
        notes = []
        for other in plan["subtasks"]:
            path = run.notes_path(other["id"])
            relevant = other["id"] in st["depends_on"] or other["capability"] in ("design", "copy")
            if relevant and status[other["id"]]["state"] == "done" and path.is_file():
                notes.append(f"### {other['id']} {other['title']}\n{path.read_text()[:NOTE_LIMIT]}")
        if notes:
            lines.append("\n--- NOTES FROM EARLIER SUBTASKS ---\n" + "\n\n".join(notes))
        if st["capability"] in ("code", "debug"):
            files = assembler.read_project(run.project_dir)
            lines.append("\n--- CURRENT PROJECT FILES ---\n" + (files or "(none yet)"))
        return "\n".join(lines)

    def _complete(self, cap, prompt, tid, exclude):
        """Call an agent, asking for continuations while a file block is left unterminated by an output cap."""
        result, agent = self.call(cap, SYSTEM_PROMPTS[cap], prompt, tid, exclude=exclude)
        text, tokens = result.text, result.tokens
        for _ in range(MAX_CONTINUATIONS):
            open_header = assembler.unterminated_header(text)
            if not open_header:
                break
            self.log(f"{tid}: output was cut off inside {open_header.strip()}, requesting continuation")
            # Hand the partial answer back as the assistant's own turn so the model resumes instead of restarting.
            history = [("assistant", text[-CONTINUATION_TAIL:]), ("user", CONTINUE_PROMPT)]
            part, _ = self.call(cap, SYSTEM_PROMPTS[cap], prompt, tid, exclude=exclude, history=history)
            tokens += part.tokens
            cont = part.text
            if not cont.strip():
                self.log(f"{tid}: continuation came back empty, keeping partial output", "warn")
                break
            if cont.lstrip().startswith(open_header.strip()):
                # The agent restarted the file instead of continuing it; keep the fresh copy.
                text = text[:text.rindex(open_header)] + cont.lstrip()
            else:
                text += cont
        return providers.Completion(text, tokens), agent

    def _run_subtask(self, run, st):
        tid, cap = st["id"], st["capability"]
        entry = run.data["status"][tid]
        entry["state"] = "running"
        run.save()
        prompt = self._build_prompt(run, st)
        failed = set()
        for _ in range(3):
            result, agent = self._complete(cap, prompt, tid, failed)
            files, notes = assembler.parse_files(result.text)
            if cap == "code" and not files:
                raw = run.notes_path(tid).with_suffix(f".{agent.name}.raw.txt")
                raw.parent.mkdir(parents=True, exist_ok=True)
                raw.write_text(result.text)
                self.log(f"{tid}: {agent.name} returned no files (raw output in {raw}), trying another agent", "warn")
                failed.add(agent.name)
                continue
            written, rejected = assembler.write_files(run.project_dir, files)
            if rejected:
                self.log(f"{tid}: ignored unsafe paths from {agent.name}: {', '.join(rejected)}", "warn")
            note_path = run.notes_path(tid)
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text(notes)
            entry.update(state="done", agent=agent.name, tokens=entry["tokens"] + result.tokens, files=written)
            run.save()
            wrote = f", wrote {', '.join(written)}" if written else ""
            self.log(f"{tid} done by {agent.name} ({result.tokens} tokens{wrote})", "ok")
            return
        raise NoCapacity(cap, {name: "returned no usable files" for name in failed})

    def _degrade(self, run, st, err):
        reset = QuotaTracker.seconds_until_reset()
        self.log(f"{st['id']} paused: {err}", "warn")
        self.log(f"  Options: resume after daily reset in {reset // 3600}h{reset % 3600 // 60:02d}m with "
                 f"`python -m orchestrator resume {run.data['run_id']}`; add another provider key; or enable "
                 f"the local fallback (set ollama-local \"enabled\": true in agents.json, run `ollama pull "
                 f"qwen2.5-coder:7b`).", "warn")
        print("\a", end="", flush=True)

    def _run_pending(self, run):
        status = run.data["status"]
        for st in run.data["plan"]["subtasks"]:
            entry = status[st["id"]]
            if entry["state"] == "done":
                continue
            blocked = [d for d in st["depends_on"] if status[d]["state"] != "done"]
            if blocked:
                entry["state"] = "blocked"
                run.save()
                self.log(f"{st['id']} blocked, waiting on {', '.join(blocked)}", "warn")
                continue
            try:
                self._run_subtask(run, st)
            except NoCapacity as e:
                entry["state"] = "paused"
                run.save()
                self._degrade(run, st, e)

    def _add_fix_subtask(self, run, problems):
        """Queue a code subtask that repairs what the local integrity check found."""
        n = sum(1 for st in run.data["plan"]["subtasks"] if st["id"].startswith("fix")) + 1
        st = {"id": f"fix{n}", "title": "Fix integrity problems found by local check", "capability": "code",
              "depends_on": [], "files": [],
              "instructions": "A local check of the project found these problems:\n- " + "\n- ".join(problems) +
                              "\nFix all of them: create missing files (entry points, package.json, configs) or "
                              "correct the references. For missing images, create a simple SVG placeholder and "
                              "point the reference at it. Return every file you add or change."}
        run.data["plan"]["subtasks"].append(st)
        run.data["status"][st["id"]] = {"state": "pending", "agent": None, "tokens": 0, "files": []}
        run.save()
        return st

    def execute(self, run):
        if not run.data["plan"]:
            self._plan(run)
        status = run.data["status"]
        self._run_pending(run)
        while all(s["state"] == "done" for s in status.values()):
            problems = assembler.check_project(run.project_dir)
            if not problems:
                self.log("local integrity check passed", "ok")
                break
            fixes = sum(1 for sid in status if sid.startswith("fix"))
            for p in problems:
                self.log(f"integrity check: {p}", "warn")
            if fixes >= MAX_FIX_ROUNDS:
                self.log(f"integrity problems remain after {fixes} fix rounds; review the project by hand", "warn")
                break
            st = self._add_fix_subtask(run, problems)
            self.log(f"queued {st['id']} to repair {len(problems)} problem(s)")
            self._run_pending(run)
        done = sum(1 for s in status.values() if s["state"] == "done")
        total = len(status)
        if done == total:
            self.log(f"run complete: {total}/{total} subtasks; project at {run.project_dir}", "ok")
        else:
            self.log(f"run paused: {done}/{total} subtasks done; resume with "
                     f"`python -m orchestrator resume {run.data['run_id']}`", "warn")
        return run
