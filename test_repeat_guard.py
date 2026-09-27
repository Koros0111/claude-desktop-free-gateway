"""THE REPORTED BUG: the model repeats the same promise and nothing gets done.
Three self-inflicted loop causes, all inside this gateway:
  1. a zero-argument tool call (TaskList) was dropped -> no tool_result came
     back, so the model asked the same thing every turn;
  2. a prose answer to the STRICT-JSON decision prompt threw away ALL 31 tools
     of the turn, so the turn ended with "دارم می‌نویسم" and no file;
  3. nothing ever told the model it had already promised the same thing.
Run: python test_repeat_guard.py
"""
import os, sys
os.environ.setdefault("GATEWAY_API_KEY", "test-key")
os.environ["COMBO_PATH"] = "combo.json"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gateway as g
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

fails = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (("  -> " + str(extra)) if extra else ""))
    if not cond:
        fails.append(name)


TOOLS = [
    {"name": "TaskList", "description": "list tasks",
     "input_schema": {"type": "object",
                      "properties": {"status": {"type": "string"}}, "required": []}},
    # The shape Claude Desktop local-agent-mode actually sends for a no-argument
    # tool: an object schema with NO properties at all. This is what used to be
    # dropped ("needs an input object") and caused the loop.
    {"name": "ClearGoal", "description": "clear the goal",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "Bare", "description": "declared object, nothing listed",
     "input_schema": {"type": "object"}},
    {"name": "Read", "description": "read",
     "input_schema": {"type": "object",
                      "properties": {"file_path": {"type": "string"}},
                      "required": ["file_path"]}},
    {"name": "Write", "description": "write",
     "input_schema": {"type": "object",
                      "properties": {"file_path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["file_path", "content"]}},
    {"name": "Mystery", "description": "never declared a schema"},
]
BY = {t["name"]: t for t in TOOLS}
NAMES = set(BY)

# --- 1. a zero-argument call is a REAL call --------------------------------
check("zero-arg: TaskList {} is forwarded",
      g._input_is_usable("TaskList", {}, BY) is True)
check("zero-arg: the real Claude Desktop shape (properties: {}) is forwarded",
      g._input_is_usable("ClearGoal", {}, BY) is True)
check("zero-arg: a bare object schema demands nothing",
      g._input_is_usable("Bare", {}, BY) is True)
check("zero-arg: Read {} is still refused",
      g._input_is_usable("Read", {}, BY) is False)
check("zero-arg: a tool that never declared a schema is refused",
      g._input_is_usable("Mystery", {}, BY) is False)
check("zero-arg: unknown tool is refused",
      g._input_is_usable("Whatever", {}, BY) is False)
check("zero-arg: Read with a path is fine",
      g._input_is_usable("Read", {"file_path": "a.py"}, BY) is True)

dec = {"content": [{"type": "tool_use", "name": "TaskList", "input": {}}]}
kept = g._validate_decision_items(dec, NAMES, BY)
check("zero-arg: survives validation",
      kept and kept[0]["type"] == "tool_use" and kept[0]["name"] == "TaskList", kept)

# --- 2. broken promises are counted (Persian AND English) ------------------
fa = [{"role": "assistant", "content": [{"type": "text",
                                         "text": "باشه — دارم داشبورد را می‌سازم"}]},
      {"role": "user", "content": "ادامه بده"},
      {"role": "assistant", "content": [{"type": "text",
                                         "text": "الان فایل را می‌نویسم"}]},
      {"role": "user", "content": "ادامه بده"}]
check("promise: Persian promises counted",
      g.detect_broken_promises(fa) == 2, g.detect_broken_promises(fa))

acting = fa + [{"role": "assistant", "content": [
    {"type": "tool_use", "id": "x", "name": "Write",
      "input": {"file_path": "C:\\demo\\dashboard.html", "content": "<html>"}}]}]
check("promise: an actual tool call resets the streak",
      g.detect_broken_promises(acting) == 0, g.detect_broken_promises(acting))

answered = [{"role": "assistant", "content": [{"type": "text",
                                               "text": "پروژه ۲۹۲ هزار ستاره دارد"}]}]
check("promise: a plain answer is not a promise",
      g.detect_broken_promises(answered) == 0)
check("promise: English promise counted",
      g.detect_broken_promises([{"role": "assistant", "content":
                                 "I'll write the file now"}]) == 1)
check("promise: empty history is safe", g.detect_broken_promises([]) == 0)

TOOLS_ONE = [{"name": "Write", "description": "w",
              "input_schema": {"type": "object"}}]
warn = g.build_tool_decision_prompt(TOOLS_ONE, None, "t", "", 0, 4000,
                                    promise_streak=2)
check("promise: the model is told about it", "REPEAT GUARD" in warn)
check("promise: the guard forbids re-announcing",
      "Announcing that you are about to write is forbidden" in warn)
quiet = g.build_tool_decision_prompt(TOOLS_ONE, None, "t", "", 0, 4000)
check("promise: no guard for a healthy turn", "REPEAT GUARD" not in quiet)
# 0 must mean "guard off", not "guard on every turn" (a streak of 0 would
# otherwise satisfy `0 >= 0` on a healthy request).
_off = g.PROMISE_GUARD
try:
    g.PROMISE_GUARD = False
    noisy = g.build_tool_decision_prompt(TOOLS_ONE, None, "t", "", 0, 4000,
                                         promise_streak=9)
    check("promise: threshold 0 disables the guard (never always-on)",
          "REPEAT GUARD" not in noisy)
finally:
    g.PROMISE_GUARD = _off

# --- 3. a prose answer to the JSON decision prompt gets ONE re-ask ---------
calls = {"n": 0}
PROSE = "باشه — دارم داشبورد RTL را می‌سازم و فایل را داخل پوشه می‌گذارم."
REPLY = ('{"content": [{"type": "tool_use", "name": "Write", "input": '
          '{"file_path": "C:\\\\demo\\\\dashboard.html", "content": "<html>"}}]}')


def prose_then_json(client, alias, system, prompt, image_paths=None,
                    deadline=None, work_dir=None):
    calls["n"] += 1
    if calls["n"] == 1:
        return PROSE, "m1", {"input_tokens": 1, "output_tokens": 1}
    assert "FORMAT ERROR" in prompt, "the re-ask must say what was wrong"
    return REPLY, "m2", {"input_tokens": 1, "output_tokens": 1}


real_cwf = g.chat_with_fallback
g.chat_with_fallback = prose_then_json
try:
    raw, model, usage, items = g._decision_worker(
        "claude-opus-4-5", "PROMPT", None, None, NAMES, None, 9e9, None, BY, [])
    check("retry: prose reply triggered exactly one re-ask", calls["n"] == 2, calls["n"])
    check("retry: the tool call from the re-ask is used",
          items and items[0]["type"] == "tool_use" and items[0]["name"] == "Write", items)
    check("retry: the prose is not returned as the answer",
          all(i["type"] != "text" for i in items), items)

    calls["n"] = 0
    g.DECISION_JSON_RETRY = False
    raw, model, usage, items = g._decision_worker(
        "claude-opus-4-5", "PROMPT", None, None, NAMES, None, 9e9, None, BY, [])
    check("retry: kill switch stops the extra call", calls["n"] == 1, calls["n"])
    check("retry: with the switch off the prose is still forwarded, not a 502",
          items and items[0]["type"] == "text" and PROSE in items[0]["text"], items)
    g.DECISION_JSON_RETRY = True

    calls["n"] = 0

    def prose_twice(client, alias, system, prompt, image_paths=None,
                    deadline=None, work_dir=None):
        calls["n"] += 1
        return PROSE, "m1", {"input_tokens": 1, "output_tokens": 1}

    g.chat_with_fallback = prose_twice
    raw, model, usage, items = g._decision_worker(
        "claude-opus-4-5", "PROMPT", None, None, NAMES, None, 9e9, None, BY, [])
    check("retry: gives up after ONE extra try (never a retry loop)",
          calls["n"] == 2, calls["n"])
    check("retry: still answers with text instead of failing",
          items and items[0]["type"] == "text", items)

    calls["n"] = 0

    def failing(client, alias, system, prompt, image_paths=None,
                deadline=None, work_dir=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return PROSE, "m1", {"input_tokens": 1, "output_tokens": 1}
        raise g.UpstreamError("all upstream models failed: timeout")

    g.chat_with_fallback = failing
    raw, model, usage, items = g._decision_worker(
        "claude-opus-4-5", "PROMPT", None, None, NAMES, None, 9e9, None, BY, [])
    check("retry: an upstream error in the re-ask cannot break the turn",
          items and items[0]["type"] == "text", items)
finally:
    g.chat_with_fallback = real_cwf

print()
print("FAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)

