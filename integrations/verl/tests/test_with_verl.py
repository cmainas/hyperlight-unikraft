"""verl-hyperlight in a real verl: found through its entry points at verl's import, and scored by verl's own
Sandbox Fusion code. Skipped without verl installed, and without RUN_ALL, hluk and /dev/kvm."""

import json
import os
import shutil

import pytest


sandbox_fusion = pytest.importorskip("verl.utils.reward_score.sandbox_fusion")
pytestmark = pytest.mark.skipif(
    not (os.getenv("RUN_ALL") and (os.getenv("HLUK_PATH") or shutil.which("hluk")) and os.access("/dev/kvm", os.W_OK)),
    reason="requires RUN_ALL, hluk and /dev/kvm",
)

URL = "hyperlight://" + (os.path.abspath(os.environ["HLUK_INITRD"]) if os.getenv("HLUK_INITRD") else "")
ADD = "```python\na, b = map(int, input().split())\nprint(a + b)\n```"
CASES = {"inputs": ["1 2\n", "3 4\n"], "outputs": ["3\n", "7\n"]}


def score(completion, cases):
    return sandbox_fusion.compute_score(URL, None, 1024, completion, json.dumps(cases), continuous=True)[0]


def test_verl_finds_the_runner():
    from verl.utils.reward_score.sandbox_fusion import utils

    # The plugin, imported with verl's plugins, registered the runner with a verl that has sandbox
    # runners, or had an earlier one route hyperlight:// here
    if hasattr(utils, "register_sandbox_runner"):
        assert utils.get_sandbox_runner("hyperlight").__name__ == "run_code"
    else:
        assert getattr(utils.call_sandbox_api, "_hyperlight", False)


def test_correct_and_wrong_solutions():
    assert score(ADD, CASES) == 1.0
    assert score(ADD.replace("a + b", "a * b"), CASES) == 0.0


def test_call_based_solution():
    # verl's own wrapper for fn_name problems runs in the guest with the solution
    cases = {"inputs": ["[1, 2, 3]", "[]"], "outputs": ["6", "0"], "fn_name": "total"}
    assert score("```python\ndef total(xs):\n    return sum(xs)\n```", cases) == 1.0


def test_a_solution_cannot_forge_its_reward():
    # Under prime_code, which runs the code in a process forked from the grader, this rewrites the
    # grader's result list and scores 1.0
    forger = (
        "```python\nimport sys\nf = sys._getframe()\nwhile f:\n    r = f.f_locals.get('result')\n"
        "    if r is not None and hasattr(r, 'append') and not isinstance(r, (list, str)):\n"
        "        r.append([True] * 64)\n    f = f.f_back\nprint('wrong')\n```"
    )
    assert score(forger, CASES) == 0.0
