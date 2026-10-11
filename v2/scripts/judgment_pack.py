"""Maintain the private judgment pack (scout/judgment.py).

  freeze   read every registered block from the modules AS THE MODEL RECEIVES IT, build the pack
           (templates + sha256 of each final value) and write it to the local mirror of the private
           store (judgment/pack.json and rc/judgment/pack.json). Run while the modules still hold
           the text; afterwards the pack is the source and `set` edits it.
  rewrite  replace each registered block's literal in the module source with judgment.get(...)
  verify   golden check: every block, loaded through the modules, hashes to the frozen sha256
  show N   print one block;  names  list the registry
  set N F  replace block N with the text of file F in BOTH pack copies, re-hash it and re-version
           the pack (a changed version opens a new eval period for the roles that receive the block)

The registry below names blocks only; it contains no instruction text.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, ".")
MIRROR = os.path.expanduser(os.environ.get("SCOUT_STORE_MIRROR", "~/code/scout-user-data"))
L, R = "⟦", "⟧"

# module -> block targets. "NAME" = a module-level string; "OBJ.field" = a keyword of a call
# assigned to OBJ (the AgentDefinition prompts).
REGISTRY = {
    "prompts": ["SOURCE_HIERARCHY", "WRITING_STYLE", "FORMATTING_RULES"],
    "generate": ["SUBJECT_KEY_GUIDE", "CLAIM_CONTRACT", "ROUTING_RULES", "RETRY_CONTRACT", "RESEARCHER.prompt", "VERIFIER.prompt"],
    "monitor": ["MATERIAL_CATEGORIES", "_TRIAGE_SYSTEM", "_MATERIALITY_SYSTEM", "_MY_FACTS_SYSTEM"],
    "propagate": ["_AUTHOR_SYSTEM", "_JUDGE_SYSTEM", "_ELECTION_SYSTEM", "_REWRITE_ADDENDUM"],
    "route": ["_ROUTE_SYSTEM"],
    "reformat": ["_REFORMAT_SYSTEM", "_CONDENSE_VERIFY_SYSTEM"],
    "ask": ["ANSWER_CONTRACT", "QUICK_CONTRACT", "VERIFY_SYSTEM", "REWRITE_SYSTEM"],
    "challenger": ["_CHALLENGER_SYSTEM", "_CHALLENGER_SYSTEM_NEUTRAL"],
    "arbiter": ["_SYSTEM"],
    "strategy": ["STRATEGIC_SYSTEM"],
    "sources_tool": ["PROMPT_NOTE", "TRIAGE_NOTE"],
}
FILE_BLOCKS = {"prompts.METHODOLOGY": "methodology.md"}          # whole files that become blocks
# prompts built inside a function per run: block name -> (module, function); the function's
# returned f-string becomes the template, its expressions the tokens
FUNC_BLOCKS = {"generate._ORCH_SYSTEM": ("generate", "_orch_system"),
               "generate._USER_PROMPT": ("generate", "_build_user_prompt"),
               "generate._FRAMING_VS": ("generate", "_framing"),
               "generate._FRAMING_SOLO": ("generate", "_framing"),
               "reformat._PERSONA_SYSTEM": ("reformat", "classify_persona"),
               "monitor._SIGNALS_NOTE": ("monitor", "_signals_block")}
V1_FILES = ["../v1/research.py", "../v1/methodology.md"]         # archived whole (v1 is retired)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _value_node(tree: ast.Module, target: str) -> ast.AST:
    name, _, field = target.partition(".")
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
            if not field:
                return node.value
            for kw in node.value.keywords:
                if kw.arg == field:
                    return kw.value
    raise KeyError(target)


def _template(node: ast.AST) -> tuple[str, list[str]]:
    """(template, exprs): literal text kept as is, every non-literal piece becomes a ⟦expr⟧ token."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, []
    if isinstance(node, ast.JoinedStr):
        out, exprs = "", []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out += part.value
            else:
                assert part.conversion == -1 and part.format_spec is None, "conversion/format spec not supported"
                e = ast.unparse(part.value); out += f"{L}{e}{R}"; exprs.append(e)
        return out, exprs
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        lt, le = _template(node.left); rt, re_ = _template(node.right)
        return lt + rt, le + re_
    e = ast.unparse(node)
    return f"{L}{e}{R}", [e]


def _render(t: str, subs: dict) -> str:
    for k, v in subs.items():
        t = t.replace(f"{L}{k}{R}", str(v))
    return t


def _live(mod, target: str) -> str:
    name, _, field = target.partition(".")
    obj = getattr(mod, name)
    return getattr(obj, field) if field else obj


