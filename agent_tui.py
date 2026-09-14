"""
Blender Agent TUI — Textual-based, split-panel, todo-driven.

Usage:
    python agent_tui.py
"""

import json
import ast
import os
import re
import shutil
import socket
import datetime
import tempfile
import urllib.request
import urllib.error
import argparse
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RichLog, Select, Static
from textual import work
from rich.text import Text

DEBUG = False  # Logs raw LLM responses to debug.log when tool call extraction fails

_DEBUG_LOG = Path(__file__).parent / "debug.log"
_DEBUG_DIR  = Path(__file__).parent / "debug"

def _debug_log(msg: str) -> None:
    try:
        with _DEBUG_LOG.open("a") as f:
            f.write(f"\n--- {msg}\n")
    except Exception:
        pass

# ── Paths & defaults ───────────────────────────────────────────────────────────

CONFIG_PATH     = Path(__file__).parent / "agent_config.json"
SCRATCHPAD_PATH = Path(__file__).parent / "scratchpad.md"

DEFAULT_CONFIG: Dict[str, Any] = {
    "ollama_url":   "http://localhost:11434",
    "ollama_model": "llama3.2",
    "blender_host": "localhost",
    "blender_port": 12345,
}

# ── Blender tools ──────────────────────────────────────────────────────────────

