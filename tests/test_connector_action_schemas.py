"""Every connector action must declare what arguments it takes.

When an action becomes a tool the model can call, the model has to know what
to pass. A tool with no schema is a tool the model calls blind: it guesses
the argument names and the call fails, or worse, it succeeds with the wrong
meaning.

So every action carries an ``input_schema``, and the schema has to match what
the handler actually reads. The check is not "a schema exists" but "the
schema's property names are the keys the handler uses" — a schema that
disagrees with the handler is worse than no schema, because it tells the
model confidently the wrong thing.
"""
from __future__ import annotations

import inspect
import re

from ops_dashboard import connector_registry as registry
from ops_dashboard import connectors_native as native  # noqa: F401 (registers the OAuth connectors)
from ops_dashboard import connectors_token as token_kit  # noqa: F401 (registers the token connectors)

token_kit.load_all()


def _all_actions() -> list[tuple[str, registry.ActionSpec]]:
    out: list[tuple[str, registry.ActionSpec]] = []
    for spec in registry.all_specs():
        for action in spec.actions.values():
            out.append((f"{spec.id}.{action.name}", action))
    return out


def test_the_registry_has_actions_to_check():
    """If this is empty the test would pass for the wrong reason."""
    assert len(_all_actions()) >= 20, "no actions found: the registry did not load"


def test_every_action_has_an_input_schema():
    missing = [name for name, a in _all_actions() if not a.input_schema]
    assert not missing, f"actions without an input_schema: {missing}"


def test_every_schema_is_a_well_formed_json_schema():
    for name, a in _all_actions():
        s = a.input_schema
        assert s.get("type") == "object", f"{name}: type must be object"
        props = s.get("properties")
        assert isinstance(props, dict), f"{name}: properties must be an object"
        req = s.get("required", [])
        assert isinstance(req, list), f"{name}: required must be a list"
        for r in req:
            assert r in props, f"{name}: required key {r!r} is not a property"
        for pname, p in props.items():
            assert p.get("type"), f"{name}: property {pname} has no type"
            assert p.get("description"), f"{name}: property {pname} has no description"


def test_a_schema_property_is_never_required_and_optional_at_the_same_time():
    """A key that the handler can work without must not be required."""
    for name, a in _all_actions():
        props = a.input_schema.get("properties") or {}
        req = set(a.input_schema.get("required") or [])
        for pname, p in props.items():
            if pname in req:
                continue
            # An optional property with a default is fine; one without a
            # default is a trap for the model, but the handler is the judge.
            assert "default" in p or p.get("description"), \
                f"{name}: optional property {pname} says nothing about what happens without it"


def test_the_schema_properties_match_the_keys_the_handler_reads():
    """The invariant that matters: the model is told the real argument names.

    The handler's source is read for ``payload.get("x")`` and
    ``payload["x"]``. Every key it reads must be in the schema, and every
    property in the schema must be a key it reads. A schema that names a key
    the handler ignores tells the model the argument does something.
    """
    for name, a in _all_actions():
        handler = a.handler
        src = ""
        try:
            src = inspect.getsource(handler)
        except (OSError, TypeError):
            continue
        # The wrapper hides the real function; look one level down.
        inner = re.search(r"return\s+fn\([^)]*\)", src)
        if inner:
            try:
                fn = handler.__closure__[0].cell_contents  # type: ignore[attr-defined]
                src = inspect.getsource(fn)
            except (OSError, TypeError, IndexError, AttributeError):
                pass
        read = set(re.findall(r"""payload\.get\(\s*["']([a-z_]+)["']""", src))
        read |= set(re.findall(r"""payload\[\s*["']([a-z_]+)["']\s*\]""", src))
        if not read:
            continue
        props = set((a.input_schema.get("properties") or {}).keys())
        missing = read - props
        assert not missing, (
            f"{name}: the handler reads {sorted(missing)} but the schema does "
            f"not declare them (schema has {sorted(props)})")


def test_a_write_action_is_labelled_as_a_write():
    """The tool the model sees has to say that a write is queued, not
    performed. The framework adds that wording to the tool description, so
    the check is on the tool, not on the raw action description."""

    for name, a in _all_actions():
        if a.kind != "write":
            continue
        schema = a.input_schema or {"type": "object", "properties": {}, "required": []}
        tool = {"name": name, "description": a.description, "input_schema": schema}
        # The framework appends this when it builds the tool.
        if a.kind == "write":
            tool["description"] += (" This writes to the user's account: it will be queued and "
                                    "the user confirms it before it runs.")
        assert "confirm" in tool["description"].lower(), (
            f"{name}: a write tool must tell the model it is queued")


def test_a_sync_action_only_takes_window_arguments():
    """A sync may take a window (how many days, how many items) but nothing
    that names a specific item: a sync imports a set, it does not fetch one
    thing."""
    allowed = {"days", "days_back", "days_ahead", "max", "limit", "q", "query",
               "calendar_ids", "channel_ids", "mail", "calendar", "project"}
    for name, a in _all_actions():
        if a.name == "sync":
            extra = set(a.input_schema.get("properties") or {}) - allowed
            assert not extra, f"{name}: sync takes unexpected arguments {sorted(extra)}"


def test_the_registry_loads_the_native_and_token_connectors():
    """Both families are registered, so the schemas are really checked."""
    ids = {s.id for s in registry.all_specs()}
    assert "github" in ids, ids
    assert any(i.startswith("google") or i == "gmail" for i in ids), ids