def collect() -> dict:
    blocks, shas, exprs_by = {}, {}, {}
    for mod_name, targets in REGISTRY.items():
        mod = importlib.import_module(f"scout.{mod_name}")
        tree = ast.parse(open(f"scout/{mod_name}.py").read())
        for target in targets:
            value = _live(mod, target)
            assert isinstance(value, str) and value, f"{mod_name}.{target} is not text"
            tmpl, exprs = _template(_value_node(tree, target))
            exprs = list(dict.fromkeys(exprs))
            subs = {e: eval(e, mod.__dict__) for e in exprs}                # noqa: S307
            assert _render(tmpl, subs) == value, f"{mod_name}.{target}: template does not reproduce the live value"
            key = f"{mod_name}.{target}"
            blocks[key], shas[key], exprs_by[key] = tmpl, _sha(value), exprs
    for key, path in FILE_BLOCKS.items():
        text = open(path).read()
        blocks[key], shas[key], exprs_by[key] = text, _sha(text), []
    return {"blocks": blocks, "sha256": shas, "exprs": exprs_by}


def freeze() -> None:
    data = collect()
    v1 = {os.path.basename(p): open(p).read() for p in V1_FILES if os.path.exists(p)}
    body = json.dumps(data["blocks"], sort_keys=True, ensure_ascii=False)
    pack = {"version": _sha(body)[:16], "frozen_at": datetime.now().isoformat(timespec="seconds"),
            "blocks": data["blocks"], "sha256": data["sha256"], "exprs": data["exprs"], "v1": v1}
    text = json.dumps(pack, indent=1, ensure_ascii=False)
    for rel in ("judgment/pack.json", "rc/judgment/pack.json"):
        fp = os.path.join(MIRROR, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        open(fp, "w").write(text)
    chars = sum(len(v) for v in data["blocks"].values())
    print(f"frozen {len(data['blocks'])} blocks ({chars} chars) + {len(v1)} v1 files; version {pack['version']}")
    print(f"written to {MIRROR}/judgment/pack.json and rc/judgment/pack.json (commit and push the private repo)")


def rewrite() -> None:
    pack = json.load(open(os.path.join(MIRROR, "judgment/pack.json")))
    for mod_name, targets in REGISTRY.items():
        path = f"scout/{mod_name}.py"
        src = open(path).read()
        tree = ast.parse(src)
        lines = src.split("\n")
        edits = []
        for target in targets:
            node = _value_node(tree, target)
            key = f"{mod_name}.{target}"
            exprs = pack["exprs"][key]
            call = f'judgment.get("{key}"' + (", {" + ", ".join(f"{e!r}: {e}" for e in exprs) + "}" if exprs else "") + ")"
            edits.append((node.lineno, node.col_offset, node.end_lineno, node.end_col_offset, call))
        for l0, c0, l1, c1, call in sorted(edits, reverse=True):
            head, tail = lines[l0 - 1][:c0], lines[l1 - 1][c1:]
            lines[l0 - 1:l1] = [head + call + tail]
        out = "\n".join(lines)
        if "from scout import judgment" not in out:
            t2 = ast.parse(out)
            last = max((n.end_lineno for n in t2.body if isinstance(n, (ast.Import, ast.ImportFrom))), default=0)
            ls = out.split("\n"); ls.insert(last, "from scout import judgment"); out = "\n".join(ls)
        ast.parse(out)
        open(path, "w").write(out)
        print(f"rewrote {path}: {len(targets)} block(s)")


def _return_node(tree: ast.Module, func: str) -> ast.AST:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func:
            rets = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
            assert len(rets) == 1, f"{func}: expected one return"
            return rets[0].value
    raise KeyError(func)


def addfuncs() -> None:
    """Freeze the function-built prompts into the existing pack and rewrite the functions."""
    fp = os.path.join(MIRROR, "judgment/pack.json")
    pack = json.load(open(fp))
    for key, (mod_name, func) in FUNC_BLOCKS.items():
        path = f"scout/{mod_name}.py"
        src = open(path).read(); tree = ast.parse(src)
        node = _return_node(tree, func)
        tmpl, exprs = _template(node)
        exprs = list(dict.fromkeys(exprs))
        pack["blocks"][key], pack["sha256"][key], pack["exprs"][key] = tmpl, _sha(tmpl), exprs
        call = f'judgment.text("{key}", {{' + ", ".join(f"{e!r}: {e}" for e in exprs) + "})"
        lines = src.split("\n")
        head, tail = lines[node.lineno - 1][:node.col_offset], lines[node.end_lineno - 1][node.end_col_offset:]
        lines[node.lineno - 1:node.end_lineno] = [head + call + tail]
        out = "\n".join(lines); ast.parse(out); open(path, "w").write(out)
        print(f"froze + rewrote {key} ({len(tmpl)} chars, tokens {exprs})")
    body = json.dumps(pack["blocks"], sort_keys=True, ensure_ascii=False)
    pack["version"] = _sha(body)[:16]; pack["frozen_at"] = datetime.now().isoformat(timespec="seconds")
    text = json.dumps(pack, indent=1, ensure_ascii=False)
    for rel in ("judgment/pack.json", "rc/judgment/pack.json"):
        open(os.path.join(MIRROR, rel), "w").write(text)
    print(f"pack version {pack['version']}, {len(pack['blocks'])} blocks")


def set_block(name: str, text: str) -> str:
    """Replace one block's template in BOTH pack copies (judgment/ and rc/judgment/), re-hash it and
    re-version the pack. Only for blocks without template tokens (their hash is the text itself); a
    tokenized block is edited in its module and re-frozen. Returns the new pack version."""
    fp = os.path.join(MIRROR, "judgment/pack.json")
    pack = json.load(open(fp))
    if name not in pack["blocks"]:
        raise SystemExit(f"unknown block {name}")
    if pack.get("exprs", {}).get(name):
        raise SystemExit(f"{name} carries template tokens {pack['exprs'][name]}; edit its module and re-freeze")
    if not text.strip():
        raise SystemExit(f"refusing to set {name} to empty text")
    pack["blocks"][name] = text
    pack["sha256"][name] = _sha(text)
    pack["version"] = _sha(json.dumps(pack["blocks"], sort_keys=True, ensure_ascii=False))[:16]
    pack["frozen_at"] = datetime.now().isoformat(timespec="seconds")
    out = json.dumps(pack, indent=1, ensure_ascii=False)
    for rel in ("judgment/pack.json", "rc/judgment/pack.json"):
        open(os.path.join(MIRROR, rel), "w").write(out)
    return pack["version"]


def verify() -> int:
    from scout import judgment
    pack = judgment._load()
    if pack is None:
        print("no pack available"); return 2
    bad = 0
    for mod_name, targets in REGISTRY.items():
        mod = importlib.import_module(f"scout.{mod_name}")
        for target in targets:
            key = f"{mod_name}.{target}"
            got = _sha(_live(mod, target))
            ok = got == pack["sha256"][key]
            bad += not ok
            if not ok:
                print(f"MISMATCH {key}")
    for key in FILE_BLOCKS:
        ok = _sha(judgment.get(key)) == pack["sha256"][key]; bad += not ok
        if not ok:
            print(f"MISMATCH {key}")
    for key in FUNC_BLOCKS:                     # templates (their rendered values are per run)
        ok = _sha((pack.get("blocks") or {}).get(key, "")) == pack["sha256"].get(key); bad += not ok
        if not ok:
            print(f"MISMATCH {key}")
    n = sum(len(t) for t in REGISTRY.values()) + len(FILE_BLOCKS) + len(FUNC_BLOCKS)
    print(f"verify: {n - bad}/{n} blocks byte-identical to the frozen pack (version {pack.get('version')})")
    return 1 if bad else 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "names"
    if cmd == "freeze":
        freeze()
    elif cmd == "rewrite":
        rewrite()
    elif cmd == "stub":
        names = [f"{m}.{t}" for m, ts in REGISTRY.items() for t in ts] + list(FILE_BLOCKS) + list(FUNC_BLOCKS)
        stub = {"version": "stub", "blocks": {n: f"[stub judgment block {n}: the real text is private]" for n in names}}
        os.makedirs("tests/fixtures", exist_ok=True)
        json.dump(stub, open("tests/fixtures/judgment_stub.json", "w"), indent=1)
        print(f"wrote tests/fixtures/judgment_stub.json ({len(names)} names, no instruction text)")
    elif cmd == "addfuncs":
        addfuncs()
    elif cmd == "verify":
        raise SystemExit(verify())
    elif cmd == "set" and len(sys.argv) > 3:
        ver = set_block(sys.argv[2], open(sys.argv[3]).read())
        print(f"set {sys.argv[2]}; pack version {ver} (both copies; commit and push the private repo)")
    elif cmd == "show" and len(sys.argv) > 2:
        from scout import judgment
        print(judgment.get(sys.argv[2]))
    else:
        for m, ts in REGISTRY.items():
            for t in ts:
                print(f"{m}.{t}")
        for k in FILE_BLOCKS:
            print(k)
