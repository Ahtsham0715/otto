"""The single dispatch path: schema, permission, ceiling, audit, verification."""

from __future__ import annotations

import pytest

from otto.core.agents import AgentSpec
from otto.core.permissions import Permission
from otto.core.state import Status
from otto.tools.registry import SchemaError, ToolSpec, always_true, validate_args


# -- schema validation ------------------------------------------------------


def _spec(**kw) -> ToolSpec:
    defaults = dict(
        name="demo",
        description="d",
        schema={"a": {"type": "string"}, "n": {"type": "integer", "default": 1}},
        required=("a",),
        handler=lambda ctx, a, n=1: {"a": a, "n": n},
        verifier=always_true,
    )
    defaults.update(kw)
    return ToolSpec(**defaults)


def test_missing_required_argument_is_rejected():
    with pytest.raises(SchemaError, match="missing required"):
        validate_args(_spec(), {})


def test_unknown_arguments_are_rejected_not_ignored():
    with pytest.raises(SchemaError, match="unknown argument"):
        validate_args(_spec(), {"a": "x", "surprise": 1})


def test_types_are_enforced():
    with pytest.raises(SchemaError, match="must be integer"):
        validate_args(_spec(), {"a": "x", "n": "three"})


def test_a_boolean_is_not_an_integer():
    with pytest.raises(SchemaError, match="boolean"):
        validate_args(_spec(), {"a": "x", "n": True})


def test_defaults_are_applied():
    assert validate_args(_spec(), {"a": "x"}) == {"a": "x", "n": 1}


def test_enum_is_enforced():
    spec = _spec(schema={"a": {"type": "string", "enum": ["x", "y"]}}, required=("a",))
    validate_args(spec, {"a": "x"})
    with pytest.raises(SchemaError, match="must be one of"):
        validate_args(spec, {"a": "z"})


def test_string_length_and_array_size_are_capped():
    spec = _spec(schema={"a": {"type": "string", "max_length": 4}}, required=("a",))
    with pytest.raises(SchemaError, match="longer than"):
        validate_args(spec, {"a": "abcde"})


def test_a_tool_cannot_require_an_argument_absent_from_its_schema(services):
    with pytest.raises(ValueError, match="not in schema"):
        services.registry.register(_spec(name="bad", required=("nope",)))


# -- dispatch ---------------------------------------------------------------


def test_unknown_tool_fails_and_is_audited(services, ctx_for):
    ctx = ctx_for("mac")
    call = services.registry.dispatch(ctx, "no_such_tool", {})
    assert call.status is Status.FAILED
    assert "no such tool" in call.error
    assert services.audit.count("tool_failed") == 1


def test_an_agent_cannot_use_a_tool_it_does_not_hold(services, ctx_for):
    """Research reads; it must not be able to reach a writing tool by name."""
    ctx = ctx_for("research")
    call = services.registry.dispatch(ctx, "write_file", {"path": "x", "content": "y"})
    assert call.status is Status.FAILED
    assert "not permitted to use" in call.error


def test_ceiling_refuses_without_even_asking_the_human(services, ctx_for):
    """A SAFE-ceiling agent is refused even when the human would approve."""
    services.broker.set_auto(True)  # human says yes to everything
    spec = services.roster.require("research")
    widened = AgentSpec(
        **{**spec.__dict__, "tools": spec.tools + ("write_file",)}
    )
    services.roster.add(widened)

    ctx = ctx_for("research")
    call = services.registry.dispatch(
        ctx, "write_file", {"path": str(services.config.home) + "/Desktop/x.txt",
                            "content": "hi"}
    )
    assert call.status is Status.FAILED
    assert "ceiling" in call.error
    assert services.audit.count("refused_by_ceiling") == 1
    # And nothing was written.
    assert not (services.sandbox.roots[0] / "x.txt").exists()


def test_a_denial_is_recorded_and_marks_requires_human(denying, ctx_for):
    ctx = ctx_for("files")
    call = denying.registry.dispatch(
        ctx, "make_folder", {"path": str(denying.config.home) + "/Desktop/nope"}
    )
    assert call.status is Status.REQUIRES_HUMAN
    assert denying.audit.count("refused_by_human") == 1
    assert not (denying.sandbox.roots[0] / "nope").exists()


def test_a_failed_verification_fails_the_call(services, ctx_for):
    """Even when the handler claims success."""
    services.registry.register(
        _spec(
            name="liar",
            handler=lambda ctx: {"claimed": "done"},
            verifier=lambda ctx, args, result: (False, "nothing actually happened"),
            schema={},
            required=(),
        )
    )
    spec = services.roster.require("mac")
    services.roster.add(AgentSpec(**{**spec.__dict__, "tools": spec.tools + ("liar",)}))

    call = services.registry.dispatch(ctx_for("mac"), "liar", {})
    assert call.status is Status.FAILED
    assert call.verified is False
    assert "nothing actually happened" in call.error
    assert services.audit.count("verification_failed") == 1


