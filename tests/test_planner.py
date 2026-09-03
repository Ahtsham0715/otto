"""Plan validation: the model proposes, the code decides."""

from __future__ import annotations

import json

import pytest

from otto.agentloop.planner import (
    MAX_STEPS,
    PlanInvalid,
    build_plan_messages,
    parse_plan,
    validate_plan,
)


def plan_of(*steps, rationale="because") -> dict:
    return {"rationale": rationale, "steps": list(steps)}


def step(**kw) -> dict:
    base = {"id": "s1", "agent": "mac", "description": "Open Safari"}
    base.update(kw)
    return base


def test_a_minimal_plan_validates(services):
    plan = validate_plan(plan_of(step()), services.roster, services.registry)
    assert len(plan) == 1
    assert plan.steps[0].agent == "mac"
    assert plan.rationale == "because"


def test_a_plan_may_name_a_concrete_tool_call(services):
    plan = validate_plan(
        plan_of(step(tool="open_app", args={"name": "Safari"})),
        services.roster,
        services.registry,
    )
    assert plan.steps[0].tool == "open_app"
    assert plan.steps[0].args == {"name": "Safari"}


@pytest.mark.parametrize("raw", [None, [], "steps", {"steps": []}, {"steps": "s1"}, {}])
def test_malformed_plans_are_rejected(services, raw):
    with pytest.raises(PlanInvalid):
        validate_plan(raw, services.roster, services.registry)


def test_an_unknown_agent_rejects_the_whole_plan(services):
    with pytest.raises(PlanInvalid, match="does not exist"):
        validate_plan(
            plan_of(step(), step(id="s2", agent="wizard", description="magic")),
            services.roster,
            services.registry,
        )


def test_the_supervisor_and_planner_cannot_be_plan_targets(services):
    for agent in ("supervisor", "planner"):
        with pytest.raises(PlanInvalid, match="cannot be assigned"):
            validate_plan(plan_of(step(agent=agent)), services.roster, services.registry)


def test_an_unknown_tool_is_rejected(services):
    with pytest.raises(PlanInvalid, match="does not exist"):
        validate_plan(
            plan_of(step(tool="hack_the_mainframe")), services.roster, services.registry
        )


def test_a_tool_the_agent_may_not_use_is_rejected(services):
    """This is the load-bearing one: a plan cannot route around the roster."""
    with pytest.raises(PlanInvalid, match="may not use"):
        validate_plan(
            plan_of(step(agent="research", tool="write_file",
                         args={"path": "x", "content": "y"})),
            services.roster,
            services.registry,
        )


def test_steps_need_descriptions(services):
    with pytest.raises(PlanInvalid, match="no description"):
        validate_plan(plan_of(step(description="")), services.roster, services.registry)


def test_duplicate_ids_are_rejected(services):
    with pytest.raises(PlanInvalid, match="duplicate"):
        validate_plan(plan_of(step(), step()), services.roster, services.registry)


def test_a_dangling_dependency_is_rejected(services):
    with pytest.raises(PlanInvalid, match="not in the plan"):
        validate_plan(
            plan_of(step(depends_on=["nope"])), services.roster, services.registry
        )


def test_self_dependency_is_rejected(services):
    with pytest.raises(PlanInvalid, match="depends on itself"):
        validate_plan(
            plan_of(step(depends_on=["s1"])), services.roster, services.registry
        )


def test_a_dependency_cycle_is_rejected(services):
    with pytest.raises(PlanInvalid, match="cycle"):
        validate_plan(
            plan_of(
                step(id="s1", depends_on=["s2"]),
                step(id="s2", depends_on=["s1"], description="other"),
            ),
            services.roster,
            services.registry,
        )


def test_too_many_steps_are_rejected(services):
    steps = [step(id=f"s{i}", description=f"do {i}") for i in range(MAX_STEPS + 1)]
    with pytest.raises(PlanInvalid, match="at most"):
        validate_plan(plan_of(*steps), services.roster, services.registry)


def test_args_must_be_an_object(services):
    with pytest.raises(PlanInvalid, match="args must be an object"):
        validate_plan(
            plan_of(step(tool="open_app", args=["Safari"])),
            services.roster,
            services.registry,
        )


def test_a_single_dependency_may_be_a_bare_string(services):
    plan = validate_plan(
        plan_of(step(), step(id="s2", description="then", depends_on="s1")),
        services.roster,
        services.registry,
    )
    assert plan.steps[1].depends_on == ("s1",)


# -- waves ------------------------------------------------------------------


def test_independent_steps_share_a_wave(services):
    plan = validate_plan(
        plan_of(
            step(id="a", description="one"),
            step(id="b", description="two"),
            step(id="c", description="three", depends_on=["a", "b"]),
        ),
        services.roster,
        services.registry,
    )
    waves = plan.waves()
    assert [len(w) for w in waves] == [2, 1]
    assert {s.id for s in waves[0]} == {"a", "b"}
    assert waves[1][0].id == "c"


def test_a_chain_is_fully_serial(services):
    plan = validate_plan(
        plan_of(
            step(id="a", description="one"),
            step(id="b", description="two", depends_on=["a"]),
            step(id="c", description="three", depends_on=["b"]),
        ),
        services.roster,
        services.registry,
    )
    assert [len(w) for w in plan.waves()] == [1, 1, 1]


# -- prompt + parsing -------------------------------------------------------


def test_parse_plan_accepts_a_fenced_reply(services):
    text = "```json\n" + json.dumps(plan_of(step())) + "\n```"
    assert len(parse_plan(text, services.roster, services.registry)) == 1


def test_parse_plan_rejects_prose(services):
    with pytest.raises(PlanInvalid):
        parse_plan("I'll open Safari for you!", services.roster, services.registry)


def test_the_prompt_lists_real_agents_and_tools(services):
    messages = build_plan_messages(
        "open safari", services.roster, services.registry, []
    )
    user = messages[1].content
    assert "mac:" in user and "files:" in user
    assert "open_app" in user
    assert "supervisor" not in user.split("Tools:")[0]


def test_memory_is_folded_into_the_prompt(services):
    services.memory.remember("projects", "my projects live in ~/Projects")
    memories = services.memory.search("projects")
    messages = build_plan_messages(
        "run the tests", services.roster, services.registry, memories
    )
    assert "~/Projects" in messages[1].content


def test_a_rejection_reason_is_fed_back(services):
    messages = build_plan_messages(
        "x", services.roster, services.registry, [], retry_reason="agent 'wizard' does not exist"
    )
    assert "was rejected" in messages[1].content
    assert "wizard" in messages[1].content
