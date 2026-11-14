#!/usr/bin/env python3
"""Negative controls for PR #141 (the agent shift).

A test that passes proves nothing until you see it fail. Each mutation below
breaks the code on purpose; the named test MUST go red. If a mutation leaves
the suite green, that test is not watching what it claims to watch.

Run it from the worktree:  python3 tests/negative_controls_agent_duties.py
Every file it touches is restored afterwards, whatever happens.

This file is not a pytest test: pytest only collects test_*.py, and this one
mutates the source tree, which a test run must never do.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(Path.home() / "ops-dashboard" / ".venv" / "bin" / "python")

# (file, what to replace, what to replace it with, the test that must fail)
MUTATIONS = [
    # --- the shift itself -------------------------------------------------
    ("ops_dashboard/duty.py",
     'SHIFT_SOURCE = "shift"',
     'SHIFT_SOURCE = "manual"',
     "test_putting_an_agent_on_shift_creates_its_own_trigger"),

    ("ops_dashboard/duty.py",
     "    else:\n"
     "        trigger_id = int(existing[0][\"id\"]) if existing else 0\n"
     "        if existing:\n"
     "            db_agents.update_trigger(trigger_id, enabled=False)",
     "    else:\n"
     "        trigger_id = int(existing[0][\"id\"]) if existing else 0",
     "test_taking_an_agent_off_shift_keeps_the_record_of_what_it_did"),

    ("ops_dashboard/duty.py",
     "        if existing:\n"
     "            trigger_id = int(existing[0][\"id\"])\n"
     "            db_agents.update_trigger(trigger_id, spec=duty[\"cron\"],\n"
     "                                     prompt=duty[\"prompt\"], enabled=True)\n"
     "        else:",
     "        if False:\n"
     "            trigger_id = int(existing[0][\"id\"])\n"
     "            db_agents.update_trigger(trigger_id, spec=duty[\"cron\"],\n"
     "                                     prompt=duty[\"prompt\"], enabled=True)\n"
     "        else:",
     "test_going_back_on_shift_does_not_pile_up_triggers"),

    ("ops_dashboard/duty.py",
     "    triggers = [t for t in db_agents.list_triggers(agent_id, enabled_only=False)\n"
     "                if (t.get(\"source\") or \"manual\") == SHIFT_SOURCE]",
     "    triggers = list(db_agents.list_triggers(agent_id, enabled_only=False))",
     "test_a_trigger_a_person_created_is_not_a_shift"),

    ("ops_dashboard/duty.py",
     "        if not duty:\n"
     "            raise ValueError(\"no_duty_for_role\")",
     "        if not duty:\n"
     "            duty = {\"cron\": \"0 9 * * *\", \"prompt\": \"anything\"}",
     "test_an_agent_whose_role_has_no_brief_cannot_go_on_shift"),

    ("ops_dashboard/duty.py",
     "    for duty in duties():\n"
     "        if duty[\"role\"].lower() == want or duty[\"pack_id\"].lower() == want:\n"
     "            return duty\n"
     "    return None",
     "    for duty in duties():\n"
     "        if duty[\"role\"].lower() == want or duty[\"pack_id\"].lower() == want:\n"
     "            return duty\n"
     "    return duties()[0] if duties() else None",
     "test_the_brief_of_a_role_is_found_by_its_name"),

    # --- the switch must never be locked (the bot's finding) --------------
    ("ops_dashboard/duty.py",
     "    if enabled:\n"
     "        if not role:\n"
     "            raise ValueError(\"no_agent\")\n"
     "        duty = duty_for(role)\n"
     "        if not duty:\n"
     "            raise ValueError(\"no_duty_for_role\")\n",
     "    if not role:\n"
     "        raise ValueError(\"no_agent\")\n"
     "    duty = duty_for(role)\n"
     "    if not duty:\n"
     "        raise ValueError(\"no_duty_for_role\")\n"
     "    if enabled:\n",
     "test_a_shift_can_be_turned_off_after_the_role_changes"),

    ("ops_dashboard/routes_duty.py",
     "        if not brief and not shift.get(\"enabled\") and not shift.get(\"trigger_id\"):\n"
     "            continue",
     "        if not brief:\n"
     "            continue",
     "test_the_panel_shows_an_agent_whose_role_changed"),

    # --- the brief --------------------------------------------------------
    ("ops_dashboard/skills/packs.py",
     "    cron = str(duty.get(\"cron\") or \"\").strip()",
     "    cron = str(duty.get(\"cron\") or \"\").strip()\n"
     "    return {}",
     "test_every_pack_of_the_c_suite_has_a_standing_brief"),

    ("ops_dashboard/skills/packs/cto/pack.json",
     '"cron": "10 9 * * 1-5"',
     '"cron": "0 8 * * 1-5"',
     "test_a_shift_never_lands_on_the_minute_of_a_process_of_the_app"),

    ("ops_dashboard/skills/packs/cto/pack.json",
     "code-review-rigor y technical-debt-triage",
     "los procesos de la semana",
     "test_the_brief_is_written_from_the_skills_of_the_role"),

    ("ops_dashboard/skills/packs/cto/pack.json",
     '"cron": "10 9 * * 1-5"',
     '"cron": "25 9 * * 1-5"',
     "test_no_two_agents_wake_at_the_same_minute"),

    # --- the routes -------------------------------------------------------
    ("ops_dashboard/routes_duty.py",
     '@router.post("/api/agents/<agent_id>/shift")',
     '@router.post("/api/agents/{agent_id}/shift")',
     "test_the_shift_route_is_registered_with_a_path_that_can_match"),

    ("ops_dashboard/routes_duty.py",
     '@router.post("/api/agents/<agent_id>/shift")\n@_local_only',
     '@router.post("/api/agents/<agent_id>/shift")',
     "test_the_shift_is_local_only"),

    ("ops_dashboard/routes_duty.py",
     "        elif code == \"no_duty_for_role\":\n"
     "            _err(handler, 404, \"no_duty\",\n"
     "                 \"this agent's role has no standing brief\")",
     "        elif code == \"no_duty_for_role\":\n"
     "            _err(handler, 200, \"no_duty\",\n"
     "                 \"this agent's role has no standing brief\")",
     "test_an_agent_without_a_brief_is_not_found"),

    ("ops_dashboard/routes_duty.py",
     "    try:\n"
     "        agent = int(agent_id)\n"
     "    except (TypeError, ValueError):\n"
     "        _err(handler, 400, \"bad_agent\", \"not an agent\")\n"
     "        return",
     "    try:\n"
     "        agent = int(agent_id)\n"
     "    except (TypeError, ValueError):\n"
     "        agent = 0",
     "test_a_broken_id_is_a_bad_request"),

    ("ops_dashboard/routes_duty.py",
     '            "on_shift": bool(shift.get("enabled")),',
     '            "on_shift": False,',
     "test_the_duties_endpoint_lists_the_roles_and_who_is_on_shift"),

    # --- the wake-up ------------------------------------------------------
    ("ops_dashboard/scheduler.py",
     "        skills_block = _skills_of(agent_id, prompt)\n"
     "        if skills_block:\n"
     "            system_prompt += \"\\n\\n## Your skills\\n\" + skills_block",
     "        skills_block = \"\"",
     "test_an_agent_wakes_up_with_its_own_pack"),

    # --- the migration ----------------------------------------------------
    ("ops_dashboard/migrations.py",
     "        conn.execute(\"ALTER TABLE agent_triggers ADD COLUMN source TEXT \"\n"
     "                     \"NOT NULL DEFAULT 'manual'\")",
     "        return",
     "test_a_trigger_that_existed_before_the_shift_is_a_manual_one"),

    # --- the page ---------------------------------------------------------
    ("ops_dashboard/web/agents.html",
     'id="shifts"',
     'id="turnos"',
     "test_the_agents_page_shows_the_shifts"),

    ("ops_dashboard/web/i18n/agents.es.json",
     '"shift_on": "Poner de turno",',
     "",
     "test_both_languages_say_the_same_about_the_shifts"),

    ("ops_dashboard/web/i18n/agents.es.json",
     '"shift_stale": "Este rol ya no tiene parte de turno. El agente sigue con el anterior.",',
     "",
     "test_both_languages_say_the_same_about_the_shifts"),
]


def run(test_name: str) -> tuple[bool, str]:
    """Run the one test; True when it failed (which is what we want)."""
    proc = subprocess.run(
        [PY, "-m", "pytest", "tests/test_agent_duties.py", "-q", "-p", "no:cacheprovider",
         "-k", test_name],
        cwd=ROOT, capture_output=True, text=True, timeout=600)
    out = proc.stdout + proc.stderr
    return proc.returncode != 0, out


def main() -> int:
    failures = []
    for rel, old, new, test_name in MUTATIONS:
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"SKIP  {test_name}: the pattern is not in {rel}")
            failures.append((test_name, "pattern not found"))
            continue
        try:
            path.write_text(original.replace(old, new, 1), encoding="utf-8")
            red, out = run(test_name)
        finally:
            path.write_text(original, encoding="utf-8")
        if red:
            print(f"RED   {test_name}  ({rel})")
        else:
            print(f"GREEN {test_name}  ({rel})  <-- the test does not watch this")
            failures.append((test_name, "still green"))
            print("\n".join(out.strip().splitlines()[-12:]))

    print()
    if failures:
        print(f"{len(failures)} of {len(MUTATIONS)} mutations left the test green:")
        for name, why in failures:
            print(f"  - {name}: {why}")
        return 1
    print(f"all {len(MUTATIONS)} mutations turned their test red")
    return 0


if __name__ == "__main__":
    sys.exit(main())
