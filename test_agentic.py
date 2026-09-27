"""Both fixes: empty tool input + URL guard. Run: python test_agentic.py"""
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
    {"name": "Read", "description": "read",
     "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}},
                      "required": ["file_path"]}},
    {"name": "WebFetch", "description": "fetch",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}},
                      "required": ["url"]}},
]
BY = {t["name"]: t for t in TOOLS}
NAMES = set(BY)

# ---------- FIX 1: empty / incomplete tool input ----------------------------
empty = {"content": [{"type": "tool_use", "name": "Read", "input": {}}]}
check("fix1: Read with {} is dropped",
      g._validate_decision_items(empty, NAMES, BY) is None,
      g._validate_decision_items(empty, NAMES, BY))

missing_req = {"content": [{"type": "tool_use", "name": "Read",
                            "input": {"wrong_key": "x"}}]}
# Policy, chosen for Claude Desktop fidelity: a merely INCOMPLETE call is
# forwarded instead of swallowed. The client then returns an error tool_result
# naming the missing field, which is what a native backend would do and what
# lets the model fix itself; dropping the call left the assistant turn with no
# tool_result at all, so the model just repeated itself.
kept_missing = g._validate_decision_items(missing_req, NAMES, BY)
check("fix1: Read missing required key is forwarded, not dropped",
      kept_missing is not None and kept_missing[0]["name"] == "Read", kept_missing)

