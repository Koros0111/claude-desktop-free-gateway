"""Tool-surface parity: everything Claude Desktop may ask for must survive the bridge.

Offline: imports gateway.py, no network, no server. The point is that a user on
Claude Desktop cannot tell the backend is not Anthropic - so a declared tool must
never be dropped, misnamed, or answered with a stop_reason that hides the truth.

Run:  .venv\\Scripts\\python.exe test_tool_parity.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway as g  # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -> {extra}" if extra else ""))
    if not ok:
        FAILS.append(name)


def _tool(name, props, required=None, desc="tool"):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props,
                             "required": list(required or [])}}


# A realistic Claude Desktop / Cowork surface, including the shapes that used to
# break: {"properties": {}} zero-argument tools, namespaces, optional-only args.
TOOLS = [
    _tool("Read", {"file_path": {"type": "string"}, "offset": {"type": "number"},
                   "limit": {"type": "number"}}, ["file_path"]),
    _tool("Write", {"file_path": {"type": "string"}, "content": {"type": "string"}},
          ["file_path", "content"]),
    _tool("Edit", {"file_path": {"type": "string"}, "old_string": {"type": "string"},
                   "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}},
          ["file_path", "old_string", "new_string"]),
    _tool("Glob", {"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]),
    _tool("Grep", {"pattern": {"type": "string"}, "path": {"type": "string"},
                   "glob": {"type": "string"}}, ["pattern"]),
    _tool("Bash", {"command": {"type": "string"}, "description": {"type": "string"},
                   "timeout": {"type": "number"}}, ["command"]),
    _tool("TaskList", {}, []),
    _tool("ClearGoal", {}, []),
    _tool("ListMcpResources", {}, []),
    _tool("TaskCreate", {"subject": {"type": "string"},
                         "description": {"type": "string"}}, ["subject"]),
    _tool("TodoWrite", {"todos": {"type": "array"}}, ["todos"]),
    _tool("WebFetch", {"url": {"type": "string"}, "prompt": {"type": "string"}},
          ["url", "prompt"]),
    _tool("WebSearch", {"query": {"type": "string"}}, ["query"]),
    _tool("ExitPlanMode", {"plan": {"type": "string"}}, ["plan"]),
    _tool("SlashCommand", {"command": {"type": "string"}}, ["command"]),
    {"name": "LegacyNoSchema", "description": "no input_schema at all"},
]
BY_NAME = {t["name"]: t for t in TOOLS}
NAMES = set(BY_NAME)
ZERO_ARG = [t["name"] for t in TOOLS
            if isinstance(t.get("input_schema"), dict)
            and not t["input_schema"].get("properties")
            and not t["input_schema"].get("required")]

# ---- 1. zero-argument calls are legal and must reach the client -------------
check("zero-arg tools detected", sorted(ZERO_ARG) == ["ClearGoal", "ListMcpResources",
                                                       "TaskList"], str(ZERO_ARG))
for name in ZERO_ARG:
    check(f"{name} {{}} is forwarded", g._input_is_usable(name, {}, BY_NAME) is True)

# ---- 2. nothing is dropped silently ----------------------------------------
check("Read {} still refused (schema demands file_path)",
      g._input_is_usable("Read", {}, BY_NAME) is False)
check("tool with no schema at all is refused",
      g._input_is_usable("LegacyNoSchema", {}, BY_NAME) is False)
# Incomplete args are FORWARDED: the client answers with an error tool_result
# naming the missing field, which is how the model learns to fix it.
check("Write with path but no content is forwarded, not dropped",
      g._input_is_usable("Write", {"file_path": "x.md"}, BY_NAME) is True)
kept = g._validate_decision_items(
    {"content": [{"type": "tool_use", "name": "Write",
                   "input": {"file_path": "C:\\demo\\project\\x.md"}}]},
    NAMES, BY_NAME)
check("incomplete call survives validation", bool(kept) and kept[0]["name"] == "Write",
      str(kept))

# ---- 3. names the model mutates are fixed, not discarded -------------------
check("exact name unchanged", g._canonical_tool_name("TaskList", NAMES) == "TaskList")
check("lowercase fixed", g._canonical_tool_name("tasklist", NAMES) == "TaskList")
check("mcp namespace stripped",
      g._canonical_tool_name("mcp__tools__TaskList", NAMES) == "TaskList")
check("namespace with underscores stripped",
      g._canonical_tool_name("mcp__srv_name__TaskCreate", NAMES) == "TaskCreate")
check("one-character slip fixed", g._canonical_tool_name("TodoWrit", NAMES) == "TodoWrite")
check("invented tool still refused", g._canonical_tool_name("Teleport", NAMES) is None)
kept = g._validate_decision_items(
    {"content": [{"type": "text", "text": "ok"},
                 {"type": "tool_use", "name": "mcp__tools__TaskList", "input": {}},
                 {"type": "tool_use", "name": "Teleport", "input": {}}]},
    NAMES, BY_NAME)
names = [i.get("name") for i in kept or [] if i["type"] == "tool_use"]
check("near-miss renamed, hallucination dropped", names == ["TaskList"], str(kept))
check("text block kept alongside tool", any(i["type"] == "text" for i in kept or []))
check("only hallucination -> None (caller re-asks)",
      g._validate_decision_items({"content": [{"type": "tool_use", "name": "Teleport",
                                               "input": {}}]}, NAMES, BY_NAME) is None)

# ---- 4. parallel calls: the cap follows the client -------------------------
check("cap is at least 10 parallel calls", g.MAX_TOOL_USE_BLOCKS >= 10,
      str(g.MAX_TOOL_USE_BLOCKS))
check("decision prompt states the real cap",
      f"At most {g.MAX_TOOL_USE_BLOCKS} tool_use" in g.TOOL_DECISION_ROLE)
many = {"content": [{"type": "tool_use", "name": "Read",
                     "input": {"file_path": f"f{i}.py"}} for i in range(1, 15)]}
kept = g._validate_decision_items(many, NAMES, BY_NAME)
check("14 calls -> cap kept, none lost to silence",
      len(kept) == g.MAX_TOOL_USE_BLOCKS, f"kept {len(kept) if kept else 0}")

# ---- 5. every declared tool is offered to the model ------------------------
# the repo root itself: exists on every clone, never a scratch dir.
prompt = g.build_tool_decision_prompt(TOOLS, None, "user: hi", "sys", 0, 4000,
                                       project_paths=[str(Path(__file__).resolve().parent)])
missing = [t["name"] for t in TOOLS if t["name"] not in prompt]
check("every tool appears in the decision prompt", not missing, str(missing))

# ---- 6. stop_reason tells the truth ---------------------------------------
check("tool_use wins", g._stop_reason(True, True) == "tool_use")
check("clean text turn", g._stop_reason(False, False) == "end_turn")
check("cut-off answer says max_tokens", g._stop_reason(False, True) == "max_tokens")
check("upstream 'length' detected", g._is_truncated({"_finish": "length"}) is True)
check("upstream 'stop' not truncated", g._is_truncated({"_finish": "stop"}) is False)
check("no finish info -> not truncated", g._is_truncated({}) is False)
check("openai: cut-off says length", g._openai_finish({"_finish": "length"}) == "length")
check("openai: clean turn says stop", g._openai_finish({"_finish": "stop"}) == "stop")
check("openai: no finish info says stop", g._openai_finish({}) == "stop")
check("openai: never leaks anthropic names",
      g._openai_finish({"_finish": "length"}) in ("stop", "length"))

# ---- 7. runtime scratch dirs can never win as "the project" ---------------
# synthetic fixtures only ("demo" user, nothing from a real machine).
for p in [r"C:\Users\demo\AppData\Local\Claude-3p\local-agent-mode-sessions\a\b\outputs",
          r"C:\Users\demo\AppData\Local\Claude-3p\Cache\Everything",
          r"C:\Users\demo\AppData\Local\Claude-3p\vm_bundles\bundle",
          r"C:\Users\demo\AppData\Local\Temp\claude\scratch",
          r"C:\Users\demo\AppData\Roaming\skills-plugin\tools"]:
    check(f"scratch excluded: {Path(p).name}", g._is_scratch_path(p) is True)
for p in [r"E:\projects\my-app", r"E:\projects\my-app\outputs",
          r"C:\Users\demo\source\repos\claude-tools", r"D:\work\projects\my-app"]:
    check(f"real project kept: {p}", g._is_scratch_path(p) is False)

# ---- 8. the real tool surface is recorded for auditing --------------------
with tempfile.TemporaryDirectory() as td:
    path = Path(td) / "tool_catalog.json"
    old_env = os.environ.get("GATEWAY_TOOL_CATALOG_PATH")
    old_seen = set(g._catalog_seen)
    try:
        os.environ["GATEWAY_TOOL_CATALOG_PATH"] = str(path)
        g._catalog_seen.clear()
        g.record_tool_catalog(TOOLS)
        saved = json.loads(path.read_text(encoding="utf-8"))
        check("catalog written", saved.get("tool_count") == len(TOOLS),
              str(saved.get("tool_count")))
        tools_saved = saved.get("tools", {})
        check("catalog flags zero-arg tools",
              tools_saved.get("TaskList", {}).get("zero_arg") is True
              and tools_saved.get("Read", {}).get("zero_arg") is False,
              json.dumps(tools_saved.get("TaskList", {}))[:160])
        check("catalog spots a tool with no schema",
              tools_saved.get("LegacyNoSchema", {}).get("has_schema") is False,
              json.dumps(tools_saved.get("LegacyNoSchema", {}))[:160])
        check("catalog keeps required keys",
              tools_saved.get("Edit", {}).get("required") == ["file_path", "new_string",
                                                              "old_string"],
              str(tools_saved.get("Edit", {}).get("required")))
        before = path.stat().st_mtime_ns
        g.record_tool_catalog(TOOLS)  # same tool list again: must not rewrite
        check("repeat request does not rewrite the catalog",
              path.stat().st_mtime_ns == before)
        g.record_tool_catalog([_tool("NewTool", {"a": {"type": "string"}}, ["a"])])
        saved = json.loads(path.read_text(encoding="utf-8"))
        check("new tool merged in", "NewTool" in saved["tools"]
              and "TaskList" in saved["tools"])
    finally:
        if old_env is None:
            os.environ.pop("GATEWAY_TOOL_CATALOG_PATH", None)
        else:
            os.environ["GATEWAY_TOOL_CATALOG_PATH"] = old_env
        g._catalog_seen.clear()
        g._catalog_seen.update(old_seen)

print("-" * 60)
print("FAILED: " + (", ".join(FAILS) if FAILS else "none"))
sys.exit(1 if FAILS else 0)
