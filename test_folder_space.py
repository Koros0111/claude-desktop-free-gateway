"""THE REPORTED BUG: "why is nothing in my folder?" - the space in the path.
The gateway's own path regex stopped at the first space, so a project like
"C:\\My Project" was read as "C:\\My", failed is_dir(), and the session ran
in Claude Desktop's ephemeral ...\\outputs folder instead.
Run: python test_folder_space.py
"""
import base64, json, os, shutil, sys, tempfile, time
from pathlib import Path
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


def tmp_outside_scratch(prefix):
    """mkdtemp, but never under %TEMP%.

    The gateway deliberately refuses to treat anything inside the OS temp
    folder as a project (Claude's runtime scratch files live there), so a
    test folder created in %TEMP% would be filtered out FOR THE RIGHT REASON
    and hide the behaviour under test. The workspace's parent folder is used
    instead; everything created here is removed again at the end.
    """
    candidates = [Path(__file__).resolve().parent.parent, Path(os.path.expanduser("~"))]
    for parent in candidates:
        try:
            if parent.is_dir():
                return Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))
        except Exception:
            continue
    return Path(tempfile.mkdtemp(prefix=prefix))


# A folder that really exists and really has a space in it.
ROOT = tmp_outside_scratch("gw space proj ")
INNER = ROOT / "src files"
INNER.mkdir()
STR = str(ROOT)
SCRATCH = str(ROOT / "AppData" / "local-agent-mode-sessions" / "abc" / "outputs")

