from __future__ import annotations

import json

import pytest

from retrace import parsers
from conftest import claude_msg, codex_meta, codex_msg, jsonl


def test_claude_string_content():
    got = parsers.parse_claude(claude_msg("user", "plain string"), "fb")
    assert got["role"] == "user" and got["text"] == "plain string"


def test_claude_block_list():
    got = parsers.parse_claude(claude_msg("assistant", [
        {"type": "thinking", "thinking": "hmm"},
        {"type": "text", "text": "answer"},
    ]), "fb")
    assert got["text"] == "hmm\nanswer"


def test_claude_sidechain_role_suffix():
    got = parsers.parse_claude(claude_msg("user", "sub work", isSidechain=True), "fb")
    assert got["role"] == "user/sub"


def test_claude_summary_line():
    got = parsers.parse_claude(
        {"type": "summary", "summary": "a recap", "leafUuid": "leaf-1"}, "fb")
    assert got["role"] == "summary" and got["text"] == "a recap"
    assert got["session"] == "fb" and got["key"] == "leaf-1"


def test_claude_entry_without_message_is_skipped():
    assert parsers.parse_claude({"type": "system", "content": "x"}, "fb") is None


def test_codex_response_item():
    got = parsers.parse_codex(codex_msg("assistant", "keyset cursor"), "fb")
    assert got["role"] == "assistant" and "keyset" in got["text"]


def test_codex_event_and_state_are_skipped():
    for kind in ("event_msg", "state"):
        assert parsers.parse_codex({"type": kind, "payload": {"x": 1}}, "fb") is None


def test_codex_meta_carries_cwd_and_id():
    got = parsers.parse_codex(codex_meta(sid="sid-1", cwd="/w"), "fb")
    assert got == {"role": "meta", "ts": "2026-06-02T09:00:00Z", "text": "",
                   "session": "sid-1", "cwd": "/w", "key": None}


def test_codex_function_call_needs_include_tools():
    obj = {"type": "response_item",
           "payload": {"type": "function_call", "name": "shell",
                       "arguments": {"cmd": "ls"}}}
    assert parsers.parse_codex(obj, "fb")["text"] == ""
    assert "[tool:shell]" in parsers.parse_codex(obj, "fb", True)["text"]


def test_harvest_depth_is_bounded():
    node = cur = {}
    for _ in range(40):
        cur["nested"] = {}
        cur = cur["nested"]
    cur["text"] = "too deep"
    out: list[str] = []
    parsers.harvest(node, out)
    assert out == []


def test_harvest_survives_hostile_shapes():
    for hostile in (
        {"type": {"type": "string"}},
        {"type": ["a"], "text": "kept"},
        {"content": [None, 3, "bare string", {"text": "kept too"}]},
        {"role": [1, 2], "message": {"content": {"text": "deep"}}},
    ):
        out: list[str] = []
        parsers.harvest(hostile, out)                 # must not raise
        assert all(isinstance(s, str) for s in out)


def test_prescan_reads_cwd_sid_and_name(env):
    p = (env["codex"] / "2026" / "06" / "03" /
         "rollout-2026-06-03T09-00-00-ffffffff-1111-2222-3333-444444444444.jsonl")
    jsonl(p, [codex_meta(sid="real-sid", cwd="/home/max/api"),
              {"type": "response_item",
               "payload": {"type": "message", "role": "user", "id": "call-999",
                           "sessionName": "renamed later",
                           "content": [{"type": "input_text", "text": "hi"}]}}])
    got = parsers.prescan("codex", p)
    assert got == {"project": "/home/max/api", "sid": "real-sid",
                   "cli_name": "renamed later"}


def test_prescan_falls_back_to_the_filename_uuid(env):
    uid = "abcdef01-2345-6789-abcd-ef0123456789"
    p = env["codex"] / f"rollout-2026-06-03T09-00-00-{uid}.jsonl"
    jsonl(p, [codex_msg("user", "no meta line here")])
    assert parsers.prescan("codex", p)["sid"] == uid


def test_prescan_ignores_a_tool_call_id(env):
    p = env["codex"] / "rollout-2026-06-03T09-00-00-nouuid.jsonl"
    jsonl(p, [{"type": "response_item", "payload": {"type": "function_call",
                                                    "id": "call_should_not_win"}}])
    assert parsers.prescan("codex", p)["sid"] is None


def test_prescan_on_a_missing_file_is_quiet(env):
    got = parsers.prescan("claude", env["claude"] / "-a-b" / "gone.jsonl")
    assert got == {"project": "/a/b", "sid": None, "cli_name": None}


def test_prescan_rejects_absurd_names(env):
    p = env["claude"] / "-tmp-n" / "n.jsonl"
    jsonl(p, [{"type": "user", "title": "x" * 500,
               "message": {"role": "user", "content": "hi"}}])
    assert parsers.prescan("claude", p)["cli_name"] is None


@pytest.mark.parametrize("v,expected_type", [
    ({"a": 1}, str), ([1], str), (1, str), (1.5, str), (True, str), (None, type(None)),
])
def test_as_text_types(v, expected_type):
    assert isinstance(parsers.as_text(v), expected_type)


def test_as_text_respects_the_limit():
    assert len(parsers.as_text("x" * 500, 10)) == 10
    assert len(parsers.as_text("x" * 500, None)) == 500


def test_as_text_on_unserialisable_objects():
    class Weird:
        def __repr__(self):
            return "<weird>"

    assert isinstance(parsers.as_text({"k": Weird()}), str)
