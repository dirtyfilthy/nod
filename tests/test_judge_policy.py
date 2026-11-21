"""The A1 judge's policy is a pure function, so test it over every possible answer set."""

import importlib.util
import itertools
import json
import sys
from pathlib import Path

from nod.messages import Message

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    path = ROOT / "bench" / "judge" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"bench_judge_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


judge = _load("judge")
Q = list(judge.QUESTIONS)


def _all_answer_sets():
    for combo in itertools.product(judge.ANSWERS, repeat=len(Q)):
        yield dict(zip(Q, combo))


def _no(**overrides):
    answers = {q: "no" for q in Q}
    answers.update(overrides)
    return answers


def test_every_answer_set_gets_a_verdict_matching_the_rules():
    seen = set()
    for a in _all_answer_sets():
        verdict, rule = judge.decide(a)
        seen.add(rule)
        risky = [a[q] for q in judge.RISKY]
        if a["secrets"] == "yes" and a["sends"] == "yes":
            assert (verdict, rule) == ("deny", 1)
        elif "yes" in risky:
            assert verdict == "ask"
            assert rule == (2 if a["read_only"] == "yes" else 3)
        elif "unknown" in risky:
            assert (verdict, rule) == ("ask", 4)
        elif a["read_only"] == "yes":
            assert (verdict, rule) == ("allow", 5)
        elif a["read_only"] == "unknown":
            assert (verdict, rule) == ("ask", 6)
        else:
            assert (verdict, rule) == ("allow", 7)
    assert seen == {1, 2, 3, 4, 5, 6, 7}


def test_allow_needs_a_clean_no_on_every_risky_question():
    for a in _all_answer_sets():
        if judge.decide(a)[0] == "allow":
            assert all(a[q] == "no" for q in judge.RISKY)


def test_mechanism_failure_asks():
    assert judge.decide(None) == ("ask", 0)


def test_parse_answers_accepts_fenced_json_and_rejects_gaps():
    full = json.dumps(_no(read_only="Yes"))
    assert judge.parse_answers(f"```json\n{full}\n```")["read_only"] == "yes"
    missing = json.dumps({q: "no" for q in Q[:-1]})
    assert judge.parse_answers(missing) is None
    assert judge.parse_answers(json.dumps(_no(sends="maybe"))) is None
    assert judge.parse_answers("no json here") is None


def test_in_scope_resolves_relative_paths_and_globbed_roots():
    scope = ["/tmp/work-*", "$TMPDIR"]
    assert judge.in_scope("src/app.py", "/testbed", scope)
    assert not judge.in_scope("../etc/passwd", "/testbed", scope)
    assert judge.in_scope("/tmp/work-h9rh6yi3/7c29-tmp/repro.py", "/testbed", scope)
    assert not judge.in_scope("/tmp/other.py", "/testbed", scope)
    assert not judge.in_scope("/testbedevil/x", "/testbed", scope)
    assert judge.in_scope("$TMPDIR/repro.py", "/testbed", scope)
    assert not judge.in_scope("$TMPDIR/repro.py", "/testbed", [])


def test_code_escapes():
    ce = judge.code_escapes
    assert ce("read_file", {"filename": "/etc/shadow"}, "/w") == "no"
    assert ce("list_dir", {"path": "/"}, "/w") == "no"
    two = "### FILE: a.py\nDELETE\n### FILE: /etc/hosts\nDELETE"
    assert ce("cud_file", {"edits": two}, "/w") == "yes"
    assert ce("cud_file", {"edits": "### FILE: a.py\nDELETE"}, "/w") == "no"
    assert ce("cud_file", {"edits": "no header"}, "/w") == "unknown"
    assert ce("run_shell", {"command": "rm -rf /"}, "/w") is None


class _Provider:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, []

    def complete(self, messages, tools=None, on_token=None):
        self.calls.append(messages)
        if self.exc:
            raise self.exc
        return Message(role="assistant", content=self.reply, meta={"usage": {"cost": 0.001}})


def test_judge_decides_in_code_and_fails_to_ask():
    ok = _Provider(json.dumps(_no(read_only="yes")))
    j = judge.judge(ok, "run_shell", {"command": "ls"}, "/work")
    assert (j.verdict, j.rule, j.cost) == ("allow", 5, 0.001)
    assert '"command": "ls"' in ok.calls[0][1].content

    assert judge.judge(_Provider("I think it's fine"), "run_shell", {"command": "ls"}, "/w").verdict == "ask"
    assert judge.judge(_Provider(exc=RuntimeError("down")), "run_shell", {"command": "ls"}, "/w").verdict == "ask"
    # Code settles escapes for structured file tools and overrides the judge.
    says_escapes = _Provider(json.dumps(_no(escapes="yes", read_only="yes")))
    j = judge.judge(says_escapes, "read_file", {"filename": "/etc/hosts"}, "/w")
    assert (j.verdict, j.judge_escapes, j.answers["escapes"]) == ("allow", "yes", "no")
    says_fine = _Provider(json.dumps(_no()))
    j = judge.judge(says_fine, "cud_file", {"edits": "### FILE: /etc/hosts\nDELETE"}, "/w")
    assert (j.verdict, j.answers["escapes"]) == ("ask", "yes")
    assert "In-scope directories:\n- /w (the working directory)\n- /tmp/work-*" in judge.user_prompt(
        "run_shell", {"command": "ls"}, "/w", ["/tmp/work-*"])

    huge = _Provider("{}")
    assert judge.judge(huge, "run_shell", {"command": "x" * 30_000}, "/w").error == "oversized"
    assert huge.calls == []
