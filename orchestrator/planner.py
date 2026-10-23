"""Breaks a user task into atomic subtasks, via an LLM when quota allows, otherwise a built-in template."""
import json
import re

from .config import CAPABILITIES

PLAN_SYSTEM = """You are the planning module of a multi-agent software build system.
Break the user's task into 4-10 atomic subtasks that smaller models can each finish in one response.
Each subtask needs exactly one capability from: planning, design, copy, code, debug.
- design: UI/UX spec (layout, palette, typography, components) as notes, no code
- copy: all user-facing text as notes
- code: writes or rewrites specific files
- debug: reviews existing files and returns fixed versions
Unless the user explicitly asks for a framework, build a static site in plain HTML, CSS and JavaScript
that works by opening index.html (at the project root) directly in a browser, with no build step.
If a framework is requested, the plan must create every file needed to install and run it
(package.json, entry index.html, configs) and say how to run it in a README.md.
Keep each code subtask to 1-3 files. Use relative file paths. Put a final debug subtask last.
Respond with ONLY a JSON object, no prose:
{"project_name": "kebab-case-name",
 "subtasks": [{"id": "t1", "title": "...", "capability": "design", "depends_on": [],
               "instructions": "...", "files": []}]}"""


def slugify(text, limit=40):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].strip("-") or "project"


def heuristic_plan(task):
    """Generic static-website plan used when no planning agent is available."""
    def st(id_, title, cap, deps, instructions, files=()):
        return {"id": id_, "title": title, "capability": cap, "depends_on": list(deps),
                "instructions": instructions, "files": list(files)}
    return {
        "project_name": slugify(task),
        "subtasks": [
            st("t1", "UI/UX design spec", "design", [],
               "Define the pages/sections, layout, color palette (with dark mode if relevant), "
               "typography, components and responsive behaviour."),
            st("t2", "Copywriting", "copy", ["t1"],
               "Write every piece of user-facing text for the sections in the design spec."),
            st("t3", "HTML structure", "code", ["t1", "t2"],
               "Build semantic, accessible HTML using the design spec and copy. "
               "Link style.css and script.js.", ["index.html"]),
            st("t4", "Styling", "code", ["t1", "t3"],
               "Write responsive CSS implementing the design spec for the existing HTML.", ["style.css"]),
            st("t5", "Interactivity", "code", ["t1", "t3"],
               "Write vanilla JS for any interactive behaviour in the design spec.", ["script.js"]),
            st("t6", "Debug & integration review", "debug", ["t3", "t4", "t5"],
               "Check that all files work together (selectors, ids, links, accessibility, "
               "responsiveness) and return corrected versions of any file with problems."),
        ],
    }


def _extract_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in planner output")
    return json.loads(text[start:end + 1])


def validate_plan(plan):
    subtasks = plan.get("subtasks")
    if not isinstance(subtasks, list) or not subtasks:
        raise ValueError("plan has no subtasks")
    by_id = {}
    for st in subtasks:
        if st.get("capability") not in CAPABILITIES:
            raise ValueError(f"subtask {st.get('id')} has invalid capability {st.get('capability')!r}")
        if not st.get("id") or st["id"] in by_id:
            raise ValueError(f"missing or duplicate subtask id {st.get('id')!r}")
        st.setdefault("depends_on", [])
        st.setdefault("files", [])
        st.setdefault("instructions", st.get("title", ""))
        by_id[st["id"]] = st
    # Topologically order so dependencies always run first.
    ordered, seen, visiting = [], set(), set()

    def visit(sid):
        if sid in seen:
            return
        if sid in visiting:
            raise ValueError(f"dependency cycle at {sid}")
        visiting.add(sid)
        for dep in by_id[sid]["depends_on"]:
            if dep not in by_id:
                raise ValueError(f"subtask {sid} depends on unknown {dep}")
            visit(dep)
        visiting.discard(sid)
        seen.add(sid)
        ordered.append(by_id[sid])

    for st in subtasks:
        visit(st["id"])
    plan["subtasks"] = ordered
    plan["project_name"] = slugify(plan.get("project_name") or "project")
    return plan


def plan_from_text(text):
    return validate_plan(_extract_json(text))