blank_val = {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": ""}}]}
kept_blank = g._validate_decision_items(blank_val, NAMES, BY)
check("fix1: Read with empty string value is forwarded, not dropped",
      kept_blank is not None and kept_blank[0]["input"] == {"file_path": ""},
      kept_blank)

# ---------- FIX 2: URL detection + refusal detection ----------------------
check("fix2: URL extracted from text",
      g.find_urls("go to https://github.com/obra/superpowers now") ==
      ["https://github.com/obra/superpowers"])
dupes = g.find_urls("http://a.com/x and http://a.com/x and https://b.com")
check("fix2: multiple URLs deduped+ordered", dupes == ["http://a.com/x", "https://b.com"], dupes)
check("fix2: no URL -> empty", g.find_urls("no links here") == [])
for t in ["I cannot access this site", "دسترسی به این سایت بسته است",
          "network egress blocked", "I'm not able to reach it"]:
    check(f"fix2: refusal detected: {t[:30]}", g.looks_like_refusal(t))
for t in ["the repo has 292k stars", "می‌خواهم پوشه را بررسی کنم", "done"]:
    check(f"fix2: NOT a false refusal: {t[:30]}", not g.looks_like_refusal(t))

check("fix2: web tool detected", g.has_web_tool(BY))
check("fix2: no web tool when only Read", not g.has_web_tool({"Read": TOOLS[0]}))

guard = g.build_url_guard_prompt(["https://x.com"], ["WebFetch", "Bash"])
check("fix2: guard prompt contains the URL", "https://x.com" in guard)
check("fix2: guard prompt forbids prose", "FORBIDDEN" in guard)
check("fix2: guard prompt names a tool", "WebFetch" in guard)
check("fix2: guard enabled by default", g.URL_GUARD is True)

# ---------- FIX 2 end-to-end: refusal triggers a second attempt -----------
calls = {"n": 0}


def refusing_then_acting(client, alias, system, prompt, image_paths=None,
                         deadline=None, work_dir=None):
    calls["n"] += 1
    if calls["n"] == 1:
        return ('{"content": [{"type": "text", "text": '
                '"I cannot access github.com, network is blocked"}]}',
                "m1", {"input_tokens": 1, "output_tokens": 1})
    return ('{"content": [{"type": "tool_use", "name": "WebFetch", '
            '"input": {"url": "https://github.com/obra/superpowers"}}]}',
            "m2", {"input_tokens": 1, "output_tokens": 1})


g.chat_with_fallback = refusing_then_acting
raw, model, usage, items = g._decision_worker(
    "claude-opus-4-5", "PROMPT", None, None, NAMES, None, 9e9, None, BY,
    ["https://github.com/obra/superpowers"])
check("fix2: guard made a second attempt", calls["n"] == 2, calls["n"])
check("fix2: tool call from the retry is returned",
      items and items[0]["type"] == "tool_use" and items[0]["name"] == "WebFetch", items)

calls["n"] = 0


def polite(client, alias, system, prompt, image_paths=None,
           deadline=None, work_dir=None):
    calls["n"] += 1
    return ('{"content": [{"type": "text", "text": "The repo is about skills"}]}',
            "m1", {"input_tokens": 1, "output_tokens": 1})


g.chat_with_fallback = polite
raw, model, usage, items = g._decision_worker(
    "claude-opus-4-5", "PROMPT", None, None, NAMES, None, 9e9, None, BY,
    ["https://github.com/obra/superpowers"])
check("fix2: no second attempt for a normal answer", calls["n"] == 1, calls["n"])

calls["n"] = 0
g._decision_worker("claude-opus-4-5", "P", None, None, NAMES, None, 9e9, None, BY, [])
check("fix2: no URL -> no second attempt", calls["n"] == 1, calls["n"])

# ---------- FIX 3: gateway fetches URLs the client could not ---------------
check("fix3: real fetch works (github)", (lambda t: bool(t) and "superpowers" in t.lower())(
    g.fetch_url_text("https://github.com/obra/superpowers")))
check("fix3: html->text strips tags+scripts",
      g.html_to_text("<p>hello <b>world</b></p><script>x()</script>") == "hello world")
check("fix3: bad scheme returns None", g.fetch_url_text("not-a-url") is None)
check("fix3: 404 returns None",
      g.fetch_url_text("https://github.com/obra/nope-xyz-404") is None)
check("fix3: egress wording detected (long form)",
      g.looks_like_egress_block(
          "Access to this website is blocked by your organization's network egress settings."))
check("fix3: egress wording detected (code)",
      g.looks_like_egress_block("cowork-egress-blocked"))
check("fix3: no false egress positive",
      not g.looks_like_egress_block("the repo has 292k stars"))
check("fix3: fetch enabled by default", g.URL_FETCH is True)

# the egress path must FETCH and hand over content, not re-ask the model
attempts = []


def egress_then_answer(client, alias, system, prompt, image_paths=None,
                       deadline=None, work_dir=None):
    attempts.append(prompt)
    if len(attempts) == 1:
        return ('{"content": [{"type": "text", "text": '
                '"Access to this website is blocked by your organization\'s '
                'network egress settings. (cowork-egress-blocked)"}]}',
                "m1", {"input_tokens": 1, "output_tokens": 1})
    # second attempt must have received the fetched page
    assert "FETCHED" in prompt, "gateway did not inject fetched content"
    return ('{"content": [{"type": "text", "text": "superpowers is a skills framework"}]}',
            "m2", {"input_tokens": 1, "output_tokens": 1})


g.chat_with_fallback = egress_then_answer
raw, model, usage, items = g._decision_worker(
    "claude-opus-4-5", "PROMPT", None, None, NAMES, None, 9e9, None, BY,
    ["https://github.com/obra/superpowers"], "")
check("fix3: egress block triggers a second attempt", len(attempts) == 2, len(attempts))
check("fix3: fetched page was injected into the retry",
      len(attempts) > 1 and "FETCHED https://github.com/obra/superpowers" in attempts[1])
check("fix3: answer replaced the refusal",
      items and "skills framework" in items[0].get("text", ""), items)

print()
print("FAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)



good = {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]}
kept = g._validate_decision_items(good, NAMES, BY)
check("fix1: a valid Read survives", kept and kept[0]["input"] == {"file_path": "a.py"}, kept)

old_style = g._validate_decision_items(empty, NAMES)
check("fix1: no tool_by_name = old behaviour (still kept)", old_style is not None, old_style)

mixed = {"content": [{"type": "text", "text": "hi"},
                     {"type": "tool_use", "name": "Read", "input": {}}]}
res = g._validate_decision_items(mixed, NAMES, BY)
check("fix1: text preserved even when the tool call is dropped",
      res and res[0]["type"] == "text" and len(res) == 1, res)

