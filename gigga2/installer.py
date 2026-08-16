"""opencode integration installer (gigga2 install / uninstall).

Two artifacts:
1. `~/.config/opencode/agents/GIGGA.md` — a red, Tab-switchable primary agent
   (opencode loads markdown agents from this directory; the filename is the
   agent name).
2. `~/.config/opencode/gigga/gigga-flow.tsx` + a `plugin` entry in
   `~/.config/opencode/tui.json` — the sidebar stage-flowchart panel
   (TUI plugins load via tui.json, not opencode.json).
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

OPENCODE_CONFIG = Path.home() / ".config" / "opencode"
AGENT_NAME = "GIGGA.md"
PLUGIN_NAME = "gigga-flow.tsx"


def _asset(name: str) -> str:
    return (resources.files("gigga2") / "assets" / name).read_text(encoding="utf-8")


def _load_jsonc(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    # strip // line comments (naive but adequate for config files) and trailing commas
    text = re.sub(r"(^|\s)//[^\n]*", r"\1", text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def _file_url(p: Path) -> str:
    return "file:///" + str(p).replace("\\", "/")


def install() -> dict:
    done = {"agent": None, "plugin": None, "tui_json": None, "notes": []}

    agents_dir = OPENCODE_CONFIG / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_path = agents_dir / AGENT_NAME
    agent_path.write_text(_asset(AGENT_NAME), encoding="utf-8")
    done["agent"] = str(agent_path)

    plugin_dir = OPENCODE_CONFIG / "gigga"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    plugin_path = plugin_dir / PLUGIN_NAME
    plugin_path.write_text(_asset(PLUGIN_NAME), encoding="utf-8")
    done["plugin"] = str(plugin_path)

    tui_json = OPENCODE_CONFIG / "tui.json"
    cfg = _load_jsonc(tui_json)
    cfg.setdefault("$schema", "https://opencode.ai/tui.json")
    plugins = cfg.get("plugin") or []
    if isinstance(plugins, str):
        plugins = [plugins]
    url = _file_url(plugin_path)
    # drop any previous gigga entry, then add the current one
    plugins = [p for p in plugins if "gigga-flow" not in str(p)]
    plugins.append(url)
    cfg["plugin"] = plugins
    tui_json.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    done["tui_json"] = str(tui_json)

    done["notes"].append("restart opencode; Tab cycles to the red GIGGA agent")
    done["notes"].append("the sidebar flowchart appears once a gigga2 run exists "
                         "(GIGGA starts one, or run `gigga2 start` yourself)")
    return done


def uninstall() -> dict:
    removed = {"agent": False, "plugin": False, "tui_json": None}
    agent_path = OPENCODE_CONFIG / "agents" / AGENT_NAME
    if agent_path.exists():
        agent_path.unlink()
        removed["agent"] = True
    plugin_path = OPENCODE_CONFIG / "gigga" / PLUGIN_NAME
    if plugin_path.exists():
        plugin_path.unlink()
        removed["plugin"] = True
    tui_json = OPENCODE_CONFIG / "tui.json"
    if tui_json.exists():
        cfg = _load_jsonc(tui_json)
        plugins = cfg.get("plugin") or []
        if isinstance(plugins, str):
            plugins = [plugins]
        cfg["plugin"] = [p for p in plugins if "gigga-flow" not in str(p)]
        tui_json.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        removed["tui_json"] = str(tui_json)
    return removed