def test_a_raising_verifier_is_a_failure_not_a_crash(services, ctx_for):
    def boom(ctx, args, result):
        raise RuntimeError("verifier exploded")

    services.registry.register(
        _spec(name="boomer", schema={}, required=(), handler=lambda ctx: 1,
              verifier=boom)
    )
    spec = services.roster.require("mac")
    services.roster.add(AgentSpec(**{**spec.__dict__, "tools": spec.tools + ("boomer",)}))
    call = services.registry.dispatch(ctx_for("mac"), "boomer", {})
    assert call.status is Status.FAILED
    assert "verifier exploded" in call.verification_detail


def test_a_raising_handler_is_captured(services, ctx_for):
    call = services.registry.dispatch(ctx_for("mac"), "open_app", {"name": "Nope"})
    assert call.status is Status.FAILED
    assert "not installed" in call.error


def test_a_cancelled_task_executes_nothing(services, ctx_for, task):
    task.cancel()
    call = services.registry.dispatch(ctx_for("mac"), "open_app", {"name": "Safari"})
    assert call.status is Status.CANCELLED
    assert services.mac.frontmost_app() == "Finder"  # never opened


def test_successful_calls_are_attached_to_the_subtask(services, ctx_for, task):
    ctx = ctx_for("mac")
    services.registry.dispatch(ctx, "open_app", {"name": "Safari"})
    assert task.subtasks[0].calls[-1].tool == "open_app"
    assert task.calls[-1].verified is True


def test_every_registered_tool_has_a_verifier(services):
    for name in services.registry.names():
        assert services.registry.get(name).verifier is not None


def test_audit_redacts_secrets(services, ctx_for):
    ctx = ctx_for("mac")
    services.registry.dispatch(
        ctx, "write_clipboard", {"text": "my key is sk-ABCDEFGHIJKLMNOPQRSTUV12345"}
    )
    dumped = str(services.audit.recent())
    assert "sk-ABCDEFGHIJKLMNOPQRSTUV12345" not in dumped
    assert "[redacted]" in dumped


# -- what the human is actually shown --------------------------------------


def _plausible_args(spec):
    """One value per schema field, of the right type."""
    samples = {
        "string": "sample",
        "integer": 1,
        "number": 1.0,
        "boolean": False,
        "array": ["ls"],
        "object": {},
    }
    return {k: samples[v.get("type", "string")] for k, v in spec.schema.items()}


def _prompting_tools(services):
    """Tools that can actually raise an approval dialog."""
    for name in services.registry.names():
        spec = services.registry.get(name)
        if spec.level_for(_plausible_args(spec)) is not Permission.SAFE:
            yield name, spec


def test_every_confirmation_prompt_renders(services):
    """A template naming a key the tool does not have falls back to dumping the
    raw arguments into the dialog — which for write_file meant showing the entire
    file. The prompt is the last thing between the user and an action they cannot
    undo, so it has to be readable."""
    checked = 0
    for name, spec in _prompting_tools(services):
        args = _plausible_args(spec)
        # Through the registry, which is what supplies the derived `_spoken`
        # values the templates use.
        rendered = services.registry._confirm_text(spec, args, services.config.home)
        assert rendered, name
        assert "{" not in rendered, f"{name}: unrendered placeholder in {rendered!r}"
        assert rendered != "Allow {tool}?".format(tool=name), (
            f"{name} prompts the human but never says what it is about to do"
        )
        checked += 1
    assert checked >= 5, "expected several tools to require confirmation"


def test_no_confirmation_prompt_dumps_a_large_value(services):
    """Specifically: the prompt must not contain a whole file."""
    spec = services.registry.get("write_file")
    args = {"path": "/Users/apple/Desktop/notes.txt", "content": "x" * 5000,
            "append": False}
    rendered = services.registry._confirm_text(spec, args, "/Users/apple")
    assert "x" * 100 not in rendered
    assert "notes.txt" in rendered
    assert len(rendered) < 200
    # And it is phrased the way it will be heard, not as a raw path.
    assert rendered == "Save changes to notes.txt on your Desktop?"


def test_a_broken_template_still_produces_something(services):
    """And if one ever does break, the fallback must not raise."""
    from otto.core.permissions import Permission
    from otto.tools.registry import ToolSpec, always_true

    spec = ToolSpec(
        name="oops",
        description="d",
        schema={"a": {"type": "string"}},
        required=("a",),
        handler=lambda ctx, a: a,
        verifier=always_true,
        permission=Permission.CONFIRM,
        confirm_template="{missing_key} please?",
    )
    assert services.registry._confirm_text(spec, {"a": "x"})


def test_paths_are_described_the_way_a_person_would_say_them():
    """This is the sentence Otto says most often, so it has to sound like
    English rather than a filesystem path read out one slash at a time."""
    from otto.tools.registry import friendly_path

    home = "/Users/apple"
    assert friendly_path("/Users/apple/Desktop/Invoices", home) == (
        "Invoices on your Desktop"
    )
    assert friendly_path("/Users/apple/Documents/report.md", home) == (
        "report.md in your Documents"
    )
    assert friendly_path("/Users/apple/Desktop", home) == "your Desktop"
    # Deeper paths keep enough context to be unambiguous.
    assert "app" in friendly_path("/Users/apple/Projects/app/tests", home)
    # Anything outside home stays exact: a friendly-but-wrong description of
    # what is about to change would be worse than a long one.
    assert friendly_path("/etc/hosts", home) == "/etc/hosts"
    assert friendly_path("", home) == ""
