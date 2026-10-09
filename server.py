#!/usr/bin/env python3
"""
AI Practice Tutor: local prototype server. Python standard library only.

Run:   python3 server.py            then open http://localhost:8000

Modes (set in config.json, or override with TUTOR_MODE=mock|openai):
  mock    Simulated AI using keyword rules. No API key needed. Use it to check the flow only.
  openai  Live LLM via the OpenAI API. Needs OPENAI_API_KEY set in your environment.

Split of responsibilities:
  The LLM judges free text against the rubric, and writes feedback, hints and summary text.
  Code enforces the product rules: attempt and hint limits, the four-test rule for
  recommendations, the evidence check, the answer-leak guard, guardrail messages,
  task variants on retake, and the score.
"""
import datetime
import json
import os
import re
import sys
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent


def load_json(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def load_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def load_env_file(path=ROOT / ".env"):
    """Read KEY=value lines from .env into the environment. Values already set in the terminal win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()
CONFIG = load_json("config.json")
RUBRIC = load_json("data/rubric.json")
LESSON_LIST = load_json("data/lessons.json")
LESSONS = {lesson["id"]: lesson for lesson in LESSON_LIST}
TASKS = sorted((load_json(p.relative_to(ROOT)) for p in (ROOT / "data" / "tasks").glob("*.json")),
               key=lambda t: t.get("order", 99))
TASK_BY_ID = {t["id"]: t for t in TASKS}

PROMPT_EVALUATE = load_text("prompts/evaluate.md")
PROMPT_HINT = load_text("prompts/hint.md")
PROMPT_SUMMARY = load_text("prompts/summary.md")

SKILLS = RUBRIC["skills"]
SKILL_IDS = [s["id"] for s in SKILLS]
SKILL_BY_ID = {s["id"]: s for s in SKILLS}
LEVELS = ("not_yet_shown", "partially", "demonstrated")
GUARDRAILS = ("none", "asked_for_answer", "abusive", "off_topic")
REC_TESTS = ("tied_to_finding", "consistent_with_data", "proportionate", "specific_and_checkable")
NOTE_LABELS = {n["code"]: n["label"] for n in RUBRIC["practice_notes"]}
MAX_ATTEMPTS = int(CONFIG.get("max_attempts", 3))
MAX_SCORE = sum(s["points_by_attempt"][0] for s in SKILLS)


def resolve_mode():
    mode = os.environ.get("TUTOR_MODE", CONFIG.get("mode", "mock")).lower()
    if mode == "openai" and not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set, so the tutor is running in mock mode.", file=sys.stderr)
        return "mock"
    return mode if mode in ("mock", "openai") else "mock"


MODE = resolve_mode()


# ---------------------------------------------------------------- LLM client

class LLMUnavailable(Exception):
    pass


def call_openai(system_prompt, user_content):
    body = {
        "model": CONFIG["openai_model"],
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    if CONFIG.get("temperature") is not None:
        body["temperature"] = CONFIG["temperature"]
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ.get("OPENAI_API_KEY", ""),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise LLMUnavailable(f"OpenAI returned HTTP {e.code}: {detail}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise LLMUnavailable(f"Could not reach OpenAI: {e}")
    return json.loads(payload["choices"][0]["message"]["content"])


def llm_json(system_prompt, user_content):
    """Call the model and return a JSON object. One retry if the output can't be read."""
    last_error = None
    for _ in range(2):
        try:
            result = call_openai(system_prompt, user_content)
            if isinstance(result, dict):
                return result
            last_error = "response was not a JSON object"
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
            last_error = str(e)
    raise LLMUnavailable(f"Model output could not be read: {last_error}")


# ---------------------------------------------------------------- text helpers

def squash(text):
    return re.sub(r"\s+", "", (text or "").lower())


def normalise(text):
    t = (text or "").lower()
    t = re.sub(r"[\"'\u201c\u201d\u2018\u2019`]", "", t)
    return re.sub(r"\s+", " ", t).strip(" .,;:!?")


def sentences(answer):
    return [p.strip() for p in re.split(r"(?<=[.!?:])\s+|\n+", answer) if p.strip()]


def first_sentence_matching(answer, pattern):
    for piece in sentences(answer):
        if re.search(pattern, piece, re.I):
            return piece
    return ""


def from_sentence_matching(answer, pattern, limit=200):
    """The first sentence matching pattern plus everything after it, e.g. a numbered list of next steps."""
    parts = sentences(answer)
    for i, piece in enumerate(parts):
        if re.search(pattern, piece, re.I):
            return " ".join(parts[i:])[:limit].strip()
    return ""


# ---------------------------------------------------------------- task helpers

def public_task(task):
    out = {k: task[k] for k in ("id", "title", "intro", "columns", "rows", "question")}
    out["role"] = RUBRIC["role"]
    return out


def model_key(task):
    return {k: v for k, v in task["answer_key"].items() if k != "leak_terms"}


def model_rubric():
    skills = []
    for s in SKILLS:
        item = {k: s[k] for k in ("id", "name", "requirement", "levels")}
        if "tests" in s:
            item["tests"] = s["tests"]
        skills.append(item)
    return {"role": RUBRIC["role"], "skills": skills, "rules": RUBRIC["rules"]}


# ---------------------------------------------------------------- product rules

def gaps(skills):
    return [sid for sid in SKILL_IDS if skills[sid]["level"] != "demonstrated"]


def generic_nudges(skills, task):
    return [task["coaching"][sid]["nudge"] for sid in gaps(skills)]


def recommendation_explanation(rec):
    """Name the learner's next step and say which test it fails. Fixed text, never the answer."""
    if rec["level"] == "demonstrated" or not rec.get("evidence"):
        return ""
    tests = rec.get("tests") or {}
    messages = SKILL_BY_ID["recommendation"]["test_feedback"]
    for test in ("consistent_with_data", "proportionate", "tied_to_finding", "specific_and_checkable"):
        if not tests.get(test, False):
            return f'You suggested: "{rec["evidence"].rstrip(".!?:; ")}". {messages[test]}'
    return ""


def apply_rules(raw, answer, task):
    """Apply product rules on top of the model's judgement. The model judges; code decides."""
    log = []
    guardrail = raw.get("guardrail", "none")
    if guardrail not in GUARDRAILS:
        log.append(f"Unknown guardrail value '{guardrail}' treated as none.")
        guardrail = "none"

    result = {
        "guardrail": guardrail,
        "verdict": raw.get("verdict", "missing"),
        "skills": {},
        "practice_notes": [],
        "lessons": [],
        "feedback": str(raw.get("feedback") or "").strip(),
        "nudge": [str(n).strip() for n in (raw.get("nudge") or []) if str(n).strip()][:3],
        "needs_review": bool(raw.get("needs_review")),
        "rule_log": log,
    }

    if guardrail != "none":
        # Rule: fixed, tested messages for guardrails. Not counted as an attempt.
        result["feedback"] = RUBRIC["guardrail_messages"][guardrail]
        result["nudge"] = []
        return result

    answer_norm = normalise(answer)
    raw_skills = raw.get("skills") or {}
    for sid in SKILL_IDS:
        s = raw_skills.get(sid) or {}
        level = s.get("level")
        if level not in LEVELS:
            log.append(f"{sid}: missing or unknown level, set to not_yet_shown.")
            level = "not_yet_shown"
            result["needs_review"] = True
        evidence = str(s.get("evidence") or "").strip()
        tests = None

        if sid == "recommendation":
            # Rule: the four tests define the level, not the model's overall impression.
            given = s.get("tests") or {}
            tests = {k: bool(given.get(k)) for k in REC_TESTS}
            if all(tests.values()):
                rule_level = "demonstrated"
            elif not tests["consistent_with_data"] or not tests["proportionate"]:
                rule_level = "not_yet_shown"
            elif tests["tied_to_finding"] or tests["specific_and_checkable"]:
                rule_level = "partially"
            else:
                rule_level = "not_yet_shown"
            if rule_level != level:
                log.append(f"recommendation: model said {level}, four-test rule gives {rule_level}.")
                level = rule_level

        # Rule: a skill counts only if shown in the learner's own words.
        if level == "demonstrated" and not evidence:
            log.append(f"{sid}: demonstrated without quoted evidence, downgraded to partially.")
            level = "partially"
            result["needs_review"] = True
        if evidence and normalise(evidence) not in answer_norm:
            log.append(f"{sid}: quoted evidence is not in the learner's words.")
            result["needs_review"] = True
            evidence = ""
            if level == "demonstrated":
                level = "partially"
                log.append(f"{sid}: downgraded to partially.")

        entry = {"level": level, "evidence": evidence, "reason": str(s.get("reason") or "")}
        if tests is not None:
            entry["tests"] = tests
        result["skills"][sid] = entry

    result["practice_notes"] = [c for c in (raw.get("practice_notes") or []) if c in NOTE_LABELS]
    result["lessons"] = [LESSONS[c] for c in (raw.get("unknown_concepts") or []) if c in LESSONS]

    # Rule: never hand over answer values the learner hasn't written themselves.
    shown = squash(result["feedback"] + " " + " ".join(result["nudge"]))
    said = squash(answer)
    leaked = [t for t in task["answer_key"]["leak_terms"] if squash(t) in shown and squash(t) not in said]
    if leaked:
        log.append("Feedback contained answer values the learner had not written ("
                   + ", ".join(leaked) + "). Replaced with safe feedback.")
        explanation = recommendation_explanation(result["skills"]["recommendation"])
        result["feedback"] = (RUBRIC["safe_feedback"] + " " + explanation).strip()
        result["nudge"] = generic_nudges(result["skills"], task)

    if not result["feedback"]:
        result["feedback"] = RUBRIC["safe_feedback"]
    if not result["nudge"] and gaps(result["skills"]):
        result["nudge"] = generic_nudges(result["skills"], task)
    return result


# ---------------------------------------------------------------- mock mode (simulated AI)

ASK_FOR_ANSWER = r"(do it for me|give me the answer|tell me the answer|just tell me|write (the|my|this) answer|answer (it|this) for me|solve (it|this)|what is the answer|can you (just )?(write|do|answer))"
ABUSIVE = r"\b(stupid|idiot|dumb|damn|shut up|fuck\w*|shit\w*)\b"
DECREASE = r"decreas|drop|fell|fall|lower|worse|declin|down|reduc"
RATE_WORDS = r"rate|ratio|share|conversion|percent|%|per visit|proportion|fraction"
NEXT_STEP = r"\b(check|investigate|look at|look into|analy[sz]e|review|find out|see)\b"


def mock_evaluate(answer, task):
    """Keyword rules standing in for the LLM. Clearly simulated, for checking the flow only."""
    m = task["mock"]
    t = answer.lower()
    empty_skills = {sid: {"level": "not_yet_shown", "evidence": "", "reason": ""} for sid in SKILL_IDS}
    if re.search(ABUSIVE, t):
        return {"guardrail": "abusive", "skills": empty_skills}
    if re.search(ASK_FOR_ANSWER, t):
        return {"guardrail": "asked_for_answer", "skills": empty_skills}

    rates = bool(re.search(m["rate_a"], t)) and bool(re.search(m["rate_b"], t))
    derived = bool(re.search(m["derived"], t))
    if rates or derived:
        calc = "demonstrated"
        calc_ev = first_sentence_matching(answer, m["rate_b"] if rates else m["derived"])
    elif re.search(m["rate_a"] + "|" + m["rate_b"] + r"|ratio|rate|conversion", t):
        calc = "partially"
        calc_ev = first_sentence_matching(answer, m["rate_a"] + "|" + m["rate_b"] + r"|ratio|rate|conversion")
    else:
        calc, calc_ev = "not_yet_shown", ""

    dec = bool(re.search(DECREASE, t))
    if dec and re.search(RATE_WORDS, t):
        interp, interp_ev = "demonstrated", first_sentence_matching(answer, DECREASE)
    elif dec:
        interp, interp_ev = "partially", first_sentence_matching(answer, DECREASE)
    else:
        interp, interp_ev = "not_yet_shown", ""

    spend = bool(re.search(m["spend_rec"], t))
    rebuild = bool(re.search(m["rebuild_rec"], t))
    if spend or rebuild:
        tests = {"tied_to_finding": dec, "consistent_with_data": not spend, "proportionate": not rebuild, "specific_and_checkable": False}
        rec_ev = first_sentence_matching(answer, m["spend_rec"] if spend else m["rebuild_rec"])
    elif re.search(m["valid_rec"], t):
        tests = {"tied_to_finding": dec, "consistent_with_data": True, "proportionate": True, "specific_and_checkable": True}
        rec_ev = first_sentence_matching(answer, m["valid_rec"])
    else:
        # A next step that names nothing in the data or a likely cause: not connected, not checkable.
        tests = {k: False for k in REC_TESTS}
        tests["consistent_with_data"] = tests["proportionate"] = True
        rec_ev = from_sentence_matching(answer, NEXT_STEP)
        if re.search(r"not sure|don.?t know|no idea", rec_ev, re.I):
            rec_ev = ""  # the learner didn't actually suggest anything

    notes = []
    if derived and not rates:
        notes.append("working_not_shown")
    if re.search(r"(?<!\d)\d\s*%\s*(drop|decrease|fall|less|lower)|(decreased|dropped|fell|down) by \d\s*%", t) and "percentage point" not in t:
        notes.append("change_described_imprecisely")
    if re.search(r"(visits?|visitors?|opens?) (by|per) (enquir|click|purchase)", t):
        notes.append("ratio_named_inversely")

    unknown = []
    if re.search(r"(don.?t|do not) know|not sure what|forgot|never heard", t):
        if "percentage point" in t:
            unknown.append("percentage_points")
        if "conversion" in t:
            unknown.append("conversion_rate")

    praise = []
    if calc == "demonstrated":
        praise.append("You compared the weeks using a rate, not just the totals.")
    if interp == "demonstrated":
        praise.append("You saw that bigger totals don't automatically mean better performance.")
    if all(tests.values()):
        praise.append("Your next step is specific and linked to what you found.")
    feedback = " ".join(praise) if praise else "Your answer judges the weeks on the totals alone."
    if all(tests.values()):
        rec_level = "demonstrated"
    elif not tests["consistent_with_data"] or not tests["proportionate"]:
        rec_level = "not_yet_shown"
    elif tests["tied_to_finding"] or tests["specific_and_checkable"]:
        rec_level = "partially"
    else:
        rec_level = "not_yet_shown"
    explanation = recommendation_explanation({"level": rec_level, "evidence": rec_ev, "tests": tests})
    if explanation:
        feedback += " " + explanation

    return {
        "guardrail": "none",
        "verdict": "no" if re.search(r"\bno\b|not |didn.?t|did not|" + DECREASE, t) else ("yes" if re.search(r"improv|better|increas", t) else "missing"),
        "skills": {
            "metric_calculation": {"level": calc, "evidence": calc_ev, "reason": "mock rule"},
            "interpretation": {"level": interp, "evidence": interp_ev, "reason": "mock rule"},
            "recommendation": {"level": rec_level, "evidence": rec_ev, "reason": "mock rule", "tests": tests},
        },
        "practice_notes": notes,
        "unknown_concepts": unknown,
        "feedback": feedback,
        "nudge": [],
        "needs_review": False,
    }


# ---------------------------------------------------------------- sessions and learners

SESSIONS = {}
LEARNERS = {}  # learner name (lowercase) -> list of session ids, oldest first

SESSIONS_DIR = ROOT / "sessions"
SAVE_SESSIONS = True  # the test runner turns this off


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "learner"


def session_stem(session):
    return f"{session['created'][:19].replace(':', '').replace('T', '_')}_{slug(session['name'])}_{session['id']}"


def save_session(session):
    """Write the session as JSON (reloaded on start) and as a readable transcript (for examples)."""
    if not SAVE_SESSIONS:
        return
    try:
        SESSIONS_DIR.mkdir(exist_ok=True)
        stem = session_stem(session)
        (SESSIONS_DIR / f"{stem}.json").write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")
        (SESSIONS_DIR / f"{stem}.md").write_text(transcript(session), encoding="utf-8")
    except OSError as e:
        print(f"[save] could not save session {session['id']}: {e}", file=sys.stderr)


def load_sessions():
    if not SESSIONS_DIR.exists():
        return 0
    loaded = []
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            loaded.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            print(f"[load] skipped {path.name}: {e}", file=sys.stderr)
    for session in sorted(loaded, key=lambda x: x.get("created", "")):
        if session.get("task_id") not in TASK_BY_ID:
            continue
        SESSIONS[session["id"]] = session
        LEARNERS.setdefault(session["learner_key"], []).append(session["id"])
    return len(SESSIONS)


LEVEL_LABEL = {"not_yet_shown": "Not yet shown", "partially": "Partially", "demonstrated": "Demonstrated"}
GUARDRAIL_LABEL = {"asked_for_answer": "asked for the answer", "abusive": "abusive language", "off_topic": "off topic"}
FINISH_LABEL = {"all_shown": "all three skills shown", "max_attempts": "all attempts used", "learner_finished": "learner chose to stop"}


def quote(text):
    return "\n".join("> " + line if line.strip() else ">" for line in text.splitlines())


def transcript(session):
    """A readable record of one session, ready to share as an example."""
    task = task_of(session)
    lines = [
        f"# {session['name']}: {task['title']}" + (f" (retake {session['retake_number']})" if session["retake_number"] else ""),
        "",
        f"Started {session['created'][:16].replace('T', ' ')}. Assessed with "
        + ("live AI (" + CONFIG["openai_model"] + ")" if session.get("mode") == "openai" else "simulated AI (mock rules)") + ". "
        + (f"Finished: {FINISH_LABEL.get(session['finish_reason'], 'finished')}." if session["finished"] else "In progress."),
        "",
        "## Task",
        "",
        "| " + " | ".join(task["columns"]) + " |",
        "|" + "---|" * len(task["columns"]),
    ] + ["| " + " | ".join(f"{v:,}" if isinstance(v, int) else str(v) for v in row) + " |" for row in task["rows"]] + [
        "",
        f"**Question:** {task['question']}",
        "",
        "## What happened",
        "",
    ]
    total = len(session["attempts"])
    for n in range(1, total + 2):
        for b in session["blocked"]:
            if b["before_attempt"] == n:
                lines += [f"**Blocked message, not counted** ({GUARDRAIL_LABEL.get(b['result']['guardrail'], 'blocked')})", "", quote(b["answer"]), "",
                          f"Tutor: {b['result']['feedback']}", ""]
        for h in session["hints"]:
            if h["before_attempt"] == n:
                lines += [f"**Hint requested before attempt {n}** (one per task, free, not scored)", "", f"Tutor: {h['hint']}", ""]
        if n > total:
            break
        a = session["attempts"][n - 1]
        r = a["result"]
        lines += [f"### Attempt {n}", "", "**Learner:**", "", quote(a["answer"]), "", f"**Feedback shown:** {r['feedback']}", ""]
        if r["nudge"]:
            lines += ["**Guiding questions shown:**"] + [f"- {x}" for x in r["nudge"]] + [""]
        if r["lessons"]:
            lines += ["**Lesson offered:** " + ", ".join(f"{l['title']} (simulated link)" for l in r["lessons"]), ""]
        lines += ["**Assessment (hidden from the learner):** "
                  + "; ".join(f"{SKILL_BY_ID[sid]['name']}: {LEVEL_LABEL[r['skills'][sid]['level']]}" for sid in SKILL_IDS), ""]
        if r["practice_notes"]:
            lines += ["**Practice notes:** " + ", ".join(NOTE_LABELS[c] for c in r["practice_notes"]), ""]
        if r["rule_log"]:
            lines += ["**Product rules applied:**"] + [f"- {x}" for x in r["rule_log"]] + [""]
    rep = session.get("report")
    if rep:
        lines += [
            "## Result",
            "",
            f"**Work sample score:** {rep['score']}/{rep['max_score']}",
            "",
            "| Skill | First shown | Points |",
            "|---|---|---|",
        ] + [f"| {r['name']} | {('Attempt ' + str(r['first_shown'])) if r['first_shown'] else 'Not yet shown'} | {r['points']}/{r['max_points']} |" for r in rep["skills"]] + [""]
        if rep["practice_notes"]:
            lines += ["**Practice notes:** " + "; ".join(rep["practice_notes"]), ""]
        lines += [
            f"**Learner's next step:** {rep['next_step']}",
            "",
            f"**What an employer can reasonably conclude:** {rep['employer_conclusion']}",
            "",
            f"**Shared with employer:** {'Yes' if session['shared'] else 'No'}",
            "",
        ]
    return "\n".join(lines)


def pick_task(previous_ids):
    """Rule: a retake always gets a task variant the learner hasn't seen, so it can't be memorised."""
    used = {SESSIONS[s]["task_id"] for s in previous_ids if s in SESSIONS}
    for task in TASKS:
        if task["id"] not in used:
            return task
    return TASKS[len(previous_ids) % len(TASKS)]  # all variants used: cycle


def new_session(name, task_id=None):
    display = (name or "").strip()[:60] or "Learner"
    key = display.lower()
    previous = LEARNERS.setdefault(key, [])
    task = TASK_BY_ID[task_id] if task_id in TASK_BY_ID else pick_task(previous)
    sid = uuid.uuid4().hex[:12]
    SESSIONS[sid] = {
        "id": sid,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "mode": MODE,
        "name": display,
        "learner_key": key,
        "task_id": task["id"],
        "retake_number": len(previous),
        "attempts": [],
        "blocked": [],
        "hints": [],
        "first_shown": {k: None for k in SKILL_IDS},
        "finished": False,
        "finish_reason": None,
        "shared": False,
        "report": None,
    }
    previous.append(sid)
    return SESSIONS[sid]  # saved from the learner's first action, so unused sessions leave no files


def task_of(session):
    return TASK_BY_ID[session["task_id"]]


def evaluation_input(session, answer, attempt_no):
    task = task_of(session)
    previous = [
        {"attempt": i + 1, "levels": {k: v["level"] for k, v in a["result"]["skills"].items()}}
        for i, a in enumerate(session["attempts"])
    ]
    return json.dumps({
        "TASK": public_task(task),
        "RUBRIC": model_rubric(),
        "ANSWER_KEY": model_key(task),
        "LESSONS": [{"id": l["id"], "title": l["title"]} for l in LESSON_LIST],
        "PRACTICE_NOTE_CODES": RUBRIC["practice_notes"],
        "PREVIOUS_ATTEMPTS": previous,
        "ATTEMPT_NUMBER": attempt_no,
        "CURRENT_ANSWER": answer,
    }, indent=2)


def evaluate(session, answer, attempt_no):
    task = task_of(session)
    if MODE == "mock":
        raw = mock_evaluate(answer, task)
    else:
        raw = llm_json(PROMPT_EVALUATE, evaluation_input(session, answer, attempt_no))
    return raw, apply_rules(raw, answer, task)


def submit_attempt(session, answer):
    if session["finished"]:
        return {"error": "This practice is finished. Retake with a new task to try again."}, 409
    answer = (answer or "").strip()
    if not answer:
        return {"error": "Write an answer before submitting."}, 400
    if len(answer) > 3000:
        return {"error": "Keep your answer under 3,000 characters."}, 400

    attempt_no = len(session["attempts"]) + 1
    try:
        raw, result = evaluate(session, answer, attempt_no)
    except LLMUnavailable as e:
        # Fallback: never count an attempt the learner didn't get feedback on.
        print(f"[llm] {e}", file=sys.stderr)
        return {"error": "Feedback isn't available right now, so this attempt wasn't counted. Try again in a moment."}, 503

    if result["guardrail"] != "none":
        session["blocked"].append({"before_attempt": attempt_no, "answer": answer, "result": result})
        save_session(session)
        return {
            "counted": False, "guardrail": result["guardrail"],
            "feedback": result["feedback"], "nudge": [], "lessons": [],
            "attempt": attempt_no - 1, "attempts_left": MAX_ATTEMPTS - (attempt_no - 1), "finished": False,
        }, 200

    session["attempts"].append({"answer": answer, "result": result, "raw": raw})
    for sid in SKILL_IDS:
        if session["first_shown"][sid] is None and result["skills"][sid]["level"] == "demonstrated":
            session["first_shown"][sid] = attempt_no
    all_shown = all(session["first_shown"][sid] for sid in SKILL_IDS)
    if all_shown:
        finish(session, "all_shown")
    elif attempt_no >= MAX_ATTEMPTS:
        finish(session, "max_attempts")
    save_session(session)

    return {
        "counted": True, "attempt": attempt_no, "attempts_left": MAX_ATTEMPTS - attempt_no,
        "feedback": result["feedback"], "nudge": result["nudge"], "lessons": result["lessons"],
        "finished": session["finished"], "all_shown": all_shown,
    }, 200


def earliest_gap(session):
    last = session["attempts"][-1]["result"]["skills"] if session["attempts"] else None
    for sid in SKILL_IDS:
        if session["first_shown"][sid] is None and (last is None or last[sid]["level"] != "demonstrated"):
            return sid
    return None


def give_hint(session, draft):
    if session["finished"]:
        return {"error": "This practice is finished."}, 409
    current = len(session["attempts"]) + 1
    if session["hints"]:
        # Rule: one free hint per task, across all attempts.
        return {"error": "You've already used your hint for this task."}, 409
    task = task_of(session)
    focus = earliest_gap(session)
    if focus is None:
        hint = "You've shown all three skills. Read your answer once more as if you were the manager receiving it."
    elif MODE == "mock":
        hint = task["coaching"][focus]["hint"]
    else:
        skill = SKILL_BY_ID[focus]
        fallback = task["coaching"][focus]["hint"]
        try:
            out = llm_json(PROMPT_HINT, json.dumps({
                "TASK": public_task(task),
                "FOCUS_SKILL": {k: skill[k] for k in ("name", "requirement", "levels")},
                "ANSWER_KEY": model_key(task),
                "DRAFT": (draft or "").strip(),
            }, indent=2))
            hint = str(out.get("hint") or "").strip() or fallback
        except LLMUnavailable as e:
            print(f"[llm] {e}", file=sys.stderr)
            hint = fallback
        said = squash(draft)
        if any(squash(t) in squash(hint) and squash(t) not in said for t in task["answer_key"]["leak_terms"]):
            hint = fallback  # Rule: a hint must never contain answer values.
    session["hints"].append({"before_attempt": current, "focus": focus, "hint": hint})
    save_session(session)
    return {"hint": hint}, 200


# ---------------------------------------------------------------- report

def score_rows(session):
    rows, total = [], 0
    for s in SKILLS:
        a = session["first_shown"][s["id"]]
        points = s["points_by_attempt"][a - 1] if a else 0
        total += points
        evidence = session["attempts"][a - 1]["result"]["skills"][s["id"]]["evidence"] if a else ""
        rows.append({
            "id": s["id"], "name": s["name"], "requirement": s["requirement"],
            "first_shown": a, "points": points, "max_points": s["points_by_attempt"][0], "evidence": evidence,
        })
    return rows, total


def template_summary(rows, notes):
    first = [r["name"] for r in rows if r["first_shown"] == 1]
    later = [r["name"] for r in rows if r["first_shown"] and r["first_shown"] > 1]
    missing = [r for r in rows if not r["first_shown"]]
    parts = []
    if first:
        parts.append("Shown on the first attempt: " + ", ".join(first) + ".")
    if later:
        parts.append("Shown after feedback: " + ", ".join(later) + ".")
    if missing:
        parts.append("Not yet shown: " + ", ".join(r["name"] for r in missing) + ".")
    parts.append("This is one short work sample, so it is limited evidence and does not on its own establish readiness for the role.")

    retake = "Then retake with a new task. It uses different data, so you show the skill rather than remember an answer."
    if missing:
        lesson = LESSONS[SKILL_BY_ID[missing[0]["id"]]["lesson"]]
        step = f"Learn the lesson \"{lesson['title']}\" ({lesson['minutes']} min). {retake}"
    elif notes:
        step = f"Work on this: {notes[0].lower()}. {retake}"
    else:
        step = "Retake with a new task, without hints, to show you can do this again on different data."
    return step, " ".join(parts)


def journey(session):
    out = []
    for i, a in enumerate(session["attempts"]):
        n = i + 1
        out.append({
            "attempt": n,
            "hints_before": [h["hint"] for h in session["hints"] if h["before_attempt"] == n],
            "blocked_before": [b["result"]["guardrail"] for b in session["blocked"] if b["before_attempt"] == n],
            "answer": a["answer"],
            "levels": {sid: a["result"]["skills"][sid]["level"] for sid in SKILL_IDS},
            "feedback": a["result"]["feedback"],
            "nudge": a["result"]["nudge"],
        })
    return out


def history(session):
    """Earlier work samples this learner has shared. Unshared practice stays private."""
    items = []
    for sid in LEARNERS.get(session["learner_key"], []):
        if sid == session["id"]:
            break
        other = SESSIONS.get(sid)
        if other and other["finished"] and other["shared"]:
            items.append({"task": TASK_BY_ID[other["task_id"]]["title"], "score": other["report"]["score"],
                          "max_score": MAX_SCORE, "retake_number": other["retake_number"]})
    return items


def build_report(session):
    task = task_of(session)
    rows, total = score_rows(session)
    last = session["attempts"][-1]["result"] if session["attempts"] else None
    notes = [NOTE_LABELS[c] for c in (last["practice_notes"] if last else [])]

    step, conclusion = template_summary(rows, notes)
    if MODE == "openai" and session["attempts"]:
        try:
            out = llm_json(PROMPT_SUMMARY, json.dumps({
                "LEARNER": session["name"],
                "TASK": {"title": task["title"], "question": task["question"]},
                "RESULTS": [{k: r[k] for k in ("name", "first_shown", "evidence")} for r in rows],
                "PRACTICE_NOTES": notes,
                "LESSONS": [{"id": l["id"], "title": l["title"], "minutes": l["minutes"]} for l in LESSON_LIST],
            }, indent=2))
            step = str(out.get("learner_next_step") or "").strip() or step
            conclusion = str(out.get("employer_conclusion") or "").strip() or conclusion
        except LLMUnavailable as e:
            print(f"[llm] {e}, using template summary", file=sys.stderr)

    return {
        "learner": session["name"],
        "role": RUBRIC["role"],
        "task": task["title"],
        "retake_number": session["retake_number"],
        "mode": MODE,
        "attempts_used": len(session["attempts"]),
        "max_attempts": MAX_ATTEMPTS,
        "hints_used": len(session["hints"]),
        "finish_reason": session["finish_reason"],
        "score": total,
        "max_score": MAX_SCORE,
        "skills": rows,
        "practice_notes": notes,
        "next_step": step,
        "employer_conclusion": conclusion,
        "needs_review": any(a["result"]["needs_review"] for a in session["attempts"]),
        "journey": journey(session),
        "history": [],
    }


def finish(session, reason):
    if not session["finished"]:
        session["finished"] = True
        session["finish_reason"] = reason
        session["report"] = build_report(session)
        save_session(session)
    return session["report"]


def shared_report(session):
    report = dict(session["report"])
    report["history"] = history(session)
    return report


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            body = (ROOT / "static" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/config":
            self.send_json({"mode": MODE, "model": CONFIG["openai_model"] if MODE == "openai" else None,
                            "max_attempts": MAX_ATTEMPTS, "role": RUBRIC["role"]})
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        data = self.read_json()
        if path == "/api/session":
            session = new_session(data.get("name", ""))
            self.send_json({"session_id": session["id"], "name": session["name"],
                            "retake_number": session["retake_number"], "task": public_task(task_of(session))})
            return
        session = SESSIONS.get(data.get("session_id", ""))
        if not session:
            self.send_json({"error": "Session not found. Start a new practice."}, 404)
            return
        if path == "/api/attempt":
            self.send_json(*submit_attempt(session, data.get("answer", "")))
        elif path == "/api/hint":
            self.send_json(*give_hint(session, data.get("draft", "")))
        elif path == "/api/finish":
            if not session["attempts"]:
                self.send_json({"error": "Submit at least one answer first."}, 400)
            else:
                self.send_json(finish(session, "learner_finished"))
        elif path == "/api/share":
            if not session["finished"]:
                self.send_json({"error": "Finish the practice before sharing."}, 400)
            else:
                session["shared"] = True
                save_session(session)
                self.send_json(shared_report(session))
        else:
            self.send_json({"error": "Not found"}, 404)


def main():
    count = load_sessions()
    port = int(os.environ.get("PORT", CONFIG.get("port", 8000)))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    label = f"live AI ({CONFIG['openai_model']})" if MODE == "openai" else "simulated AI (mock rules)"
    print(f"AI Practice Tutor running at http://localhost:{port} using {label}")
    print(f"Task variants loaded: {', '.join(t['title'] for t in TASKS)}")
    print(f"Saved sessions loaded: {count}. New sessions are saved to {SESSIONS_DIR.name}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