BLENDER_TOOLS = [
    {"type": "function", "function": {
        "name": "get_scene_info",
        "description": (
            "Get a full overview of the current Blender scene: objects, "
            "materials, lights, cameras, and settings. Call this first "
            "before making any changes."
        ),
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_object_info",
        "description": "Get detailed properties of a single named object in the scene.",
        "parameters": {"type": "object", "properties": {
            "object_name": {"type": "string", "description": "Exact name of the Blender object"}
        }, "required": ["object_name"]},
    }},
    {"type": "function", "function": {
        "name": "execute_blender_code",
        "description": (
            "Execute Python (bpy) code inside Blender to create, modify, "
            "delete, or configure anything in the scene. "
            "This is the primary tool for ALL scene edits."
        ),
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "Valid Python code using the bpy module"}
        }, "required": ["code"]},
    }},
    {"type": "function", "function": {
        "name": "set_camera",
        "description": (
            "Move and aim the render camera. Creates the camera if it doesn't exist. "
            "Always call this before get_render_preview to position the shot correctly."
        ),
        "parameters": {"type": "object", "properties": {
            "location":          {"type": "array",  "items": {"type": "number"},
                                  "description": "[x, y, z] world position"},
            "rotation_degrees":  {"type": "array",  "items": {"type": "number"},
                                  "description": "[rx, ry, rz] Euler rotation in degrees"},
            "camera_name":       {"type": "string", "description": "Camera object name (default: Camera)"},
            "focal_length":      {"type": "number", "description": "Lens focal length in mm (optional)"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_render_preview",
        "description": (
            "Render the scene from the active camera"
            "Use this to verify progress — call it after major modelling or shading steps. "
        ),
        "parameters": {"type": "object", "properties": {
            "max_size": {"type": "integer",
                         "description": "Square render resolution in pixels (default: 512)"},
        }},
    }},
]

SYSTEM_PROMPT = """\
You are a Blender 3D assistant. You control a live Blender session through tool calls. 
Always start with getting scene info to understand the current state before making changes to get idea of scales and proportions and position for the objects. 
If a user request is unclear, ask for clarification instead of making assumptions. Always start with the basic shapes to get the proportions right before adding details and modifying mesh.

## Core Rules
- ONLY interact with Blender via the provided tools. Never suggest running shell
  commands, editing files manually, or using anything outside these tools.
- Always check blender file before making changes.
- You MUST call tools using the structured tool-call mechanism. Never write tool
  calls as plain text or code blocks — always invoke them as actual function calls.
- Make sure there is a light source in the scene before rendering. If no lights are present, create a simple one using execute_blender_code.
## Materials & Color
- NEVER use `obj.color` to set object colors — this only affects the solid viewport, not renders.
- Always create a node-based material with Principled BSDF when applying color:
    mat = bpy.data.materials.new(name="MyMat")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (r, g, b, 1.0)
    obj.data.materials.append(mat)
- `Base Color` requires exactly 4 values (RGBA).
- Always set `use_nodes = True` on every material you create.

## Scale
- ALWAYS set scale to (1, 1, 1) for every object you create or modify.
## Location & Dimensions — confirm before acting
- If there is ANY ambiguity about location or dimensions, ask the user before calling tools.
- Confirm objects are not overlapping or intersecting in unintended ways. Make sure meshes are not accidentally created inside each other use BVHTree.overlap to confirm.

## Code execution
- ALL code passed to execute_blender_code MUST be valid Python.
- NEVER use // comments. Python uses # only.
- The code sandbox allows practical Blender scripting. You may define helper functions and use most standard library modules.
- Imports — blocked (will raise an error): os, sys, subprocess, socket, shutil, pathlib, importlib, ctypes, pty, signal, threading, multiprocessing.
- Imports — allowed: bpy, mathutils, bmesh, math, random, json, itertools, functools, collections, and any other module not in the blocked list.
- Do not use dunder attributes (anything starting with __).
- If a task cannot be done within these constraints, explain briefly and ask for permission to simplify the step.

## Camera control & progress verification
- Use set_camera to position the render camera before taking any render preview.
- The camera MUST be placed far enough back to fit the ENTIRE scene in frame — no
  cropping, no partial views. A distance of at least 2–3× the largest scene dimension
  is a safe starting point. Prefer a raised 3/4 angle (e.g. location [5, -8, 5],
  rotation [65, 0, 45]) so depth and all objects are visible.
- If any object appears cut off in a preview, move the camera further back and re-render.
- After every major modelling or shading milestone, call get_render_preview to  visually confirm the scene is progressing as intended. If not edit the TODO list to add corrective steps.
- take multiple shots if needed to find the right distance and get the full view from all angles before confirming the task done.
## Complex tasks — todo list
- When a user request requires more than one distinct Blender operation, you MUST
  output a TODO list as the VERY FIRST thing in your response, before any tool calls
  or other text. Use exactly this format:
    TODO:
    [ ] Step one description
    [ ] Step two description
    [ ] Step three description
- Each step must be a single, concrete Blender action.
- Only output the TODO list on the first response. Do not repeat it.
- Do not call any tools in the same response as the TODO list — wait for the next turn.
- After the TODO list is created, you will be prompted one step at a time. For each step, call the appropriate Blender tool(s) immediately — do not output text, do not repeat or update the TODO list, just call the tool.
"""

# ── TodoManager ────────────────────────────────────────────────────────────────

class TodoManager:
    def __init__(self):
        self.steps: List[Dict] = []
        self._load()

    def _load(self):
        self.steps = []
        if not SCRATCHPAD_PATH.exists():
            return
        for line in SCRATCHPAD_PATH.read_text().splitlines():
            s = line.strip()
            if s.startswith("[x] ") or s.startswith("[X] "):
                self.steps.append({"text": s[4:], "done": True})
            elif s.startswith("[ ] "):
                self.steps.append({"text": s[4:], "done": False})

    def save(self):
        lines = ["# Scratchpad\n"]
        for s in self.steps:
            lines.append(f"{'[x]' if s['done'] else '[ ]'} {s['text']}")
        SCRATCHPAD_PATH.write_text("\n".join(lines) + "\n")

    def clear(self):
        self.steps = []
        if SCRATCHPAD_PATH.exists():
            SCRATCHPAD_PATH.unlink()

    def parse_from_llm(self, text: str) -> bool:
        steps: List[Dict] = []
        in_todo = False
        for line in text.splitlines():
            s = line.strip()
            if "TODO:" in s.upper() and not in_todo:
                in_todo = True
                continue
            if in_todo:
                bare = re.sub(r'^[-*]\s+', '', s)
                bare = re.sub(r'^\d+\.\s+', '', bare)
                if bare.startswith("[ ] "):
                    steps.append({"text": bare[4:], "done": False})
                elif bare.startswith("[x] ") or bare.startswith("[X] "):
                    steps.append({"text": bare[4:], "done": True})
                elif steps and s and not re.match(r'^[-*\d]', s):
                    break
        if steps:
            self.steps = steps
            self.save()
            return True
        return False

    def is_empty(self) -> bool:
        return not self.steps

    def has_pending(self) -> bool:
        return any(not s["done"] for s in self.steps)

    def all_done(self) -> bool:
        return bool(self.steps) and all(s["done"] for s in self.steps)

    def current_step(self) -> Optional[str]:
        for s in self.steps:
            if not s["done"]:
                return s["text"]
        return None

    def current_index(self) -> int:
        for i, s in enumerate(self.steps):
            if not s["done"]:
                return i + 1
        return len(self.steps)

    def mark_current_done(self):
        for s in self.steps:
            if not s["done"]:
                s["done"] = True
                break
        self.save()

# ── BlenderClient ──────────────────────────────────────────────────────────────

class BlenderClient:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None

    def connect(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect((self.host, self.port))
            self._sock = s
            return True
        except Exception:
            self._sock = None
            return False

    def _receive_full(self) -> bytes:
        chunks: List[bytes] = []
        self._sock.settimeout(180)
        while True:
            try:
                chunk = self._sock.recv(8192)
                if not chunk:
                    break
                chunks.append(chunk)
                try:
                    json.loads(b"".join(chunks).decode())
                    break
                except json.JSONDecodeError:
                    continue
            except socket.timeout:
                break
        return b"".join(chunks)

    def send_command(self, command_type: str, params: Dict = None) -> Any:
        if not self._sock:
            if not self.connect():
                raise ConnectionError(
                    f"Cannot connect to Blender at {self.host}:{self.port}. "
                    "Make sure the addon is running."
                )
        try:
            cmd = {"type": command_type, "params": params or {}}
            self._sock.sendall(json.dumps(cmd).encode())
            data = self._receive_full()
            resp = json.loads(data.decode())
            if resp.get("status") == "error":
                raise RuntimeError(resp.get("message", "Blender error"))
            return resp.get("result", {})
        except (ConnectionError, BrokenPipeError, ConnectionResetError, OSError):
            self._sock = None
            raise

    def disconnect(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

# ── LLM client ─────────────────────────────────────────────────────────────────

def _http_post(url: str, payload: Dict, timeout: int = 120) -> Dict:
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError:
        raise
    except urllib.error.URLError as e:
        raise ConnectionError(str(e.reason)) from e

def _normalize_tool_calls(tool_calls: List[Dict]) -> List[Dict]:
    out = []
    for tc in tool_calls:
        fn   = tc.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        out.append({
            "id":       tc.get("id", fn.get("name", "")),
            "function": {"name": fn.get("name", ""), "arguments": args},
        })
    return out

def llm_chat(base_url: str, model: str, messages: List[Dict], tools: List[Dict]) -> Dict:
    payload = {"model": model, "messages": messages, "tools": tools, "stream": False}
    try:
        raw = _http_post(f"{base_url.rstrip('/')}/api/chat", payload)
    except urllib.error.HTTPError as e:
        raise ConnectionError(f"HTTP {e.code}: {e.reason}") from e
    except ConnectionError:
        raise ConnectionError(f"Cannot reach Ollama at {base_url}")
    msg = raw.get("message", {})
    return {"message": {
        "role":       msg.get("role", "assistant"),
        "content":    msg.get("content") or "",
        "tool_calls": _normalize_tool_calls(msg.get("tool_calls") or []),
    }}

_CONTENT_TC_RE = re.compile(
    r'<tool_call>\s*(.*?)\s*</tool_call>'
    r'|\[TOOL_CALL\]\s*(.*?)\s*\[/TOOL_CALL\]',
    re.DOTALL | re.IGNORECASE,
)

_FUNCTIONCALL_RE = re.compile(
    r'<functioncall>\s*(.*?)\s*</functioncall>',
    re.DOTALL | re.IGNORECASE,
)

# Llama 3.x / some Ollama models: <function=tool_name>\n{args}\n</function>
_FUNCTION_TAG_RE = re.compile(
    r'<function=(\w+)>\s*(.*?)\s*</function>',
    re.DOTALL | re.IGNORECASE,
)

_JSON_BLOCK_RE = re.compile(r'```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```', re.DOTALL | re.IGNORECASE)

_PLAIN_TOOL_CALL_RE = re.compile(
    r'^(?:call|use|run|execute)\s+(get_scene_info|get_render_preview)\s*$',
    re.IGNORECASE,
)

# Matches JSON objects with up to one level of brace nesting (covers all tool call shapes)
_EMBEDDED_JSON_RE = re.compile(r'\{(?:[^{}]|\{[^{}]*\})*\}', re.DOTALL)

_KNOWN_TOOL_NAMES = {t["function"]["name"] for t in BLENDER_TOOLS}


def _tool_call_from_data(data: Any) -> Optional[Dict]:
    if not isinstance(data, dict):
        return None

    function = data.get("function") if isinstance(data.get("function"), dict) else {}
    name = data.get("name") or function.get("name") or ""
    args = data.get("arguments")
    if args is None:
        args = data.get("parameters")
    if args is None:
        args = data.get("args")
    if args is None:
        args = function.get("arguments") or {}

    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}

    if not name:
        return None

    return {"id": data.get("id", name), "function": {"name": name, "arguments": args}}

def _append_tool_calls_from_raw(raw: str, tool_calls: List[Dict]) -> None:
    data = json.loads(raw)
    items = data if isinstance(data, list) else [data]
    for item in items:
        tc = _tool_call_from_data(item)
        if tc:
            tool_calls.append(tc)

def _extract_content_tool_calls(content: str):
    """
    Pull tool-call blocks out of content text (used by models that don't
    populate the tool_calls field natively).
    Returns (tool_calls_list, cleaned_content).
    """
    tool_calls: List[Dict] = []
    for m in _CONTENT_TC_RE.finditer(content):
        raw = (m.group(1) or m.group(2) or "").strip()
        try:
            data = json.loads(raw)
            tool_call = _tool_call_from_data(data)
            if tool_call:
                tool_calls.append(tool_call)
        except (json.JSONDecodeError, AttributeError):
            pass

    if not tool_calls:
        try:
            _append_tool_calls_from_raw(content.strip(), tool_calls)
        except (json.JSONDecodeError, TypeError):
            pass

    if not tool_calls:
        block_match = _JSON_BLOCK_RE.search(content)
        if block_match:
            try:
                _append_tool_calls_from_raw(block_match.group(1).strip(), tool_calls)
            except (json.JSONDecodeError, TypeError):
                pass

    if not tool_calls:
        for m in _FUNCTION_TAG_RE.finditer(content):
            name = m.group(1).strip()
            raw  = m.group(2).strip()
            args: Dict = {}
            if raw:
                try:
                    args = json.loads(raw)
                except json.JSONDecodeError:
                    pass
            if name:
                tool_calls.append({"id": name, "function": {"name": name, "arguments": args}})

    if not tool_calls:
        for m in _FUNCTIONCALL_RE.finditer(content):
            raw = m.group(1).strip()
            try:
                data = json.loads(raw)
                tool_call = _tool_call_from_data(data)
                if tool_call:
                    tool_calls.append(tool_call)
            except (json.JSONDecodeError, AttributeError):
                pass

    if not tool_calls:
        for m in _EMBEDDED_JSON_RE.finditer(content):
            try:
                data = json.loads(m.group(0))
                tool_call = _tool_call_from_data(data)
                if tool_call and tool_call["function"]["name"] in _KNOWN_TOOL_NAMES:
                    tool_calls.append(tool_call)
            except (json.JSONDecodeError, AttributeError):
                pass

    if not tool_calls:
        plain = content.strip()
        if plain.lower() in {"get_scene_info", "get_render_preview"}:
            tool_calls.append({"id": plain, "function": {"name": plain, "arguments": {}}})
        else:
            plain_match = _PLAIN_TOOL_CALL_RE.match(plain)
            if plain_match:
                name = plain_match.group(1)
                tool_calls.append({"id": name, "function": {"name": name, "arguments": {}}})

    cleaned = _CONTENT_TC_RE.sub("", content).strip()
    cleaned = _FUNCTION_TAG_RE.sub("", cleaned).strip()
    cleaned = _FUNCTIONCALL_RE.sub("", cleaned).strip()
    if cleaned.startswith("```"):
        cleaned = _JSON_BLOCK_RE.sub("", cleaned).strip()
    return tool_calls, cleaned

def get_ollama_models(base_url: str) -> tuple:
    """Fetch available models from Ollama. Returns (models_list, error_msg)."""
    try:
        req = urllib.request.Request(f"{base_url.rstrip('/')}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
        names = [m.get("name", "") for m in data.get("models", [])]
        return names, None
    except Exception as e:
        return [], str(e)

def ping_ollama(base_url: str, model: str):
    names, err = get_ollama_models(base_url)
    if err:
        return False, err
    found = any(n == model or n.startswith(model.split(":")[0]) for n in names)
    return True, "model found" if found else "model not listed"

# ── Tool execution ─────────────────────────────────────────────────────────────

_BLOCKED_COMPLEX_NODES = (
    ast.ClassDef,
    ast.Lambda,
    ast.NamedExpr,
)

_BLOCKED_IMPORT_ROOTS = {
    "os",
    "sys",
    "subprocess",
    "socket",
    "shutil",
    "pathlib",
    "builtins",
    "importlib",
    "ctypes",
    "pty",
    "signal",
    "threading",
    "multiprocessing",
}

_BLOCKED_CALL_ROOTS = {
    "eval",
    "exec",
    "open",
    "compile",
    "input",
    "__import__",
    "globals",
    "locals",
    "vars",
    "getattr",
    "setattr",
    "delattr",
}

_BLOCKED_NAMES = {
    "__builtins__",
    "eval",
    "exec",
    "open",
    "compile",
    "input",
    "__import__",
}


def _get_root_name(node: ast.AST) -> Optional[str]:
    while True:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            node = node.value
            continue
        if isinstance(node, ast.Subscript):
            node = node.value
            continue
        if isinstance(node, ast.Call):
            node = node.func
            continue
        return None


def _check_code(code: str) -> Optional[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "invalid syntax"

    for node in ast.walk(tree):
        if isinstance(node, _BLOCKED_COMPLEX_NODES):
            return f"complex python not allowed: {type(node).__name__}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _BLOCKED_IMPORT_ROOTS:
                    return f"blocked import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level != 0 or root in _BLOCKED_IMPORT_ROOTS:
                return f"blocked import: from {node.module or ''} import ..."
        elif isinstance(node, ast.Call):
            callee = _get_root_name(node.func)
            if callee in _BLOCKED_CALL_ROOTS:
                return f"dangerous call blocked: {callee}"
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                return f"dunder attribute blocked: {node.attr}"
        elif isinstance(node, ast.Name):
            if node.id in _BLOCKED_NAMES:
                return f"dangerous name blocked: {node.id}"
    return None

def execute_tool(blender: BlenderClient, name: str, args: Dict) -> str:
    try:
        if name == "get_scene_info":
            result = blender.send_command("get_scene_info")
            text   = json.dumps(result, indent=2)
            return text[:3000] + ("\n[truncated]" if len(text) > 3000 else "")
        elif name == "get_object_info":
            result = blender.send_command("get_object_info", {"name": args.get("object_name", "")})
            return json.dumps(result, indent=2)
        elif name == "execute_blender_code":
            code    = args.get("code", "")
            blocked = _check_code(code)
            if blocked:
                return f"[blocked] Disallowed pattern: '{blocked}'"
            result = blender.send_command("execute_code", {"code": code})
            return json.dumps(result, indent=2) if isinstance(result, dict) else str(result)
        elif name == "set_camera":
            params: Dict = {}
            if args.get("location") is not None:
                params["location"] = args["location"]
            if args.get("rotation_degrees") is not None:
                params["rotation_degrees"] = args["rotation_degrees"]
            if args.get("camera_name"):
                params["camera_name"] = args["camera_name"]
            if args.get("focal_length") is not None:
                params["focal_length"] = args["focal_length"]
            result = blender.send_command("set_camera", params)
            return json.dumps(result, indent=2)
        elif name == "get_render_preview":
            max_size  = int(args.get("max_size", 512))
            temp_path = os.path.join(tempfile.gettempdir(), "blender_render_preview.png")
            result = blender.send_command("get_render_preview", {
                "filepath": temp_path,
                "max_size": max_size,
            })
            if DEBUG and isinstance(result, dict) and result.get("success"):
                _DEBUG_DIR.mkdir(exist_ok=True)
                ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                dest = _DEBUG_DIR / f"render_{ts}.png"
                try:
                    shutil.copy2(temp_path, dest)
                    result["filepath"] = str(dest)
                except Exception:
                    pass
            return json.dumps(result, indent=2)
        else:
            return f"[error] Unknown tool: {name}"
    except Exception as e:
        return f"[error] {e}"

# ── Animation ──────────────────────────────────────────────────────────────────

SPIRIT_DIR = Path(__file__).parent / "assets" / "spirit"

SPIRIT_PALETTE = {
    "Y": (230, 180,   0),
    "y": (255, 220,  50),
    "h": (160, 110,   0),
    ".": (250, 185, 165),
    "c": (220, 140, 130),
    "o": ( 50,  128,  50),
    "x": (180,  40,  40),
    "w": (200, 160, 150),
    "-": (100,  55,  35),
    "_": (100,  55,  35),
    "^": (100,  55,  35),
    "B": ( 60, 105, 195),
    "b": ( 40,  70, 150),
    "W": (240, 240, 240),
    "#": (110,  60,  15),
    "Z": (220, 180,   0),
    "H": (140, 145, 155),
    "G": (100, 105, 115),
    "|": (130,  75,  30),
    "s": (190, 120,  90),
    "~": (250, 210, 190),
    "!": (255,  80,  80),
}


def _spirit_line_to_rich(line: str, pad: str = "") -> Text:
    t = Text(pad)
    for ch in line:
        rgb = SPIRIT_PALETTE.get(ch)
        if rgb:
            t.append("\u2588", f"rgb({rgb[0]},{rgb[1]},{rgb[2]})")
        else:
            t.append(" ")
    return t

_FALLBACK_LISTENING = [
    "  __________",
    " [###BOB###]",
    " | (o)  (o)|",
    " |  \\____/ |",
    " |=========|",
    " |[~BELT~~]|",
    "  ||     || ",
    " _||_   _||_",
]


class AnimationState(str, Enum):
    LISTENING = "listening"
    WORKING   = "working"
    FALLING   = "falling"


class SpiritManager:
    REQUIRED_STATES = (
        AnimationState.LISTENING,
        AnimationState.WORKING,
        AnimationState.FALLING,
    )

    def __init__(
        self,
        assets_dir: Path,
        frame_width: int = 20,
        frame_height: int = 20,
        frame_hold: int = 2,
    ):
        self.assets_dir   = assets_dir
        self.frame_width  = frame_width
        self.frame_height = frame_height
        self.frame_hold   = max(1, frame_hold)
        self.frames: Dict[AnimationState, List[List[str]]] = {}
        self._load()

    def _normalize_frame(self, lines: List[str]) -> List[str]:
        normalized: List[str] = []
        for line in lines[:self.frame_height]:
            clean = line.rstrip("\n")
            normalized.append(clean[:self.frame_width].ljust(self.frame_width))
        while len(normalized) < self.frame_height:
            normalized.append(" " * self.frame_width)
        return normalized

    def _load_single_file(self, path: Path) -> Dict["AnimationState", List[List[str]]]:
        result: Dict[AnimationState, List[List[str]]] = {s: [] for s in self.REQUIRED_STATES}
        current_state: Optional[AnimationState] = None
        current_frame: List[str] = []
        for line in path.read_text().splitlines():
            if line.startswith("[") and line.endswith("]"):
                if current_state is not None and current_frame:
                    result[current_state].append(self._normalize_frame(current_frame))
                    current_frame = []
                try:
                    current_state = AnimationState(line[1:-1].lower())
                except ValueError:
                    current_state = None
            elif line == "---":
                if current_state is not None and current_frame:
                    result[current_state].append(self._normalize_frame(current_frame))
                current_frame = []
            elif current_state is not None:
                current_frame.append(line)
        if current_state is not None and current_frame:
            result[current_state].append(self._normalize_frame(current_frame))
        return result

    def _load(self):
        fallback = self._normalize_frame(_FALLBACK_LISTENING)
        single = self.assets_dir / "bob_spirits.txt"
        if single.exists():
            loaded = self._load_single_file(single)
            for state in self.REQUIRED_STATES:
                self.frames[state] = loaded[state] if loaded[state] else [fallback]
            return

        grouped: Dict[AnimationState, List[tuple]] = {
            state: [] for state in self.REQUIRED_STATES
        }
        if self.assets_dir.exists():
            for path in sorted(self.assets_dir.glob("*.txt")):
                match = re.match(
                    r"^(listening|working|falling)(?:_(\d+))?$",
                    path.stem,
                )
                if not match:
                    continue
                state = AnimationState(match.group(1))
                order = int(match.group(2) or "1")
                lines = path.read_text().splitlines()
                grouped[state].append((order, self._normalize_frame(lines)))

        listening_frames = [
            frame
            for _, frame in sorted(grouped[AnimationState.LISTENING], key=lambda item: item[0])
        ]
        if not listening_frames:
            listening_frames = [fallback]
        self.frames[AnimationState.LISTENING] = listening_frames

        for state in self.REQUIRED_STATES:
            if state == AnimationState.LISTENING:
                continue
            frames = [frame for _, frame in sorted(grouped[state], key=lambda item: item[0])]
            self.frames[state] = frames if frames else listening_frames

    def get_frame(self, state: AnimationState, tick: int) -> List[str]:
        frames = self.frames.get(state) or self.frames[AnimationState.LISTENING]
        idx = (tick // self.frame_hold) % len(frames)
        return frames[idx]

# ── Textual widgets ────────────────────────────────────────────────────────────

class SpiritWidget(Static):
    """Animated BOB the Builder ASCII art."""

    DEFAULT_CSS = """
    SpiritWidget {
        width: 18;
        height: 18;
        border: solid yellow;
        content-align: center top;
        padding: 0;
    }
    """

    def __init__(self, spirit_mgr: SpiritManager, **kwargs):
        super().__init__(**kwargs)
        self.spirit_mgr = spirit_mgr
        self._state: AnimationState = AnimationState.LISTENING
        self._tick: int = 0

    def on_mount(self) -> None:
        self._render_frame()
        self.set_interval(0.12, self._tick_frame)

    def _tick_frame(self) -> None:
        self._tick += 1
        self._render_frame()

    def set_state(self, state: AnimationState) -> None:
        if state != self._state:
            self._state = state
            self._tick = 0

    def _sway_offset(self) -> int:
        return 0

    _SPINNER = "⣾⣽⣻⢿⡿⣟⣯⣷"
    _STATE_LABEL = {
        AnimationState.LISTENING: ("",         ""),
        AnimationState.WORKING:   ("WORKING",  "yellow bold"),
        AnimationState.FALLING:   ("ERROR",    "red bold"),
    }

    def _render_frame(self) -> None:
        frame    = self.spirit_mgr.get_frame(self._state, self._tick)
        frame_w  = self.spirit_mgr.frame_width
        widget_w = self.size.width if self.size.width > 0 else frame_w
        center   = max(0, (widget_w - frame_w) // 2)
        offset   = center + self._sway_offset()
        pad      = " " * max(0, offset)

        spin        = self._SPINNER[self._tick % len(self._SPINNER)]
        label, lsty = self._STATE_LABEL[self._state]

        inner_h = (self.size.height - 2) if self.size.height > 2 else self.size.height
        show_spinner = inner_h >= self.spirit_mgr.frame_height + 1

        text = Text()
        for line in frame:
            text.append_text(_spirit_line_to_rich(line, pad))
            text.append("\n")
        if show_spinner:
            if label:
                text.append(pad + spin + " ", "green")
                text.append(label + "\n", lsty)
            else:
                text.append(pad + spin + "\n", "green")
        self.update(text)


class TodoWidget(Static):
    """Checklist panel driven by a TodoManager."""

    DEFAULT_CSS = """
    TodoWidget {
        height: 1fr;
        border: solid cyan;
        padding: 0 1;
    }
    """

    def refresh_todos(self, todo_mgr: TodoManager) -> None:
        text = Text()
        text.append(" TODO \n", "cyan bold")
        text.append("─" * 18 + "\n", "cyan")
        if todo_mgr.is_empty():
            text.append("(empty)", "dim")
        else:
            current_idx = todo_mgr.current_index() - 1
            for i, s in enumerate(todo_mgr.steps):
                prefix = "☑ " if s["done"] else "☐ "
                if s["done"]:
                    style = "green dim"
                elif i == current_idx:
                    style = "yellow bold"
                else:
                    style = "yellow"
                text.append(prefix + s["text"] + "\n", style)
        self.update(text)


class StatusInfoWidget(Static):
    """Side panel showing live session stats next to the spirit box."""

    DEFAULT_CSS = """
    StatusInfoWidget {
        width: 1fr;
        height: 18;
        border: solid yellow;
        padding: 0 1;
    }
    """

    _STATE_STYLE = {
        AnimationState.LISTENING: ("LISTEN",  "green"),
        AnimationState.WORKING:   ("WORKING", "yellow"),
        AnimationState.FALLING:   ("ERROR",   "red bold"),
    }

    def refresh_info(
        self,
        state: AnimationState,
        msg_count: int,
        model: str,
        blender_ok: bool,
        todo_idx: int = 0,
        todo_total: int = 0,
    ) -> None:
        label, style = self._STATE_STYLE[state]
        text = Text()
        text.append(" INFO\n", "yellow bold")
        text.append("─" * 10 + "\n", "yellow dim")

        text.append("status\n", "dim")
        text.append(f" {label}\n", style)

        text.append("blender\n", "dim")
        if blender_ok:
            text.append(" ok\n", "green")
        else:
            text.append(" off\n", "red")

        text.append("ctx\n", "dim")
        text.append(f" {msg_count}\n", "white")

        text.append("model\n", "dim")
        short = model.split(":")[0]
        text.append(f" {short}\n", "white")

        if todo_total > 0:
            text.append("step\n", "dim")
            text.append(f" {todo_idx}/{todo_total}\n", "cyan")

        self.update(text)


_TOOLS_RENDERABLE = Text.from_markup(
    "[magenta bold] TOOLS [/magenta bold]\n"
    "[magenta]──────────────────[/magenta]\n"
    "[magenta dim]\\[?] get_scene_info[/magenta dim]\n"
    "[magenta dim]\\[i] get_object_info[/magenta dim]\n"
    "[magenta dim]\\[>] execute_blender_code[/magenta dim]\n"
    "[magenta dim]\\[C] set_camera[/magenta dim]\n"
    "[magenta dim]\\[R] get_render_preview[/magenta dim]"
)

# ── Config screen ──────────────────────────────────────────────────────────────

class ConfigScreen(ModalScreen):
    """Modal config editor — opened via /config."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ConfigScreen {
        align: center middle;
    }
    #config-dialog {
        width: 64;
        height: auto;
        border: solid cyan;
        background: $surface;
        padding: 1 2;
    }
    #config-title {
        color: cyan;
        text-style: bold;
        margin-bottom: 1;
    }
    .config-label {
        color: $text-muted;
        margin-top: 1;
    }
    #config-help {
        color: $text-muted;
        margin-top: 1;
    }
    #save-btn {
        margin-top: 1;
        width: 100%;
    }
    """

    def __init__(self, cfg: Dict, **kwargs):
        super().__init__(**kwargs)
        self._cfg = cfg.copy()
        self._models, _ = get_ollama_models(cfg["ollama_url"])

    def compose(self) -> ComposeResult:
        current_model = str(self._cfg["ollama_model"])
        with Vertical(id="config-dialog"):
            yield Label(" CONFIG ", id="config-title")
            yield Label("Ollama URL", classes="config-label")
            yield Input(value=str(self._cfg["ollama_url"]),   id="f-ollama_url")
            yield Label("Ollama Model", classes="config-label")
            if self._models:
                opts = [(m, m) for m in self._models]
                if current_model not in self._models:
                    opts = [(current_model, current_model)] + opts
                yield Select(opts, value=current_model, id="f-ollama_model", allow_blank=False)
            else:
                yield Input(value=current_model, id="f-ollama_model")
            yield Label("Blender Host", classes="config-label")
            yield Input(value=str(self._cfg["blender_host"]), id="f-blender_host")
            yield Label("Blender Port", classes="config-label")
            yield Input(value=str(self._cfg["blender_port"]), id="f-blender_port")
            yield Button("Save & Close", variant="primary", id="save-btn")
            yield Label("Enter saves field  •  Esc cancels", id="config-help")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            self._save_and_dismiss()

    def _save_and_dismiss(self) -> None:
        self._cfg["ollama_url"]   = self.query_one("#f-ollama_url", Input).value
        if self._models:
            v = self.query_one("#f-ollama_model", Select).value
            if v and v is not Select.BLANK:
                self._cfg["ollama_model"] = str(v)
        else:
            self._cfg["ollama_model"] = self.query_one("#f-ollama_model", Input).value
        self._cfg["blender_host"] = self.query_one("#f-blender_host", Input).value
        try:
            self._cfg["blender_port"] = int(self.query_one("#f-blender_port", Input).value)
        except ValueError:
            pass
        save_config(self._cfg)
        self.dismiss(self._cfg)

    def action_cancel(self) -> None:
        self.dismiss(None)

# ── Main app ───────────────────────────────────────────────────────────────────

class BOBApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    #main-area {
        layout: horizontal;
        height: 1fr;
    }
    #left-panel {
        width: 65%;
        height: 100%;
        border-right: solid $panel;
    }
    #chat-log {
        height: 1fr;
    }
    #status-bar {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    #right-panel {
        width: 35%;
        height: 100%;
        layout: vertical;
    }
    #spirit-row {
        height: 18;
        layout: horizontal;
    }
    #tools {
        height: 9;
        border: solid magenta;
        padding: 0 1;
    }
    #input-bar {
        height: 3;
        layout: horizontal;
        align: left middle;
        border-top: solid $panel;
        padding: 0 1;
    }
    #input-label {
        width: 7;
        content-align: left middle;
        color: yellow;
        text-style: bold;
    }
    #input-box {
        width: 1fr;
        border: none;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("escape", "interrupt", "Interrupt", priority=True),
    ]

    def __init__(self, cfg: Dict, **kwargs):
        super().__init__(**kwargs)
        self.cfg      = cfg
        self.messages: List[Dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.todo_mgr = TodoManager()
        self.todo_mgr.clear()
        self.blender: Optional[BlenderClient] = None
        self._spirit_mgr = SpiritManager(SPIRIT_DIR, frame_width=16, frame_height=16, frame_hold=4)
        self._agent_busy = False
        self._interrupt_requested = False
        self._tools_ran_in_step = False
        self._blender_ok = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-area"):
            with Vertical(id="left-panel"):
                yield RichLog(id="chat-log", auto_scroll=True, highlight=False, markup=False, wrap=True)
                yield Label("", id="status-bar")
            with Vertical(id="right-panel"):
                with Horizontal(id="spirit-row"):
                    yield SpiritWidget(self._spirit_mgr, id="spirit")
                    yield StatusInfoWidget(id="status-info")
                yield TodoWidget(id="todo")
                yield Static(_TOOLS_RENDERABLE, id="tools")
        with Horizontal(id="input-bar"):
            yield Label("You > ", id="input-label")
            yield Input(id="input-box", placeholder="Type a message or /command…")

    def on_mount(self) -> None:
        self._connect_blender()
        self._log(f"LLM: {self.cfg['ollama_url']}  model={self.cfg['ollama_model']}", "dim")
        self._log("Commands: /config  /clear  /undo  /redo  /exit  •  Esc to interrupt agent", "dim")
        self._log()

        self.query_one(TodoWidget).refresh_todos(self.todo_mgr)
        self._refresh_status_info()
        self.query_one("#input-box", Input).focus()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _log(self, text: str = "", style: str = "") -> None:
        chat = self.query_one("#chat-log", RichLog)
        chat.write(Text(text, style) if style else text)

    def _set_status(self, text: str, style: str = "") -> None:
        bar = self.query_one("#status-bar", Label)
        bar.update(Text(text, style) if style else text)

    def _refresh_todos(self) -> None:
        self.query_one(TodoWidget).refresh_todos(self.todo_mgr)
        self._refresh_status_info()

    def _refresh_status_info(self) -> None:
        spirit = self.query_one(SpiritWidget)
        ctx = max(0, len(self.messages) - 1)  # exclude system prompt
        self.query_one(StatusInfoWidget).refresh_info(
            state=spirit._state,
            msg_count=ctx,
            model=self.cfg["ollama_model"],
            blender_ok=self._blender_ok,
            todo_idx=self.todo_mgr.current_index(),
            todo_total=len(self.todo_mgr.steps),
        )

    def _set_spirit_state(self, state: AnimationState) -> None:
        self.query_one(SpiritWidget).set_state(state)
        self._refresh_status_info()

    def _blender_op(self, op: str) -> None:
        result = execute_tool(self.blender, "execute_blender_code", {"code": f"bpy.ops.ed.{op}()\nprint('{op} ok')"})
        ok = "[error]" not in result
        self._log(f"{op} {'ok' if ok else 'failed'}", "green" if ok else "red")

    def _connect_blender(self) -> None:
        self.blender = BlenderClient(self.cfg["blender_host"], self.cfg["blender_port"])
        self._blender_ok = self.blender.connect()
        self._log(
            f"Blender {'connected' if self._blender_ok else 'NOT reachable'}  ({self.cfg['blender_host']}:{self.cfg['blender_port']})",
            "green" if self._blender_ok else "red",
        )

    # ── Input handling ────────────────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._agent_busy:
            return
        user_input = event.value.strip()
        event.input.clear()
        if not user_input:
            return

        low = user_input.lower()

        if low in ("/exit", "/quit", "/q"):
            self.exit()
            return

        if low == "/config":
            self.push_screen(ConfigScreen(self.cfg), self._on_config_done)
            return

        if low == "/clear":
            self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            self.todo_mgr.clear()
            self.query_one("#chat-log", RichLog).clear()
            self._log("(conversation cleared)", "dim")
            self._refresh_todos()
            return

        if low == "/undo":
            self._blender_op("undo")
            return

        if low == "/redo":
            self._blender_op("redo")
            return

        if low.startswith("/"):
            self._log(
                f"Unknown command: {user_input}  (try /config /clear /undo /redo /exit)",
                "red",
            )
            return

        # Normal message → kick off agent worker
        self._agent_busy = True
        self.query_one("#input-box", Input).disabled = True
        self._run_agent(user_input)

    def _on_config_done(self, new_cfg: Optional[Dict]) -> None:
        if new_cfg is None:
            return
        self.cfg = new_cfg
        if self.blender:
            self.blender.disconnect()
        self._connect_blender()
        self._refresh_status_info()

    def action_interrupt(self) -> None:
        if self._agent_busy:
            self._interrupt_requested = True
            self._set_status("Interrupting…", "yellow")

    # ── Agent worker ──────────────────────────────────────────────────────────

    @work(thread=True, exclusive=True)
    def _run_agent(self, user_input: str) -> None:
        """
        Worker: run one user message through the full agentic loop, then
        auto-drive any TODO steps that were created.
        """
        self._interrupt_requested = False
        self.call_from_thread(self._log, f"You > {user_input}", "yellow bold")
        self.messages.append({"role": "user", "content": user_input})

        # First agentic pass
        while not self._interrupt_requested and self._one_llm_round():
            pass

        if self._interrupt_requested:
            self.call_from_thread(self._log, "[interrupted]", "yellow")
            self.call_from_thread(self._finish_agent)
            return

        # If TODOs were created, reset conversation and drive each step
        if self.todo_mgr.has_pending():
            self.call_from_thread(
                self._log,
                f"TODO saved ({len(self.todo_mgr.steps)} steps) — starting…",
                "green",
            )
            self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            self.call_from_thread(self._refresh_todos)

        while self.todo_mgr.has_pending() and not self._interrupt_requested:
            current = self.todo_mgr.current_step()
            idx     = self.todo_mgr.current_index()
            total   = len(self.todo_mgr.steps)

            self.call_from_thread(self._log, f"[TODO {idx}/{total}] {current}", "yellow bold")
            self.call_from_thread(self._refresh_todos)

            self._tools_ran_in_step = False
            self.messages.append({
                "role":    "user",
                "content": (
                    f"Complete step {idx}/{total}: {current}\n"
                    "Do NOT output a TODO list. Call the appropriate Blender tool(s) directly now."
                ),
            })

            step_done = False
            while not self._interrupt_requested and self._one_llm_round():
                if self._tools_ran_in_step and not step_done:
                    self.todo_mgr.mark_current_done()
                    self.call_from_thread(self._refresh_todos)
                    step_done = True

            if self._interrupt_requested:
                break

            if not step_done:
                self.call_from_thread(
                    self._log,
                    f"[warn] Step {idx} produced no tool calls — retrying once",
                    "yellow",
                )
                self.messages.append({
                    "role":    "user",
                    "content": (
                        f"You did not call any tools. Please call the Blender tool for step {idx}/{total}: {current}"
                    ),
                })
                self._tools_ran_in_step = False
                while not self._interrupt_requested and self._one_llm_round():
                    if self._tools_ran_in_step and not step_done:
                        self.todo_mgr.mark_current_done()
                        self.call_from_thread(self._refresh_todos)
                        step_done = True
                if not step_done:
                    self.todo_mgr.mark_current_done()
                    self.call_from_thread(self._refresh_todos)

        if self._interrupt_requested:
            self.call_from_thread(self._log, "[interrupted]", "yellow")
        elif self.todo_mgr.all_done():
            self.call_from_thread(self._log, "All TODO steps complete!", "green bold")
            self.todo_mgr.clear()
            self.call_from_thread(self._refresh_todos)

        # Re-enable input
        self.call_from_thread(self._finish_agent)

    def _finish_agent(self) -> None:
        self._agent_busy = False
        inp = self.query_one("#input-box", Input)
        inp.disabled = False
        inp.focus()

    def _one_llm_round(self) -> bool:
        """
        One round of LLM inference + tool execution.
        Runs inside the worker thread.
        Returns True if tools were called (caller should loop again).
        """
        self.call_from_thread(self._set_spirit_state, AnimationState.WORKING)
        self.call_from_thread(self._set_status, "Thinking…", "dim")

        try:
            resp = llm_chat(
                self.cfg["ollama_url"],
                self.cfg["ollama_model"],
                self.messages,
                BLENDER_TOOLS,
            )
        except ConnectionError as e:
            self.call_from_thread(self._set_status, "")
            self.call_from_thread(self._log, f"[error] {e}", "red")
            self.call_from_thread(self._set_spirit_state, AnimationState.FALLING)
            return False

        if self._interrupt_requested:
            self.call_from_thread(self._set_status, "")
            self.call_from_thread(self._set_spirit_state, AnimationState.LISTENING)
            return False

        self.call_from_thread(self._set_status, "")

        msg        = resp["message"]
        tool_calls = msg.get("tool_calls") or []
        content    = msg.get("content") or ""

        # Fallback: some models embed tool calls in content instead of tool_calls
        if not tool_calls and content:
            tool_calls, content = _extract_content_tool_calls(content)

        if DEBUG:
            if not tool_calls and content:
                _debug_log(f"EXTRACTION MISS\n{json.dumps(msg, indent=2)}")
            elif tool_calls:
                _debug_log(f"EXTRACTED tool_calls={json.dumps(tool_calls)}")

        if content:
            self.call_from_thread(self._log, f"BOB > {content}", "cyan")
            if self.todo_mgr.is_empty():
                if self.todo_mgr.parse_from_llm(content):
                    self.call_from_thread(self._refresh_todos)
                    self.messages.append({"role": "assistant", "content": content})
                    self.call_from_thread(self._set_spirit_state, AnimationState.LISTENING)
                    return False

        if not tool_calls:
            if content:
                self.messages.append({"role": "assistant", "content": content})
            self.call_from_thread(self._set_spirit_state, AnimationState.LISTENING)
            return False

        self.messages.append({
            "role":       "assistant",
            "content":    content,
            "tool_calls": tool_calls,
        })

        had_error = False
        self._tools_ran_in_step = True
        for tc in tool_calls:
            fn   = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments") or {}
            tcid = tc.get("id", name)

            self.call_from_thread(self._log, f"  → {name} …", "magenta")

            result = execute_tool(self.blender, name, args)
            failed = "[error]" in result or "[blocked]" in result
            had_error = had_error or failed
            if DEBUG and failed:
                _debug_log(f"TOOL FAILED name={name} args={json.dumps(args)}\nresult={result}")

            # Update the "→ name …" line suffix in-place via a helper
            suffix = " failed" if failed else " done"
            style  = "red" if failed else "green"
            self.call_from_thread(self._patch_last_log, suffix, style)

            self.messages.append({"role": "tool", "content": result, "tool_call_id": tcid})

        if had_error:
            self.call_from_thread(self._set_spirit_state, AnimationState.FALLING)
        else:
            self.call_from_thread(self._set_spirit_state, AnimationState.LISTENING)

        return True

    def _patch_last_log(self, new_suffix: str, style: str) -> None:
        """Append a tool result line to the chat log."""
        self.query_one("#chat-log", RichLog).write(Text("    " + new_suffix.strip(), style))

# ── Config I/O ─────────────────────────────────────────────────────────────────

def load_config() -> Dict:
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    return DEFAULT_CONFIG.copy()

def save_config(cfg: Dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

def prompt_config(cfg: Dict) -> Dict:
    """Interactive config prompt — plain terminal mode (used for --config flag)."""
    def ask(label: str, current: Any) -> str:
        val = input(f"  {label} [{current}]: ").strip()
        return val if val else str(current)

    print("\n--- Ollama ---")
    cfg["ollama_url"]   = ask("URL",   cfg["ollama_url"])
    cfg["ollama_model"] = ask("model", cfg["ollama_model"])
    print("--- Blender ---")
    cfg["blender_host"] = ask("host",  cfg["blender_host"])
    port_str = ask("port", cfg["blender_port"])
    try:
        cfg["blender_port"] = int(port_str)
    except ValueError:
        print(f"  Invalid port, keeping {cfg['blender_port']}")
    save_config(cfg)
    print(f"Saved to {CONFIG_PATH}\n")
    return cfg

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Blender Agent TUI")
    parser.add_argument("--config", action="store_true", help="Configure and exit")
    args = parser.parse_args()

    cfg = load_config()

    if args.config or not CONFIG_PATH.exists():
        if not CONFIG_PATH.exists():
            print("No config found — let's create one.")
        cfg = prompt_config(cfg)
        if args.config:
            return

    BOBApp(cfg).run()


if __name__ == "__main__":
    main()
