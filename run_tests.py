#!/usr/bin/env python3
"""
Run the evaluation test cases and write tests/eval_log.md.

  python3 run_tests.py                 uses the mode in config.json
  python3 run_tests.py --mode openai   live LLM (needs OPENAI_API_KEY)
  python3 run_tests.py --mode mock     simulated AI, checks the plumbing only

The auto check compares levels and guardrails with the expected values, and checks
that no answer values leaked into the feedback. Your own pass/fail judgement on the
feedback quality goes in the 'Your judgement' line of the log.
"""
import argparse
import datetime
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

parser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=["mock", "openai"])
args = parser.parse_args()
if args.mode:
    os.environ["TUTOR_MODE"] = args.mode

import server  # noqa: E402  (imported after setting the mode)

server.SAVE_SESSIONS = False  # test runs go to tests/eval_log.md, not sessions/

LEVEL = {"not_yet_shown": "Not yet shown", "partially": "Partially", "demonstrated": "Demonstrated"}


def check(case, response, session):
    exp = case["expected"]
    problems = []
    if response.get("counted") != exp["counted"]:
        problems.append(f"counted: expected {exp['counted']}, got {response.get('counted')}")
    if not exp["counted"]:
        got = response.get("guardrail")
        if got != exp.get("guardrail"):
            problems.append(f"guardrail: expected {exp.get('guardrail')}, got {got}")
        return problems
    result = session["attempts"][-1]["result"]
    for sid, want in exp["levels"].items():
        got = result["skills"][sid]["level"]
        if got != want:
            problems.append(f"{sid}: expected {LEVEL[want]}, got {LEVEL[got]}")
    for note in exp.get("practice_notes", []):
        if note not in result["practice_notes"]:
            problems.append(f"missing practice note {note}")
    if any("Replaced with safe feedback" in line for line in result["rule_log"]):
        problems.append("model leaked answer values (caught by the leak guard)")
    return problems


def main():
    cases = json.loads((ROOT / "tests" / "test_cases.json").read_text(encoding="utf-8"))
    label = f"live AI ({server.CONFIG['openai_model']})" if server.MODE == "openai" else "simulated AI (mock rules)"
    lines = [
        "# Evaluation log",
        "",
        f"Run: {datetime.datetime.now():%Y-%m-%d %H:%M}. Assessed with {label}.",
        "",
        "Auto check compares levels and guardrails with expected values and checks for leaked answer values. "
        "Feedback quality needs your own judgement.",
        "",
    ]
    summary = []
    for case in cases:
        session = server.new_session(case["id"] + " " + case["title"], task_id=case.get("task", "email_campaign"))
        try:
            response, status = server.submit_attempt(session, case["answer"])
        except Exception as e:  # keep going so one failure doesn't hide the rest
            response, status = {"error": str(e)}, 500
        if status != 200:
            problems = [f"request failed: {response.get('error')}"]
            actual_levels, rule_log = "n/a", []
        else:
            problems = check(case, response, session)
            if response.get("counted"):
                res = session["attempts"][-1]["result"]
                actual_levels = "; ".join(f"{server.SKILL_BY_ID[s]['name']}: {LEVEL[res['skills'][s]['level']]}" for s in server.SKILL_IDS)
                rule_log = res["rule_log"]
                if res["practice_notes"]:
                    actual_levels += ". Practice notes: " + ", ".join(res["practice_notes"])
            else:
                actual_levels = f"Not counted (guardrail: {response.get('guardrail')})"
                rule_log = []
        verdict = "PASS" if not problems else "FAIL"
        summary.append((case["id"], case["title"], verdict))

        exp = case["expected"]
        exp_levels = (
            "; ".join(f"{server.SKILL_BY_ID[s]['name']}: {LEVEL[l]}" for s, l in exp["levels"].items())
            if exp["counted"] else f"Not counted (guardrail: {exp['guardrail']})"
        )
        lines += [
            f"## {case['id']}: {case['title']}",
            f"*{case['source']}. Task: {server.TASK_BY_ID[case.get('task', 'email_campaign')]['title']}*",
            "",
            f"**Input:** {case['answer']}",
            "",
            f"**Expected:** {exp_levels}. {exp['behaviour']}",
            "",
            f"**Actual:** {actual_levels}",
            "",
            f"**Feedback shown:** {response.get('feedback', response.get('error', ''))}",
            "",
        ]
        if response.get("nudge"):
            lines += ["**Nudges:**"] + [f"- {n}" for n in response["nudge"]] + [""]
        if response.get("lessons"):
            lines += ["**Lessons offered:** " + ", ".join(l["title"] for l in response["lessons"]), ""]
        if rule_log:
            lines += ["**Product rules applied:**"] + [f"- {r}" for r in rule_log] + [""]
        lines += [
            f"**Auto check:** {verdict}" + ("" if not problems else " (" + "; ".join(problems) + ")"),
            "",
            "**Your judgement:** _Pass / Fail, and why_",
            "",
        ]

    lines[6:6] = ["| Test | Case | Auto check |", "|---|---|---|"] + [f"| {i} | {t} | {v} |" for i, t, v in summary] + [""]
    out = ROOT / "tests" / "eval_log.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    passed = sum(1 for *_, v in summary if v == "PASS")
    print(f"{passed}/{len(summary)} passed the auto check. Log written to {out.relative_to(ROOT)}")
    for i, t, v in summary:
        print(f"  {i}  {v}  {t}")


if __name__ == "__main__":
    main()