try:
    # --- 1. a path with a space survives extraction -----------------------
    chat = g.discover_project_paths(
        [{"role": "user", "content": "وارد پوشه بشو: " + STR + " و داشبورد را بساز"}])
    check("chat: space path is NOT truncated", STR in chat, chat)
    check("chat: the truncated half is never returned",
          all(p != STR.split(" ")[0] for p in chat), chat)

    tool_msg = g.discover_project_paths([{
        "role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "Read",
                                          "input": {"file_path": str(INNER)}}]}])
    check("chat: folder inside a space path is kept",
          any(p.startswith(STR) for p in tool_msg), tool_msg)

    # --- 2. the system prompt announces the project -----------------------
    env_sys = ("# Environment\n1. Platform: win32\n2. Working Directory: "
               + STR + "\n3. Date: 9/26/2026\n")
    check("system: announced path keeps its space",
          g.discover_cwd_paths(env_sys) == [STR], g.discover_cwd_paths(env_sys))

    greedy = g.discover_cwd_paths("Working directory: " + STR
                                 + " and the repo is a git repo")
    check("system: greedy match is cut back to the real folder",
          greedy == [STR], greedy)

    # --- 3. Claude's own session dir must never outrank the project -------
    agent_sys = ("You are in local agent mode.\nLocal outputs folder: " + SCRATCH
                 + "\nWorking directory: " + STR + "\n")
    got = g.discover_cwd_paths(agent_sys)
    check("system: Claude's outputs dir is not a project", got == [STR], got)
    check("system: the user's folder comes first", got[:1] == [STR], got)

    both = g.discover_project_paths([{"role": "user", "content": SCRATCH + " " + STR}])
    check("chat: scratch dir filtered, project kept", both == [STR], both)

    # --- 4. the session really runs in the user's folder ------------------
    check("resolve: real folder wins over the scratch dir",
          g.resolve_session_dir([SCRATCH, STR]) == ROOT,
          g.resolve_session_dir([SCRATCH, STR]))
    check("resolve: scratch dir alone falls back to the sandbox, not itself",
          g.resolve_session_dir([SCRATCH]) == g.SESSION_DIR,
          g.resolve_session_dir([SCRATCH]))
    check("resolve: our sandbox is still refused",
          g.resolve_session_dir([str(INNER), str(g.SESSION_DIR)]) == INNER)

    # --- 5. the prompt tells the model not to cut the path ----------------
    prompt = g.build_tool_decision_prompt(
        [{"name": "Write", "description": "w", "input_schema": {"type": "object"}}],
        None, "t", "", 0, 4000, project_paths=g.discover_cwd_paths(env_sys))
    check("prompt: quotes the spaced path", '"' + STR + '"' in prompt)
    check("prompt: warns about the space", "contains a SPACE" in prompt)
    check("prompt: forbids cutting it", "NEVER cut it at the space" in prompt)

    listed = g.build_tool_decision_prompt(
        [{"name": "Write", "description": "w", "input_schema": {"type": "object"}}],
        None, "t", "", 0, 4000, project_paths=[STR, str(INNER)])
    check("prompt: later paths are marked as older context",
          "older context" in listed and str(INNER) in listed)

    # --- 6. the same screenshot is not written once per turn --------------
    img_dir = Path(tempfile.mkdtemp(prefix="gw img "))
    real_dir, g.IMG_DIR = g.IMG_DIR, img_dir
    try:
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
            "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        b64 = base64.b64encode(png).decode()
        block = {"type": "image", "source": {"type": "base64",
                                             "media_type": "image/png", "data": b64}}
        turn = [{"role": "user", "content": [block]}]
        f1, o1, i1, s1 = g.extract_request_images(turn)
        f2, o2, i2, s2 = g.extract_request_images(turn)  # Claude re-sends it
        check("vision: identical image is stored once",
              len(list(img_dir.glob("*.png"))) == 1,
              [p.name for p in img_dir.glob("*.png")])
        check("vision: reuse keeps both turns attached",
              len(f1) == 1 and len(f2) == 1 and s1 == s2 == ["attached"], (f1, f2))
        check("vision: reused path still exists", os.path.isfile(f2[0]["path"]))
        other = base64.b64encode(png + b"more bytes").decode()
        g.extract_request_images([{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png", "data": other}}]}])
        check("vision: a DIFFERENT screenshot is still stored",
              len(list(img_dir.glob("*.png"))) == 2,
              [p.name for p in img_dir.glob("*.png")])
    finally:
        g.IMG_DIR = real_dir
        shutil.rmtree(img_dir, ignore_errors=True)
    # --- 7. a tool call the client declared must reach the client ----------
    # The real bug behind "I am writing it" x3: the bridge answered with a tool
    # name or argument shape the desktop had not declared, silently dropped it,
    # and the model saw an empty turn, so it promised the same file again.
    TOOLS = {
        "mcp__tools__TaskList": {"name": "mcp__tools__TaskList",
                                 "description": "list tasks",
                                 "input_schema": {"type": "object",
                                                  "properties": {}, "required": []}},
        "mcp__tools__Write": {"name": "mcp__tools__Write", "description": "write",
                              "input_schema": {"type": "object",
                                               "properties": {
                                                   "path": {"type": "string"},
                                                   "content": {"type": "string"}},
                                               "required": ["path", "content"]}},
    }
    NAMES = set(TOOLS)

    def call_of(name, inp):
        return json.dumps({"content": [{"type": "tool_use", "name": name,
                                        "id": "tu_1", "input": inp}]})

    def kept(decision):
        data = g._extract_decision_json(decision)
        if data is None:
            return None
        items = g._validate_decision_items(data, NAMES, TOOLS)
        return [i["name"] for i in items] if items else []

    check("tools: zero-argument call is forwarded",
          kept(call_of("mcp__tools__TaskList", {})) == ["mcp__tools__TaskList"],
          kept(call_of("mcp__tools__TaskList", {})))
    check("tools: complete call is forwarded",
          kept(call_of("mcp__tools__Write", {"path": "a.txt", "content": "x"}))
          == ["mcp__tools__Write"])
    check("tools: half-filled call is forwarded, not deleted",
          kept(call_of("mcp__tools__Write", {"path": "a.txt"}))
          == ["mcp__tools__Write"],
          "deleting it erases the model's intent and restarts the loop")
    check("tools: name missing the client's namespace is fixed",
          kept(call_of("TaskList", {})) == ["mcp__tools__TaskList"],
          kept(call_of("TaskList", {})))
    check("tools: lowercased name is fixed",
          kept(call_of("mcp__tools__tasklist", {})) == ["mcp__tools__TaskList"],
          kept(call_of("mcp__tools__tasklist", {})))
    check("tools: a tool the client never declared is dropped",
          kept(call_of("mcp__tools__DeleteEverything", {})) == [])
    check("tools: input:null is still refused",
          kept('{"content":[{"type":"tool_use","name":"mcp__tools__Write",'
               '"input":null}]}') == [])

    # --- 8. a cut-off reply admits it was cut off --------------------------
    # "end_turn" on a half sentence costs the user the Continue button.
    check("stop: a finished answer is end_turn",
          g._stop_reason(False, False) == "end_turn")
    check("stop: a cut-off answer says max_tokens",
          g._stop_reason(False, True) == "max_tokens")
    check("stop: a tool call still wins", g._stop_reason(True, True) == "tool_use")
    check("stop: upstream finish=length is truncation",
          g._is_truncated({"_finish": "length"}))
    check("stop: finish=stop is not truncation",
          not g._is_truncated({"_finish": "stop"}))
    check("stop: no finish info tolerated",
          not g._is_truncated({}) and not g._is_truncated(None))

    cut = ('{"content":[{"type":"tool_use","name":"mcp__tools__Write","id":"tu_1",'
           '"input":{"path":"a.txt","content":"the first line')
    meta: dict = {}
    real_cwf = g.chat_with_fallback
    try:
        g.chat_with_fallback = lambda *a, **k: (
            cut, "fake-model", {"input_tokens": 5, "output_tokens": 900})
        _raw, _used, _usage, items = g._decision_worker(
            "opus", "prompt", None, None, NAMES, None,
            time.time() + 30, None, TOOLS, None, "", meta)
    finally:
        g.chat_with_fallback = real_cwf
    check("worker: the cut-off call is salvaged and kept",
          [i["name"] for i in items if i["type"] == "tool_use"]
          == ["mcp__tools__Write"], items)
    check("worker: the turn is marked truncated",
          meta.get("truncated") is True, meta)

    # --- 9. the tools the client really declares get recorded --------------
    cat_dir = Path(tempfile.mkdtemp(prefix="gw cat "))
    cat_path = cat_dir / "tool_catalog.json"
    os.environ["GATEWAY_TOOL_CATALOG_PATH"] = str(cat_path)
    g.record_tool_catalog([{"name": "mcp__tools__TaskList", "description": "list",
                            "input_schema": {"type": "object",
                                             "properties": {"x": {"type": "string"}},
                                             "required": ["x"]}}])
    cat = (json.loads(cat_path.read_text(encoding="utf-8"))
           if cat_path.exists() else {})
    check("catalog: the declared tool is recorded",
          "mcp__tools__TaskList" in cat.get("tools", {}), str(cat_path))
    check("catalog: required keys recorded",
          cat.get("tools", {}).get("mcp__tools__TaskList", {}).get("required")
          == ["x"], cat)
    g.record_tool_catalog([{"name": "mcp__tools__Write", "description": "w",
                            "input_schema": {"type": "object",
                                             "properties": {"path": {"type": "string"}},
                                             "required": ["path"]}},
                           {"name": "no_schema"}])
    cat2 = (json.loads(cat_path.read_text(encoding="utf-8"))
            if cat_path.exists() else {"tools": {}})
    check("catalog: another client's tools merge in",
          set(cat2["tools"]) == {"mcp__tools__TaskList", "mcp__tools__Write",
                                 "no_schema"}, sorted(cat2["tools"]))
    check("catalog: stays a small schema file, not a transcript",
          len(json.dumps(cat2)) < 4000 and "messages" not in cat2)
    shutil.rmtree(cat_dir, ignore_errors=True)



finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print()
print("FAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)

