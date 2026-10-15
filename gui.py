import base64
import datetime
import io
import json
from pathlib import Path
import re
import ssl
import sys
import textwrap
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
import zlib

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from analyzer import analyze_code, compress_code, generate_dataflow_diagram
from providers import call_continue, call_llm, get_last_call_info
from quiz import evaluate_answer, generate_question_from_snippet

BASE_DIR = (
    Path(sys.executable).parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
DEFAULT_WORKSPACE_DIR = BASE_DIR / "workspace_data"
DATA_FILE = BASE_DIR / "concepts.json"

PROVIDER_CHOICES = {
    "Auto": "auto",
    "Groq (gpt-oss-20b)": "groq",
    "Gemini (3.5-flash-lite)": "gemini",
    "OpenAI (gpt-4o-mini)": "openai",
    "Claude (sonnet-4-5)": "claude",
    "OpenRouter (free)": "openrouter",
}

LEVEL_OPTIONS = [
    "Level 1: Recognition (Syntax Mechanics)",
    "Level 2: Explanation (Runtime & Memory)",
    "Level 3: Prediction (Output & State Changes)",
    "Level 4: Modification (Behavior Changes)",
    "Level 5: Reconstruction (Idiomatic Rewrites)",
    "Level 6: Transfer (Cross-Domain Usage)",
]


def load_concepts():
    if not DATA_FILE.exists():
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return {
                    c.get("name", f"Concept_{i}"): c.get("level", 1)
                    for i, c in enumerate(data)
                }
            return data
    except Exception:
        return {}


def save_concept_level(name: str, level: int):
    concepts = load_concepts()
    concepts[name] = max(concepts.get(name, 1), level)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(concepts, f, indent=2)


def get_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def sanitize_mermaid(raw_code: str) -> str:
    """Sanitizes raw Mermaid syntax and enforces high-visibility white connector

    lines across all external rendering engines.
    """
    code = raw_code.strip()
    code = re.sub(r"^```(?:mermaid)?", "", code, flags=re.MULTILINE)
    code = re.sub(r"```$", "", code, flags=re.MULTILINE)
    code = re.sub(r"%%\{.*?\}%%", "", code, flags=re.DOTALL).strip()

    lines = []
    has_header = False
    for line in code.splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue
        if trimmed.startswith(("flowchart", "graph")):
            has_header = True
            lines.append(trimmed)
            continue

        trimmed = re.sub(r"\s*&\s*", " and ", trimmed)
        trimmed = re.sub(r"--\s*([^-\n>|]+?)\s*-->", r"-->|\1|", trimmed)

        def wrap_brackets(m):
            inner = m.group(1).strip()
            if inner.startswith('"') and inner.endswith('"'):
                inner = inner[1:-1]
            inner = inner.replace('"', "'")
            return f'["{inner}"]'

        def wrap_braces(m):
            inner = m.group(1).strip()
            if inner.startswith('"') and inner.endswith('"'):
                inner = inner[1:-1]
            inner = inner.replace('"', "'")
            return f'{{"{inner}"}}'

        trimmed = re.sub(r'\[([^\[\]\n]+?)\]', wrap_brackets, trimmed)
        trimmed = re.sub(r'\{([^{}\n]+?)\}', wrap_braces, trimmed)
        lines.append(trimmed)

    if not has_header:
        lines.insert(0, "flowchart TD")

    # Force all connector links to render bright white
    lines.append("    linkStyle default stroke:#ffffff,stroke-width:2.5px;")

    # Prepend dark theme directive with white line and arrow styling
    init_header = '%%{init: {"theme": "dark", "themeVariables": {"darkMode": true, "lineColor": "#ffffff", "arrowheadColor": "#ffffff", "primaryColor": "#1e293b", "primaryTextColor": "#ffffff", "primaryBorderColor": "#60a5fa"}}}%%'
    return init_header + "\n" + "\n".join(lines)


def enhance_svg_dark_contrast(svg_text: str) -> str:
    """Injects an explicit dark background (#18181b) and forces all link paths,

    arrowheads, and text to high-contrast white so the SVG displays properly in browsers.
    """
    custom_style = """
    <style>
      svg { background-color: #18181b !important; }
      .edgePaths path, .flowchart-link, path.path { stroke: #ffffff !important; stroke-width: 2.5px !important; }
      .marker, marker path, #flowchart-pointEnd, #flowchart-pointStart { fill: #ffffff !important; stroke: #ffffff !important; }
      .edgeLabel { color: #ffffff !important; background-color: #18181b !important; }
      .edgeLabel rect { fill: #18181b !important; opacity: 0.85 !important; }
      .edgeLabel span { color: #ffffff !important; background-color: #18181b !important; padding: 2px 4px; border-radius: 4px; }
      .node rect, .node circle, .node polygon { stroke-width: 2px !important; }
    </style>
    """
    if "<svg" in svg_text:
        def inject_svg_attrs(match):
            tag = match.group(0)
            if "style=" in tag:
                tag = re.sub(r'style="([^"]*)"', r'style="background-color: #18181b; \1"', tag)
            else:
                tag = tag[:-1] + ' style="background-color: #18181b;">'
            return tag + custom_style

        svg_text = re.sub(r'(<svg[^>]*>)', inject_svg_attrs, svg_text, count=1)

    # Overwrite legacy dark strokes and fills with white
    svg_text = re.sub(r'stroke="#333333"', 'stroke="#ffffff"', svg_text)
    svg_text = re.sub(r'stroke="#333"', 'stroke="#ffffff"', svg_text)
    svg_text = re.sub(r'stroke="black"', 'stroke="#ffffff"', svg_text)
    svg_text = re.sub(r'fill="#333333"', 'fill="#ffffff"', svg_text)
    svg_text = re.sub(r'fill="#333"', 'fill="#ffffff"', svg_text)
    return svg_text


def encode_mermaid_live_pako(clean_code: str) -> str:
    """Builds the payload format used by the official Mermaid Live Editor with dark styling."""
    state = {
        "code": clean_code,
        "mermaid": json.dumps({
            "theme": "dark",
            "themeVariables": {
                "darkMode": True,
                "background": "#18181b",
                "lineColor": "#60a5fa",
                "arrowheadColor": "#60a5fa",
                "primaryColor": "#1e293b",
                "primaryTextColor": "#f8fafc",
                "primaryBorderColor": "#3b82f6"
            }
        }, indent=2),
        "autoSync": True,
        "updateDiagram": True,
    }
    json_bytes = json.dumps(state).encode("utf-8")
    compressed = zlib.compress(json_bytes, 9)
    return base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")


def fetch_diagram_data(clean_code: str, workspace_dir: Path) -> tuple[bytes | None, Path, Path]:
    """Fetches PNG and SVG assets with bright link lines and prepares local viewing files."""
    ssl_ctx = get_ssl_context()
    browser_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }

    png_data = None
    svg_text = None

    # Step A: Fetch SVG vector markup
    try:
        req_svg = urllib.request.Request(
            "https://kroki.io/mermaid/svg",
            data=clean_code.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8", **browser_headers},
        )
        with urllib.request.urlopen(req_svg, timeout=10, context=ssl_ctx) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            if "<svg" in content:
                svg_text = content
    except Exception:
        pass

    # Save SVG with enhanced line contrast
    local_svg_path = workspace_dir / "dataflow_diagram.svg"
    if svg_text:
        svg_text = enhance_svg_dark_contrast(svg_text)
        local_svg_path.write_text(svg_text, encoding="utf-8")
    else:
        fallback_svg = """<svg xmlns="http://www.w3.org/2000/svg" width="600" height="200" style="background:#18181b">
        <text x="30" y="50" fill="#38bdf8" font-family="monospace" font-size="14">Mermaid Diagram Generated</text>
        <text x="30" y="90" fill="#a1a1aa" font-family="monospace" font-size="12">Open Mermaid Live Editor to view full interactive diagram.</text>
        </svg>"""
        local_svg_path.write_text(fallback_svg, encoding="utf-8")

    # Step B: Render / Fetch PNG Bytes for In-App Display
    # Option 1: QuickChart JSON POST
    if not png_data:
        try:
            payload = json.dumps({
                "chart": clean_code,
                "format": "png",
                "theme": "dark",
                "width": 640,
                "backgroundColor": "#18181b"
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://quickchart.io/mermaid",
                data=payload,
                headers={"Content-Type": "application/json", **browser_headers},
            )
            with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
                data = resp.read()
                if data.startswith(b"\x89PNG"):
                    png_data = data
        except Exception:
            pass

    # Option 2: Mermaid.ink pako PNG request
    if not png_data:
        try:
            pako_str = encode_mermaid_live_pako(clean_code)
            url = f"https://mermaid.ink/img/pako:{pako_str}?bgColor=18181b"
            req = urllib.request.Request(url, headers=browser_headers)
            with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
                data = resp.read()
                if data.startswith(b"\x89PNG"):
                    png_data = data
        except Exception:
            pass

    # Option 3: Kroki Plain Text POST
    if not png_data:
        try:
            req = urllib.request.Request(
                "https://kroki.io/mermaid/png",
                data=clean_code.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8", "Accept": "image/png", **browser_headers},
            )
            with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
                data = resp.read()
                if data.startswith(b"\x89PNG"):
                    png_data = data
        except Exception:
            pass

    # Step C: Save Windows-safe HTML redirect bridge for Live Editor
    pako_str = encode_mermaid_live_pako(clean_code)
    live_target_url = f"https://mermaid.live/edit#pako:{pako_str}"
    local_bridge_path = workspace_dir / "open_live_editor.html"
    bridge_html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Opening Mermaid Live Editor...</title>
  <meta http-equiv="refresh" content="0; url={live_target_url}">
  <script>window.location.replace("{live_target_url}");</script>
</head>
<body style="background:#18181b;color:#a1a1aa;font-family:sans-serif;padding:32px;text-align:center;">
  <h2>Redirecting to Mermaid Live Editor...</h2>
  <p>If your browser does not redirect automatically, <a href="{live_target_url}" style="color:#38bdf8;">click here</a>.</p>
</body>
</html>"""
    local_bridge_path.write_text(bridge_html, encoding="utf-8")

    return png_data, local_svg_path, local_bridge_path


def make_read_only(widget: tk.Text):
    widget.config(
        state="disabled",
        cursor="xterm",
        selectbackground="#2563eb",
        selectforeground="#ffffff",
    )

    widget.tag_configure("sel", background="#2563eb", foreground="#ffffff")
    widget.tag_raise("sel")

    widget.bind("<Button-1>", lambda e: e.widget.focus_set(), add="+")

    def copy_selection(event=None):
        try:
            selected_text = widget.get(tk.SEL_FIRST, tk.SEL_LAST)
            widget.clipboard_clear()
            widget.clipboard_append(selected_text)
        except tk.TclError:
            pass
        return "break"

    def select_all(event=None):
        widget.tag_add(tk.SEL, "1.0", "end-1c")
        return "break"

    widget.bind("<Control-c>", copy_selection)
    widget.bind("<Control-C>", copy_selection)
    widget.bind("<Control-a>", select_all)
    widget.bind("<Control-A>", select_all)

    menu = tk.Menu(
        widget,
        tearoff=0,
        bg="#27272a",
        fg="#f4f4f5",
        activebackground="#3b82f6",
        activeforeground="#ffffff",
    )
    menu.add_command(label="Copy (Ctrl+C)", command=copy_selection)
    menu.add_command(label="Select All (Ctrl+A)", command=select_all)

    def show_context_menu(event):
        try:
            widget.focus_set()
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    widget.bind("<Button-3>", show_context_menu)


def render_markdown(widget: tk.Text, markdown_text: str):
    lines = markdown_text.split("\n")
    i = 0
    in_code_block = False
    code_lines = []

    def render_inline(text_str: str, default_tag: str = "normal"):
        pattern = r"(\*\*.*?\*\*|`.*?`|\*[^*\n]+?\*)"
        tokens = re.split(pattern, text_str)
        for token in tokens:
            if not token:
                continue
            if token.startswith("**") and token.endswith("**") and len(token) >= 4:
                widget.insert(tk.END, token[2:-2], "bold")
            elif token.startswith("`") and token.endswith("`") and len(token) >= 2:
                widget.insert(tk.END, f" {token[1:-1]} ", "inline_code")
            elif token.startswith("*") and token.endswith("*") and len(token) >= 2:
                widget.insert(tk.END, token[1:-1], "quote")
            else:
                widget.insert(tk.END, token, default_tag)

    while i < len(lines):
        line = lines[i]

        if line.strip().startswith("```"):
            if in_code_block:
                widget.insert(
                    tk.END, "\n".join(code_lines) + "\n\n", "code_block"
                )
                code_lines = []
                in_code_block = False
            else:
                in_code_block = True
                code_lines = []
            i += 1
            continue

        if in_code_block:
            code_lines.append(line)
            i += 1
            continue

        stripped = line.strip()

        if stripped in (
            "---",
            "***",
            "___",
            "===",
            "==================================================",
        ):
            widget.insert(tk.END, "\n" + "─" * 60 + "\n\n", "divider")
            i += 1
            continue

        if stripped.startswith("#### "):
            widget.insert(tk.END, "\n" + stripped[5:] + "\n", "h4")
            i += 1
            continue
        if stripped.startswith("### "):
            widget.insert(tk.END, "\n" + stripped[4:] + "\n", "h3")
            i += 1
            continue
        if stripped.startswith("## "):
            widget.insert(tk.END, "\n" + stripped[3:] + "\n", "h2")
            i += 1
            continue
        if stripped.startswith("# "):
            widget.insert(tk.END, "\n" + stripped[2:] + "\n", "h1")
            i += 1
            continue

        if stripped.startswith("> "):
            widget.insert(tk.END, "  │ ", "divider")
            render_inline(stripped[2:] + "\n", default_tag="quote")
            i += 1
            continue

        if stripped.startswith(("- ", "* ", "+ ")):
            widget.insert(tk.END, "  • ", "bold")
            render_inline(stripped[2:] + "\n", default_tag="normal")
            i += 1
            continue

        if stripped == "":
            widget.insert(tk.END, "\n")
        else:
            render_inline(line + "\n", default_tag="normal")
        i += 1

    widget.tag_raise("sel")


class LearnerApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Self Learner")
        self.geometry("1200x890")
        self.configure(bg="#18181b")

        self._init_workspace_dirs()

        self.base_font_size = 10
        self.base_code_size = 10

        self.cached_snippet = ""
        self.chat_history = []
        self.current_chat_id = None
        self._image_refs = []

        self.active_concept_name = ""
        self.active_level = 1
        self.active_question = ""
        self.awaiting_quiz_answer = False

        self._autoscroll_widget = None
        self._autoscroll_anchor_y = 0
        self._autoscroll_current_y = 0
        self._autoscroll_timer = None

        self._init_ttk_styles()
        self._build_ui()
        self._init_per_widget_zoom()
        self._init_autoscroll()
        self._restore_session()
        self.after(80, self._init_sashes)

    def _init_workspace_dirs(self):
        settings_file = BASE_DIR / "workspace_settings.json"
        self.workspace_dir = DEFAULT_WORKSPACE_DIR
        if settings_file.exists():
            try:
                with open(settings_file, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    custom_path = cfg.get("workspace_dir")
                    if custom_path and Path(custom_path).exists():
                        self.workspace_dir = Path(custom_path)
            except Exception:
                pass

        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        (self.workspace_dir / "chats").mkdir(parents=True, exist_ok=True)

    def _save_workspace_settings(self):
        settings_file = BASE_DIR / "workspace_settings.json"
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump({"workspace_dir": str(self.workspace_dir)}, f, indent=2)

    def _init_ttk_styles(self):
        self.style = ttk.Style(self)
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        # Dark styling for Project Explorer Treeview
        self.style.configure(
            "Treeview",
            background="#121214",
            foreground="#d4d4d8",
            fieldbackground="#121214",
            borderwidth=0,
            font=("Segoe UI", 9),
            rowheight=22,
        )
        self.style.map(
            "Treeview",
            background=[("selected", "#2563eb")],
            foreground=[("selected", "#ffffff")],
        )

        self.style.configure(
            "Dark.Horizontal.TScrollbar",
            gripcount=0,
            background="#333336",
            darkcolor="#18181b",
            lightcolor="#18181b",
            troughcolor="#18181b",
            bordercolor="#18181b",
            arrowcolor="#71717a",
            relief="flat",
            arrowsize=10,
        )
        self.style.map(
            "Dark.Horizontal.TScrollbar",
            background=[("active", "#4e4e52"), ("disabled", "#222225")],
            arrowcolor=[("active", "#ffffff")],
        )

        self.style.configure(
            "TCombobox",
            fieldbackground="#18181b",
            background="#27272a",
            foreground="#ffffff",
            arrowcolor="#ffffff",
            darkcolor="#27272a",
            lightcolor="#27272a",
            bordercolor="#3f3f46",
        )
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", "#18181b")],
            selectbackground=[("readonly", "#18181b")],
            selectforeground=[("readonly", "#ffffff")],
        )

    def _build_ui(self):
        nav_frame = tk.Frame(self, bg="#27272a", padx=12, pady=8)
        nav_frame.pack(fill="x")

        self.btn_dissect = tk.Button(
            nav_frame,
            text="[ Dissector ]",
            bg="#3b82f6",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            command=self._set_mode_dissect,
        )
        self.btn_dissect.pack(side="left", padx=2)

        self.btn_compress = tk.Button(
            nav_frame,
            text="[ Compressor ]",
            bg="#3f3f46",
            fg="#d4d4d8",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            command=self._set_mode_compress,
        )
        self.btn_compress.pack(side="left", padx=2)

        self.btn_chat = tk.Button(
            nav_frame,
            text="[ Mentor Chat ]",
            bg="#3f3f46",
            fg="#d4d4d8",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            command=self._set_mode_chat,
        )
        self.btn_chat.pack(side="left", padx=2)

        self.btn_quiz = tk.Button(
            nav_frame,
            text="[ Concept Quiz ]",
            bg="#3f3f46",
            fg="#d4d4d8",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            command=self._set_mode_quiz,
        )
        self.btn_quiz.pack(side="left", padx=2)

        self.cache_chip = tk.Label(
            nav_frame,
            text="Snippet: None",
            bg="#3f3f46",
            fg="#a1a1aa",
            font=("Consolas", 8, "bold"),
            padx=6,
            pady=2,
        )
        self.cache_chip.pack(side="left", padx=6)

        self.btn_clear_cache = tk.Button(
            nav_frame,
            text="Reset",
            bg="#27272a",
            fg="#f87171",
            relief="flat",
            font=("Segoe UI", 8),
            command=self._clear_cached_snippet,
        )
        self.btn_clear_cache.pack(side="left", padx=2)

        # GitHub Importer Button
        self.btn_gh_import = tk.Button(
            nav_frame,
            text="🌐 Import GitHub",
            bg="#18181b",
            fg="#38bdf8",
            relief="flat",
            font=("Segoe UI", 8, "bold"),
            padx=6,
            command=self._import_from_github,
        )
        self.btn_gh_import.pack(side="left", padx=6)

        # API Keys Management Button
        self.btn_api_keys = tk.Button(
            nav_frame,
            text="🔑 API Keys",
            bg="#18181b",
            fg="#a78bfa",
            relief="flat",
            font=("Segoe UI", 8, "bold"),
            padx=6,
            command=self._open_api_keys_modal,
        )
        self.btn_api_keys.pack(side="left", padx=2)

        self.btn_workspace = tk.Button(
            nav_frame,
            text="📁 Workspace",
            bg="#18181b",
            fg="#a1a1aa",
            relief="flat",
            font=("Segoe UI", 8),
            padx=6,
            command=self._choose_workspace_dir,
        )
        self.btn_workspace.pack(side="left", padx=2)

        self.btn_continue = tk.Button(
            nav_frame,
            text="⚠️ Continue Output",
            bg="#f59e0b",
            fg="#000000",
            relief="flat",
            font=("Segoe UI", 8, "bold"),
            padx=6,
            command=self._trigger_continue,
        )

        self.status_lbl = tk.Label(
            nav_frame,
            text="Ready",
            bg="#27272a",
            fg="#a1a1aa",
            font=("Segoe UI", 9),
        )
        self.status_lbl.pack(side="right", padx=(8, 0))

        self.provider_combo = ttk.Combobox(
            nav_frame,
            values=list(PROVIDER_CHOICES.keys()),
            state="readonly",
            width=20,
        )
        self.provider_combo.current(0)
        self.provider_combo.pack(side="right", padx=(4, 8))

        tk.Label(
            nav_frame,
            text="Provider:",
            bg="#27272a",
            fg="#a1a1aa",
            font=("Segoe UI", 9),
        ).pack(side="right")

        self.paned_standard = tk.PanedWindow(
            self,
            orient=tk.HORIZONTAL,
            bg="#3f3f46",
            sashwidth=6,
            sashrelief="flat",
            bd=0,
            opaqueresize=True,
        )
        self.paned_standard.pack(fill="both", expand=True, padx=8, pady=8)

        # LEFT PANE
        self.left_pane = tk.Frame(self.paned_standard, bg="#18181b")
        self.paned_standard.add(self.left_pane, minsize=320)

        self.left_ctrl_bar = tk.Frame(self.left_pane, bg="#27272a", padx=10, pady=8)
        self.left_ctrl_bar.pack(fill="x")

        # Dissector Controls
        self.dissect_left_bar = tk.Frame(self.left_ctrl_bar, bg="#27272a")
        self.dissect_left_bar.pack(fill="x")

        tk.Label(
            self.dissect_left_bar,
            text="Focus:",
            bg="#27272a",
            fg="#e4e4e7",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=(0, 4))

        self.focus_mode_combo = ttk.Combobox(
            self.dissect_left_bar,
            values=[
                "Summary (Mechanics First)",
                "Line-by-Line Mechanics Only",
                "Fixes & Clean Alternatives",
                "Trade-offs & Gotchas Only",
            ],
            state="readonly",
            width=25,
        )
        self.focus_mode_combo.current(0)
        self.focus_mode_combo.pack(side="left", padx=(0, 6))

        self.btn_run_dissect = tk.Button(
            self.dissect_left_bar,
            text="Run (Ctrl+Enter)",
            bg="#3b82f6",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            command=self._on_action_click,
        )
        self.btn_run_dissect.pack(side="right")

        # Chat Controls
        self.chat_left_bar = tk.Frame(self.left_ctrl_bar, bg="#27272a")
        tk.Label(
            self.chat_left_bar,
            text="💬 Mentor Dialogue",
            bg="#27272a",
            fg="#a78bfa",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        self.btn_chat_hist = tk.Button(
            self.chat_left_bar,
            text="📂 History",
            bg="#3f3f46",
            fg="#d4d4d8",
            relief="flat",
            font=("Segoe UI", 8),
            command=self._open_chat_history_modal,
        )
        self.btn_chat_hist.pack(side="right", padx=(4, 0))

        tk.Button(
            self.chat_left_bar,
            text="+ New Chat",
            bg="#3f3f46",
            fg="#d4d4d8",
            relief="flat",
            font=("Segoe UI", 8),
            command=self._start_new_chat,
        ).pack(side="right")

        # Quiz Controls
        self.quiz_left_bar = tk.Frame(self.left_ctrl_bar, bg="#27272a")
        tk.Label(
            self.quiz_left_bar,
            text="Difficulty:",
            bg="#27272a",
            fg="#e4e4e7",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=2)

        self.level_combo = ttk.Combobox(
            self.quiz_left_bar,
            values=LEVEL_OPTIONS,
            state="readonly",
            width=45,
        )
        self.level_combo.current(0)
        self.level_combo.pack(side="left", padx=4)

        self.btn_quiz_action = tk.Button(
            self.quiz_left_bar,
            text="Generate",
            bg="#10b981",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            command=self._on_action_click,
        )
        self.btn_quiz_action.pack(side="right")

        out_banner = tk.Frame(self.left_pane, bg="#121214", padx=10, pady=4)
        out_banner.pack(fill="x", pady=(4, 0))
        tk.Label(
            out_banner,
            text="OUTPUT & RESPONSES [READ-ONLY — SELECTABLE]",
            bg="#121214",
            fg="#71717a",
            font=("Consolas", 8, "bold"),
        ).pack(side="left")

        out_frame = tk.Frame(self.left_pane, bg="#18181b")
        out_frame.pack(fill="both", expand=True)

        self.output_txt = tk.Text(
            out_frame,
            bg="#121214",
            fg="#d4d4d8",
            insertbackground="#121214",
            relief="flat",
            wrap="word",
            padx=14,
            pady=14,
        )
        self.output_txt.pack(side="left", fill="both", expand=True)
        make_read_only(self.output_txt)

        self.scroll_out_v = ttk.Scrollbar(
            out_frame,
            orient="vertical",
            command=self.output_txt.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.scroll_out_v.pack(side="right", fill="y")
        self.output_txt.config(yscrollcommand=self.scroll_out_v.set)

        # RIGHT PANE
        self.right_pane = tk.Frame(self.paned_standard, bg="#18181b")
        self.paned_standard.add(self.right_pane, minsize=320)

        # Dissector Right View
        self.dissect_right_frame = tk.Frame(self.right_pane, bg="#18181b")
        self.dissect_right_frame.pack(fill="both", expand=True)

        right_top = tk.Frame(
            self.dissect_right_frame, bg="#27272a", padx=10, pady=8
        )
        right_top.pack(fill="x")
        tk.Label(
            right_top,
            text="PYTHON CODE SNIPPET (INPUT)",
            bg="#27272a",
            fg="#38bdf8",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        tk.Button(
            right_top,
            text="Clear Code",
            bg="#3f3f46",
            fg="#d4d4d8",
            relief="flat",
            font=("Segoe UI", 8),
            command=lambda: self.dissect_code_txt.delete("1.0", tk.END),
        ).pack(side="right")

        code_box_frame = tk.Frame(self.dissect_right_frame, bg="#121214")
        code_box_frame.pack(fill="both", expand=True, pady=(6, 0))

        self.dissect_code_txt = tk.Text(
            code_box_frame,
            bg="#121214",
            fg="#f4f4f5",
            insertbackground="white",
            relief="flat",
            wrap="none",
            padx=10,
            pady=10,
        )
        self.dissect_code_txt.pack(side="left", fill="both", expand=True)

        self.dissect_v_scroll = ttk.Scrollbar(
            code_box_frame,
            orient="vertical",
            command=self.dissect_code_txt.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.dissect_v_scroll.pack(side="right", fill="y")
        self.dissect_code_txt.config(yscrollcommand=self.dissect_v_scroll.set)

        self.dissect_h_scroll = ttk.Scrollbar(
            self.dissect_right_frame,
            orient="horizontal",
            command=self.dissect_code_txt.xview,
            style="Dark.Horizontal.TScrollbar",
        )
        self.dissect_h_scroll.pack(fill="x")
        self.dissect_code_txt.config(xscrollcommand=self.dissect_h_scroll.set)

        # Chat Right View
        self.chat_right_frame = tk.Frame(self.right_pane, bg="#18181b")

        chat_top = tk.Frame(
            self.chat_right_frame, bg="#27272a", padx=10, pady=6
        )
        chat_top.pack(fill="x")
        tk.Label(
            chat_top,
            text="ACTIVE CODE SNIPPET (INPUT)",
            bg="#27272a",
            fg="#38bdf8",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        chat_code_box = tk.Frame(self.chat_right_frame, bg="#121214")
        chat_code_box.pack(fill="both", expand=True, pady=(4, 6))

        self.chat_code_txt = tk.Text(
            chat_code_box,
            height=12,
            bg="#121214",
            fg="#f4f4f5",
            insertbackground="white",
            relief="flat",
            wrap="none",
            padx=8,
            pady=8,
        )
        self.chat_code_txt.pack(side="left", fill="both", expand=True)

        self.chat_code_v_scroll = ttk.Scrollbar(
            chat_code_box,
            orient="vertical",
            command=self.chat_code_txt.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.chat_code_v_scroll.pack(side="right", fill="y")
        self.chat_code_txt.config(yscrollcommand=self.chat_code_v_scroll.set)

        chat_mid = tk.Frame(
            self.chat_right_frame, bg="#27272a", padx=10, pady=6
        )
        chat_mid.pack(fill="x")
        tk.Label(
            chat_mid,
            text="YOUR QUESTION / INQUIRY (INPUT)",
            bg="#27272a",
            fg="#a78bfa",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        chat_query_box = tk.Frame(self.chat_right_frame, bg="#18181b")
        chat_query_box.pack(fill="x", pady=(4, 6))

        self.chat_query_txt = tk.Text(
            chat_query_box,
            height=8,
            bg="#18181b",
            fg="#f4f4f5",
            insertbackground="white",
            relief="flat",
            wrap="word",
            padx=8,
            pady=8,
        )
        self.chat_query_txt.pack(side="left", fill="both", expand=True)

        self.chat_query_v_scroll = ttk.Scrollbar(
            chat_query_box,
            orient="vertical",
            command=self.chat_query_txt.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.chat_query_v_scroll.pack(side="right", fill="y")
        self.chat_query_txt.config(yscrollcommand=self.chat_query_v_scroll.set)

        chat_btn_bar = tk.Frame(self.chat_right_frame, bg="#18181b")
        chat_btn_bar.pack(fill="x")
        self.btn_chat_send = tk.Button(
            chat_btn_bar,
            text="Send Message (Ctrl+Enter)",
            bg="#8b5cf6",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            command=self._on_action_click,
        )
        self.btn_chat_send.pack(side="right")

        # Quiz Right View
        self.quiz_right_frame = tk.Frame(self.right_pane, bg="#18181b")

        quiz_top = tk.Frame(
            self.quiz_right_frame, bg="#27272a", padx=10, pady=6
        )
        quiz_top.pack(fill="x")
        tk.Label(
            quiz_top,
            text="ACTIVE CODE SNIPPET (INPUT)",
            bg="#27272a",
            fg="#38bdf8",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        quiz_code_box = tk.Frame(self.quiz_right_frame, bg="#121214")
        quiz_code_box.pack(fill="both", expand=True, pady=(4, 6))

        self.quiz_code_txt = tk.Text(
            quiz_code_box,
            height=12,
            bg="#121214",
            fg="#f4f4f5",
            insertbackground="white",
            relief="flat",
            wrap="none",
            padx=8,
            pady=8,
        )
        self.quiz_code_txt.pack(side="left", fill="both", expand=True)

        self.quiz_code_v_scroll = ttk.Scrollbar(
            quiz_code_box,
            orient="vertical",
            command=self.quiz_code_txt.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.quiz_code_v_scroll.pack(side="right", fill="y")
        self.quiz_code_txt.config(yscrollcommand=self.quiz_code_v_scroll.set)

        self.quiz_mid_lbl = tk.Label(
            self.quiz_right_frame,
            text="YOUR ANSWER (INPUT):",
            bg="#27272a",
            fg="#10b981",
            font=("Segoe UI", 9, "bold"),
            padx=10,
            pady=6,
        )
        self.quiz_mid_lbl.pack(fill="x")

        quiz_ans_box = tk.Frame(self.quiz_right_frame, bg="#18181b")
        quiz_ans_box.pack(fill="x", pady=(4, 6))

        self.quiz_answer_txt = tk.Text(
            quiz_ans_box,
            height=8,
            bg="#18181b",
            fg="#f4f4f5",
            insertbackground="white",
            relief="flat",
            wrap="word",
            padx=8,
            pady=8,
        )
        self.quiz_answer_txt.pack(side="left", fill="both", expand=True)

        self.quiz_ans_v_scroll = ttk.Scrollbar(
            quiz_ans_box,
            orient="vertical",
            command=self.quiz_answer_txt.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.quiz_ans_v_scroll.pack(side="right", fill="y")
        self.quiz_answer_txt.config(yscrollcommand=self.quiz_ans_v_scroll.set)

        quiz_btn_bar = tk.Frame(self.quiz_right_frame, bg="#18181b")
        quiz_btn_bar.pack(fill="x")
        self.btn_quiz_submit = tk.Button(
            quiz_btn_bar,
            text="Submit Answer (Ctrl+Enter)",
            bg="#10b981",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            command=self._on_action_click,
        )
        self.btn_quiz_submit.pack(side="right")

        # Compressor PanedWindow
        self.paned_compress = tk.PanedWindow(
            self,
            orient=tk.HORIZONTAL,
            bg="#3f3f46",
            sashwidth=6,
            sashrelief="flat",
            bd=0,
            opaqueresize=True,
        )

        self.comp_left_pane = tk.Frame(self.paned_compress, bg="#18181b")
        self.paned_compress.add(self.comp_left_pane, minsize=240)

        c_left_top = tk.Frame(
            self.comp_left_pane, bg="#27272a", padx=10, pady=8
        )
        c_left_top.pack(fill="x")
        tk.Label(
            c_left_top,
            text="1. RAW CODE (INPUT)",
            bg="#27272a",
            fg="#38bdf8",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        self.btn_run_compress = tk.Button(
            c_left_top,
            text="Compress (Ctrl+Enter)",
            bg="#e11d48",
            fg="white",
            relief="flat",
            font=("Segoe UI", 8, "bold"),
            command=self._on_action_click,
        )
        self.btn_run_compress.pack(side="right")

        c_box1 = tk.Frame(self.comp_left_pane, bg="#121214")
        c_box1.pack(fill="both", expand=True, pady=(4, 0))

        self.compress_raw_txt = tk.Text(
            c_box1,
            bg="#121214",
            fg="#f4f4f5",
            insertbackground="white",
            relief="flat",
            wrap="none",
            padx=8,
            pady=8,
        )
        self.compress_raw_txt.pack(side="left", fill="both", expand=True)

        self.comp_raw_v_scroll = ttk.Scrollbar(
            c_box1,
            orient="vertical",
            command=self.compress_raw_txt.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.comp_raw_v_scroll.pack(side="right", fill="y")
        self.compress_raw_txt.config(yscrollcommand=self.comp_raw_v_scroll.set)

        self.comp_raw_h_scroll = ttk.Scrollbar(
            self.comp_left_pane,
            orient="horizontal",
            command=self.compress_raw_txt.xview,
            style="Dark.Horizontal.TScrollbar",
        )
        self.comp_raw_h_scroll.pack(fill="x")
        self.compress_raw_txt.config(xscrollcommand=self.comp_raw_h_scroll.set)

        self.comp_mid_pane = tk.Frame(self.paned_compress, bg="#18181b")
        self.paned_compress.add(self.comp_mid_pane, minsize=240)

        c_mid_top = tk.Frame(
            self.comp_mid_pane, bg="#27272a", padx=10, pady=8
        )
        c_mid_top.pack(fill="x")
        tk.Label(
            c_mid_top,
            text="2. TRANSFORMATIONS & AUDIT",
            bg="#27272a",
            fg="#fbbf24",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        c_box2 = tk.Frame(self.comp_mid_pane, bg="#121214")
        c_box2.pack(fill="both", expand=True, pady=(4, 0))

        self.compress_audit_txt = tk.Text(
            c_box2,
            bg="#121214",
            fg="#d4d4d8",
            insertbackground="#121214",
            relief="flat",
            wrap="word",
            padx=10,
            pady=10,
        )
        self.compress_audit_txt.pack(side="left", fill="both", expand=True)
        make_read_only(self.compress_audit_txt)

        self.comp_audit_v_scroll = ttk.Scrollbar(
            c_box2,
            orient="vertical",
            command=self.compress_audit_txt.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.comp_audit_v_scroll.pack(side="right", fill="y")
        self.compress_audit_txt.config(
            yscrollcommand=self.comp_audit_v_scroll.set
        )

        self.comp_right_pane = tk.Frame(self.paned_compress, bg="#18181b")
        self.paned_compress.add(self.comp_right_pane, minsize=240)

        c_right_top = tk.Frame(
            self.comp_right_pane, bg="#27272a", padx=10, pady=8
        )
        c_right_top.pack(fill="x")
        tk.Label(
            c_right_top,
            text="3. COMPRESSED CODE ONLY",
            bg="#27272a",
            fg="#4ade80",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        self.btn_copy_code = tk.Button(
            c_right_top,
            text="Copy Code",
            bg="#10b981",
            fg="white",
            relief="flat",
            font=("Segoe UI", 8, "bold"),
            command=self._copy_compressed_code,
        )
        self.btn_copy_code.pack(side="right")

        c_box3 = tk.Frame(self.comp_right_pane, bg="#121214")
        c_box3.pack(fill="both", expand=True, pady=(4, 0))

        self.compress_code_txt = tk.Text(
            c_box3,
            bg="#0f172a",
            fg="#4ade80",
            insertbackground="#0f172a",
            relief="flat",
            wrap="none",
            padx=8,
            pady=8,
        )
        self.compress_code_txt.pack(side="left", fill="both", expand=True)
        make_read_only(self.compress_code_txt)

        self.comp_code_v_scroll = ttk.Scrollbar(
            c_box3,
            orient="vertical",
            command=self.compress_code_txt.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.comp_code_v_scroll.pack(side="right", fill="y")
        self.compress_code_txt.config(
            yscrollcommand=self.comp_code_v_scroll.set
        )

        self.comp_code_h_scroll = ttk.Scrollbar(
            self.comp_right_pane,
            orient="horizontal",
            command=self.compress_code_txt.xview,
            style="Dark.Horizontal.TScrollbar",
        )
        self.comp_code_h_scroll.pack(fill="x")
        self.compress_code_txt.config(xscrollcommand=self.comp_code_h_scroll.set)

        self.current_mode = "dissect"

    def _init_per_widget_zoom(self):
        self._panel_names = {
            self.output_txt: "Output Feed",
            self.dissect_code_txt: "Code Editor",
            self.chat_code_txt: "Chat Snippet",
            self.chat_query_txt: "Chat Query Box",
            self.quiz_code_txt: "Quiz Snippet",
            self.quiz_answer_txt: "Quiz Answer Box",
            self.compress_raw_txt: "Compressor (Raw)",
            self.compress_audit_txt: "Compressor (Audit)",
            self.compress_code_txt: "Compressor (Result)",
        }

        self._zoom_levels = {w: 10 for w in self._panel_names}

        for w in self._panel_names:
            w.bind("<Control-equal>", lambda e, wid=w: self._zoom_widget(wid, 1))
            w.bind("<Control-plus>", lambda e, wid=w: self._zoom_widget(wid, 1))
            w.bind("<Control-KP_Add>", lambda e, wid=w: self._zoom_widget(wid, 1))
            w.bind("<Control-minus>", lambda e, wid=w: self._zoom_widget(wid, -1))
            w.bind(
                "<Control-KP_Subtract>",
                lambda e, wid=w: self._zoom_widget(wid, -1),
            )
            w.bind(
                "<Control-0>",
                lambda e, wid=w: self._zoom_widget(wid, 0, reset=True),
            )
            w.bind("<Control-MouseWheel>", lambda e, wid=w: self._on_wheel_zoom(e, wid))

        self.bind("<Control-equal>", lambda e: self._zoom_focused(1))
        self.bind("<Control-plus>", lambda e: self._zoom_focused(1))
        self.bind("<Control-KP_Add>", lambda e: self._zoom_focused(1))
        self.bind("<Control-minus>", lambda e: self._zoom_focused(-1))
        self.bind("<Control-KP_Subtract>", lambda e: self._zoom_focused(-1))
        self.bind("<Control-0>", lambda e: self._zoom_focused(0, reset=True))
        self.bind("<Control-Return>", lambda e: self._on_action_click())

        for w in self._panel_names:
            self._apply_widget_font(w)

    def _init_autoscroll(self):
        for w in self._panel_names:
            w.bind("<Button-2>", lambda e, wid=w: self._toggle_autoscroll(e, wid))

    def _toggle_autoscroll(self, event, widget: tk.Text):
        if self._autoscroll_widget == widget:
            self._stop_autoscroll()
            return "break"

        self._stop_autoscroll()
        self._autoscroll_widget = widget
        self._autoscroll_anchor_y = event.y_root
        self._autoscroll_current_y = event.y_root

        try:
            widget.config(cursor="sb_v_double_arrow")
        except Exception:
            widget.config(cursor="fleur")

        widget.bind("<Motion>", self._on_autoscroll_motion)
        widget.bind("<Button-1>", lambda e: self._stop_autoscroll(), add="+")
        self.bind("<Escape>", lambda e: self._stop_autoscroll(), add="+")

        name = self._panel_names.get(widget, "Panel")
        self._set_status(f"Autoscroll Active ({name}) — Move mouse up/down")
        self._run_autoscroll_step()
        return "break"

    def _on_autoscroll_motion(self, event):
        self._autoscroll_current_y = event.y_root

    def _run_autoscroll_step(self):
        if not self._autoscroll_widget:
            return

        dy = self._autoscroll_current_y - self._autoscroll_anchor_y
        deadzone = 12

        if abs(dy) > deadzone:
            direction = 1 if dy > 0 else -1
            distance = abs(dy) - deadzone
            step = direction * max(1, int((distance / 20) ** 1.3))
            try:
                self._autoscroll_widget.yview_scroll(step, "units")
            except Exception:
                self._stop_autoscroll()
                return

        self._autoscroll_timer = self.after(35, self._run_autoscroll_step)

    def _stop_autoscroll(self):
        if self._autoscroll_timer:
            self.after_cancel(self._autoscroll_timer)
            self._autoscroll_timer = None

        if self._autoscroll_widget:
            try:
                self._autoscroll_widget.config(cursor="xterm")
                self._autoscroll_widget.unbind("<Motion>")
            except Exception:
                pass
            self._autoscroll_widget = None
            self._set_status("Ready")

    def _get_target_widget(self):
        focused = self.focus_get()
        if focused in self._panel_names:
            return focused

        x, y = self.winfo_pointerx(), self.winfo_pointery()
        hovered = self.winfo_containing(x, y)
        if hovered in self._panel_names:
            return hovered

        if self.current_mode == "compress":
            return self.compress_audit_txt
        elif self.current_mode == "dissect":
            return self.output_txt
        elif self.current_mode == "chat":
            return self.output_txt
        return self.output_txt

    def _zoom_focused(self, delta: int, reset: bool = False):
        target = self._get_target_widget()
        self._zoom_widget(target, delta, reset)

    def _on_wheel_zoom(self, event, widget):
        delta = 1 if event.delta > 0 else -1
        self._zoom_widget(widget, delta)
        return "break"

    def _zoom_widget(self, widget: tk.Text, delta: int, reset: bool = False):
        if reset:
            self._zoom_levels[widget] = 10
        else:
            self._zoom_levels[widget] = max(
                7, min(26, self._zoom_levels[widget] + delta)
            )

        self._apply_widget_font(widget)
        name = self._panel_names.get(widget, "Panel")
        pct = int((self._zoom_levels[widget] / 10) * 100)
        self._set_status(f"Zoom ({name}): {pct}%")
        return "break"

    def _apply_widget_font(self, widget: tk.Text):
        size = self._zoom_levels[widget]
        is_code = widget in [
            self.dissect_code_txt,
            self.chat_code_txt,
            self.quiz_code_txt,
            self.compress_raw_txt,
            self.compress_code_txt,
        ]

        if is_code:
            widget.config(font=("Consolas", size))
        else:
            widget.config(font=("Segoe UI", size))

        if widget in [self.output_txt, self.compress_audit_txt]:
            self._configure_text_tags(widget, size)

    def _configure_text_tags(self, txt: tk.Text, size: int):
        s = size
        cs = size

        txt.tag_configure("normal", font=("Segoe UI", s), foreground="#d4d4d8")
        txt.tag_configure(
            "meta_badge",
            font=("Segoe UI", max(7, s - 1), "bold"),
            foreground="#38bdf8",
            background="#0f172a",
        )
        txt.tag_configure(
            "chat_user_badge",
            font=("Segoe UI", s, "bold"),
            foreground="#fbbf24",
            background="#27272a",
        )
        txt.tag_configure(
            "meta_trail",
            font=("Segoe UI", max(7, s - 2)),
            foreground="#f87171",
        )
        txt.tag_configure(
            "h1",
            font=("Segoe UI", s + 4, "bold"),
            foreground="#60a5fa",
            spacing1=8,
            spacing3=4,
        )
        txt.tag_configure(
            "h2",
            font=("Segoe UI", s + 2, "bold"),
            foreground="#818cf8",
            spacing1=8,
            spacing3=4,
        )
        txt.tag_configure(
            "h3",
            font=("Segoe UI", s + 1, "bold"),
            foreground="#a78bfa",
            spacing1=6,
            spacing3=2,
        )
        txt.tag_configure(
            "h4",
            font=("Segoe UI", s, "bold"),
            foreground="#c084fc",
            spacing1=4,
            spacing3=2,
        )
        txt.tag_configure(
            "bold", font=("Segoe UI", s, "bold"), foreground="#ffffff"
        )
        txt.tag_configure(
            "inline_code",
            font=("Consolas", cs),
            foreground="#fb7185",
            background="#27272a",
        )
        txt.tag_configure(
            "code_block",
            font=("Consolas", cs),
            foreground="#4ade80",
            background="#18181b",
        )
        txt.tag_configure(
            "divider", font=("Consolas", cs), foreground="#3f3f46"
        )
        txt.tag_configure(
            "quote", font=("Segoe UI", s, "italic"), foreground="#94a3b8"
        )

        txt.tag_configure("sel", background="#2563eb", foreground="#ffffff")
        txt.tag_raise("sel")

    def _show_welcome_instructions(self):
        welcome_md = """### SELF LEARNER // WORKSPACE READY

**Visual Dataflow Diagrams:**
• Dissect code and click **`[ 📊 Generate Dataflow Diagram (Mermaid) ]`**.
• Renders visually in the feed as an embedded diagram, with an option to open the live interactive view in your browser.

**Navigation & Zoom:**
• **Cleaner Alternatives Only:** Focuses on idiomatic Python without noise.
• **VS Code Scrollbars:** Sleek, dark scrollbars line all panels.
• **Zoom Independently:** Use **Ctrl +** / **Ctrl -** (or **Ctrl + MouseWheel**) on any panel to scale font sizes.
"""
        self.output_txt.config(state="normal")
        self.output_txt.delete("1.0", tk.END)
        render_markdown(self.output_txt, welcome_md)
        self.output_txt.config(state="disabled")

    def _init_sashes(self):
        self.update_idletasks()
        w = self.winfo_width()
        if w > 400:
            self.paned_standard.sash_place(0, w // 2, 0)
            self.paned_compress.sash_place(0, w // 3, 0)
            self.paned_compress.sash_place(1, (2 * w) // 3, 0)
        else:
            self.after(80, self._init_sashes)

    def _update_cache_display(self):
        if self.cached_snippet:
            lines = len(self.cached_snippet.strip().split("\n"))
            self.cache_chip.config(
                text=f"Snippet: {lines} lines", bg="#065f46", fg="#6ee7b7"
            )
        else:
            self.cache_chip.config(
                text="Snippet: None", bg="#3f3f46", fg="#a1a1aa"
            )

    def _sync_active_snippet_from_ui(self):
        if self.current_mode == "dissect":
            code = self.dissect_code_txt.get("1.0", tk.END).strip()
        elif self.current_mode == "compress":
            code = self.compress_raw_txt.get("1.0", tk.END).strip()
        elif self.current_mode == "chat":
            code = self.chat_code_txt.get("1.0", tk.END).strip()
        elif self.current_mode == "quiz":
            code = self.quiz_code_txt.get("1.0", tk.END).strip()
        else:
            code = ""

        if code:
            self.cached_snippet = code
        self._update_cache_display()
        self._save_session_state()

    def _clear_cached_snippet(self):
        self._stop_autoscroll()
        self.cached_snippet = ""
        self.chat_history = []
        self.current_chat_id = None
        self.active_concept_name = ""
        self.active_question = ""
        self.awaiting_quiz_answer = False
        self._image_refs.clear()
        self._update_cache_display()
        self.btn_continue.pack_forget()

        for w in self._panel_names:
            was_disabled = w.cget("state") == "disabled"
            if was_disabled:
                w.config(state="normal")
            w.delete("1.0", tk.END)
            if was_disabled:
                w.config(state="disabled")

        self.output_txt.config(state="normal")
        self.output_txt.insert(
            tk.END, "Session reset. Cache and chat memory cleared.\n", "quote"
        )
        self.output_txt.config(state="disabled")
        self._save_session_state()
        self._set_status("Reset complete")

    def _save_session_state(self):
        session_file = self.workspace_dir / "session.json"
        state = {
            "cached_snippet": self.cached_snippet,
            "current_mode": self.current_mode,
            "provider": self.provider_combo.get(),
            "level_idx": self.level_combo.current(),
            "current_chat_id": self.current_chat_id,
        }
        try:
            with open(session_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    def _restore_session(self):
        session_file = self.workspace_dir / "session.json"
        if not session_file.exists():
            return

        try:
            with open(session_file, "r", encoding="utf-8") as f:
                state = json.load(f)

            snip = state.get("cached_snippet", "")
            if snip:
                self.cached_snippet = snip
                self._update_cache_display()
                self.dissect_code_txt.insert(tk.END, snip)
                self.chat_code_txt.insert(tk.END, snip)
                self.quiz_code_txt.insert(tk.END, snip)
                self.compress_raw_txt.insert(tk.END, snip)

            mode = state.get("current_mode", "dissect")
            if mode == "compress":
                self._set_mode_compress()
            elif mode == "chat":
                self._set_mode_chat()
            elif mode == "quiz":
                self._set_mode_quiz()
            else:
                self._set_mode_dissect()

            provider = state.get("provider")
            if provider in PROVIDER_CHOICES:
                self.provider_combo.set(provider)

            lvl_idx = state.get("level_idx", 0)
            if 0 <= lvl_idx < len(LEVEL_OPTIONS):
                self.level_combo.current(lvl_idx)

            chat_id = state.get("current_chat_id")
            if chat_id:
                self._load_chat_by_id(chat_id)
        except Exception:
            pass

    def _save_current_chat(self):
        if not self.chat_history:
            return

        if not self.current_chat_id:
            self.current_chat_id = datetime.datetime.now().strftime(
                "%Y%m%d_%H%M%S"
            )

        chat_file = (
            self.workspace_dir / "chats" / f"{self.current_chat_id}.json"
        )
        title = "Chat Session"
        for msg in self.chat_history:
            if msg["role"] == "user":
                content = msg["content"].split("\n")[-1][:40]
                if content:
                    title = content
                    break

        record = {
            "id": self.current_chat_id,
            "title": title,
            "snippet": self.cached_snippet,
            "history": self.chat_history,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        try:
            with open(chat_file, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2)
        except Exception:
            pass

    def _load_chat_by_id(self, chat_id: str):
        chat_file = self.workspace_dir / "chats" / f"{chat_id}.json"
        if not chat_file.exists():
            return

        try:
            with open(chat_file, "r", encoding="utf-8") as f:
                record = json.load(f)

            self.current_chat_id = chat_id
            self.chat_history = record.get("history", [])
            snip = record.get("snippet", "")
            if snip:
                self.cached_snippet = snip
                self._update_cache_display()
                self.chat_code_txt.delete("1.0", tk.END)
                self.chat_code_txt.insert(tk.END, snip)

            self.output_txt.config(state="normal")
            self.output_txt.delete("1.0", tk.END)
            for msg in self.chat_history:
                if msg["role"] == "user":
                    clean = msg["content"]
                    if "MY QUESTION:\n" in clean:
                        clean = clean.split("MY QUESTION:\n")[-1]
                    self.output_txt.insert(
                        tk.END, f"\n\nYOU: {clean}\n", "chat_user_badge"
                    )
                else:
                    self.output_txt.insert(
                        tk.END, "\n" + "─" * 60 + "\n\n", "divider"
                    )
                    render_markdown(self.output_txt, msg["content"])
            self.output_txt.config(state="disabled")
            self.output_txt.see(tk.END)
            self._set_status(f"Loaded Chat: {record.get('title')}")
        except Exception as e:
            self._handle_error(f"Failed to load chat: {e}")

    def _start_new_chat(self):
        self._save_current_chat()
        self.current_chat_id = None
        self.chat_history = []
        self.output_txt.config(state="normal")
        self.output_txt.delete("1.0", tk.END)
        self.output_txt.insert(
            tk.END,
            "Started fresh Mentor Chat. Active snippet retained.\n",
            "quote",
        )
        self.output_txt.config(state="disabled")
        self._set_status("New chat initialized")

    def _open_chat_history_modal(self):
        chats_dir = self.workspace_dir / "chats"
        files = sorted(chats_dir.glob("*.json"), reverse=True)
        if not files:
            messagebox.showinfo(
                "Chat History", "No saved conversations found in workspace."
            )
            return

        modal = tk.Toplevel(self)
        modal.title("Saved Chat History")
        modal.geometry("520x400")
        modal.configure(bg="#18181b")
        modal.transient(self)
        modal.grab_set()

        tk.Label(
            modal,
            text="Saved Mentor Sessions",
            bg="#18181b",
            fg="#e4e4e7",
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w", padx=14, pady=(12, 6))

        box_frame = tk.Frame(modal, bg="#121214")
        box_frame.pack(fill="both", expand=True, padx=14, pady=6)

        listbox = tk.Listbox(
            box_frame,
            bg="#121214",
            fg="#d4d4d8",
            selectbackground="#3b82f6",
            selectforeground="#ffffff",
            relief="flat",
            font=("Segoe UI", 10),
        )
        listbox.pack(side="left", fill="both", expand=True)

        sc = ttk.Scrollbar(
            box_frame,
            orient="vertical",
            command=listbox.yview,
            style="Dark.Vertical.TScrollbar",
        )
        sc.pack(side="right", fill="y")
        listbox.config(yscrollcommand=sc.set)

        chat_map = []
        for f in files:
            try:
                with open(f, "r", encoding="utf-8") as r:
                    d = json.load(r)
                cid = d.get("id", f.stem)
                title = d.get("title", "Untitled")
                tstamp = d.get("timestamp", "")
                listbox.insert(tk.END, f"{tstamp}  •  {title}")
                chat_map.append(cid)
            except Exception:
                pass

        btn_box = tk.Frame(modal, bg="#18181b", padx=14, pady=12)
        btn_box.pack(fill="x")

        def on_load():
            sel = listbox.curselection()
            if not sel:
                return
            chosen_id = chat_map[sel[0]]
            self._set_mode_chat()
            self._load_chat_by_id(chosen_id)
            modal.destroy()

        def on_delete():
            sel = listbox.curselection()
            if not sel:
                return
            chosen_id = chat_map[sel[0]]
            cf = chats_dir / f"{chosen_id}.json"
            if cf.exists():
                cf.unlink()
            modal.destroy()
            self._open_chat_history_modal()

        tk.Button(
            btn_box,
            text="Load Session",
            bg="#8b5cf6",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            command=on_load,
        ).pack(side="right", padx=(6, 0))

        tk.Button(
            btn_box,
            text="Delete",
            bg="#ef4444",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9),
            command=on_delete,
        ).pack(side="right")

    def _open_api_keys_modal(self):
        """Opens a modal to view, edit, and persist provider API keys to .env and active runtime."""
        import os

        env_file = BASE_DIR / ".env"

        # Read existing .env keys if present
        env_keys = {}
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env_keys[k.strip()] = v.strip().strip('"').strip("'")
            except Exception:
                pass

        key_fields = [
            ("Groq API Key", "GROQ_API_KEY"),
            ("Gemini API Key", "GEMINI_API_KEY"),
            ("OpenAI API Key", "OPENAI_API_KEY"),
            ("Anthropic / Claude Key", "ANTHROPIC_API_KEY"),
            ("OpenRouter API Key", "OPENROUTER_API_KEY"),
        ]

        modal = tk.Toplevel(self)
        modal.title("Configure API Keys")
        modal.geometry("540x430")
        modal.configure(bg="#18181b")
        modal.transient(self)
        modal.grab_set()

        tk.Label(
            modal,
            text="Provider API Keys",
            bg="#18181b",
            fg="#e4e4e7",
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w", padx=16, pady=(14, 4))

        tk.Label(
            modal,
            text="Keys are saved locally in .env and loaded immediately into the active session.",
            bg="#18181b",
            fg="#71717a",
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=16, pady=(0, 10))

        entries = {}
        container = tk.Frame(modal, bg="#18181b")
        container.pack(fill="both", expand=True, padx=16)

        for label_text, env_var in key_fields:
            row = tk.Frame(container, bg="#18181b")
            row.pack(fill="x", pady=5)

            tk.Label(
                row,
                text=label_text,
                bg="#18181b",
                fg="#a1a1aa",
                font=("Segoe UI", 9, "bold"),
                width=20,
                anchor="w",
            ).pack(side="left")

            entry = tk.Entry(
                row,
                bg="#121214",
                fg="#f4f4f5",
                insertbackground="white",
                relief="flat",
                show="*",
                font=("Consolas", 9),
            )
            # Preload from runtime environment or .env file
            current_val = os.environ.get(env_var) or env_keys.get(env_var, "")
            entry.insert(0, current_val)
            entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(4, 6))

            # Quick show/hide toggle button
            def toggle_show(e=entry):
                e.config(show="" if e.cget("show") == "*" else "*")

            t_btn = tk.Button(
                row,
                text="👁",
                bg="#27272a",
                fg="#a1a1aa",
                relief="flat",
                font=("Segoe UI", 8),
                command=toggle_show,
            )
            t_btn.pack(side="right")
            entries[env_var] = entry

        btn_bar = tk.Frame(modal, bg="#18181b", padx=16, pady=16)
        btn_bar.pack(fill="x")

        def save_keys():
            # Update running environment immediately
            for env_var, entry_widget in entries.items():
                val = entry_widget.get().strip()
                if val:
                    os.environ[env_var] = val
                    env_keys[env_var] = val
                elif env_var in os.environ and not val:
                    del os.environ[env_var]
                    env_keys.pop(env_var, None)

            # Persist to local .env
            lines = [f"{k}={v}" for k, v in env_keys.items()]
            try:
                env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
                self._set_status("API keys updated and saved to .env")
                modal.destroy()
            except Exception as e:
                messagebox.showerror("Error Saving", f"Failed to write .env: {e}", parent=modal)

        tk.Button(
            btn_bar,
            text="Save Keys",
            bg="#8b5cf6",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padx=12,
            pady=4,
            command=save_keys,
        ).pack(side="right")

        tk.Button(
            btn_bar,
            text="Cancel",
            bg="#27272a",
            fg="#a1a1aa",
            relief="flat",
            font=("Segoe UI", 9),
            padx=10,
            pady=4,
            command=modal.destroy,
        ).pack(side="right", padx=(0, 8))

    def _choose_workspace_dir(self):
        chosen = filedialog.askdirectory(
            initialdir=self.workspace_dir,
            title="Select Local Workspace Cache Directory",
        )
        if chosen:
            self.workspace_dir = Path(chosen)
            self._init_workspace_dirs()
            self._save_workspace_settings()
            self._set_status(f"Workspace set: {self.workspace_dir.name}")

    def _import_from_github(self):
        """Imports either a single file OR an entire repository into workspace_data/repos/

        and launches the interactive Project Explorer.
        """
        url = simpledialog.askstring(
            "Import from GitHub",
            "Paste GitHub Repository or File URL:\n\n"
            "• Entire Project:  https://github.com/owner/repo\n"
            "• Single File:     https://github.com/owner/repo/blob/main/file.py",
            parent=self,
        )
        if not url:
            return

        url = url.strip()

        # Check if the URL points to a specific single file
        code_exts = (".py", ".js", ".ts", ".html", ".css", ".json", ".sql", ".rs", ".go", ".c", ".cpp", ".java", ".sh")
        is_single_file = "/blob/" in url or "/raw/" in url or any(url.endswith(ext) for ext in code_exts)

        if is_single_file:
            self._import_single_github_file(url)
        else:
            self._import_github_repository(url)

    def _import_single_github_file(self, url: str):
        raw_url = url
        if "github.com" in url and "/blob/" in url:
            raw_url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        elif "github.com" in url and "/raw/" in url:
            raw_url = url.replace("github.com", "raw.githubusercontent.com").replace("/raw/", "/")

        self._set_status("Fetching single GitHub file...")

        def worker():
            try:
                req = urllib.request.Request(raw_url, headers={"User-Agent": "SelfLearner/1.0"})
                with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
                    code_text = resp.read().decode("utf-8", errors="replace")

                def update_ui():
                    self._load_code_into_active_mode(code_text)
                    self._set_status(f"Imported single file ({len(code_text.splitlines())} lines).")

                self.after(0, update_ui)
            except Exception as e:
                self.after(0, lambda: self._handle_error(f"GitHub Single File Import Failed:\n{e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _import_github_repository(self, url: str):
        match = re.search(r"github\.com/([^/]+)/([^/]+)", url)
        if not match:
            self._display_error("Invalid GitHub URL format. Use https://github.com/owner/repo")
            return

        owner = match.group(1)
        repo = match.group(2).replace(".git", "")
        repo_name = f"{owner}_{repo}"
        repo_dir = self.workspace_dir / "repos" / repo_name

        self._set_status(f"Downloading {owner}/{repo} repository archive...")

        def worker():
            try:
                # Download full repository zipball (auto-resolves default branch)
                zip_urls = [
                    f"https://api.github.com/repos/{owner}/{repo}/zipball",
                    f"https://github.com/{owner}/{repo}/archive/HEAD.zip",
                ]

                zip_data = None
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SelfLearner/1.0"}
                ssl_ctx = get_ssl_context()

                for z_url in zip_urls:
                    try:
                        req = urllib.request.Request(z_url, headers=headers)
                        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
                            zip_data = resp.read()
                            if zip_data.startswith(b"PK"):
                                break
                    except Exception:
                        continue

                if not zip_data or not zip_data.startswith(b"PK"):
                    raise RuntimeError("Could not download valid repository archive. Verify repo is public.")

                # Extract and strip GitHub's root hash directory
                repo_dir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
                    namelist = z.namelist()
                    root_prefix = namelist[0].split("/")[0] if namelist else ""
                    for member in z.infolist():
                        rel = member.filename
                        if root_prefix and rel.startswith(root_prefix + "/"):
                            rel = rel[len(root_prefix) + 1:]
                        if not rel:
                            continue
                        target = repo_dir / rel
                        if member.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with z.open(member) as src, open(target, "wb") as dst:
                                dst.write(src.read())

                self.after(0, lambda: self._open_repo_explorer_modal(repo_dir, f"{owner}/{repo}"))
                self.after(0, lambda: self._set_status(f"Project loaded: {owner}/{repo}"))
            except Exception as e:
                self.after(0, lambda: self._handle_error(f"Repository Clone Failed:\n{e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _open_repo_explorer_modal(self, repo_dir: Path, repo_title: str):
        """Displays an interactive Project File Tree with syntax preview,

        single-file loading, and multi-file cross-module bundling.
        """
        modal = tk.Toplevel(self)
        modal.title(f"Project Explorer — {repo_title}")
        modal.geometry("960x640")
        modal.configure(bg="#18181b")
        modal.transient(self)
        modal.grab_set()

        top_bar = tk.Frame(modal, bg="#27272a", padx=14, pady=8)
        top_bar.pack(fill="x")

        tk.Label(
            top_bar,
            text=f"📂 Project: {repo_title}",
            bg="#27272a",
            fg="#38bdf8",
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left")

        self.explorer_path_lbl = tk.Label(
            top_bar,
            text="Select a file to preview",
            bg="#27272a",
            fg="#a1a1aa",
            font=("Segoe UI", 9),
        )
        self.explorer_path_lbl.pack(side="right")

        # Split pane: Treeview (left) | Code Preview (right)
        paned = tk.PanedWindow(modal, orient=tk.HORIZONTAL, bg="#3f3f46", sashwidth=5, bd=0)
        paned.pack(fill="both", expand=True, padx=10, pady=8)

        tree_frame = tk.Frame(paned, bg="#121214")
        paned.add(tree_frame, minsize=260)

        tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse", style="Treeview")
        tree.pack(side="left", fill="both", expand=True)

        tree_sc = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview, style="Dark.Vertical.TScrollbar")
        tree_sc.pack(side="right", fill="y")
        tree.config(yscrollcommand=tree_sc.set)

        preview_frame = tk.Frame(paned, bg="#121214")
        paned.add(preview_frame, minsize=400)

        preview_txt = tk.Text(
            preview_frame,
            bg="#121214",
            fg="#f4f4f5",
            insertbackground="#121214",
            relief="flat",
            wrap="none",
            font=("Consolas", 10),
            padx=10,
            pady=10,
        )
        preview_txt.pack(side="left", fill="both", expand=True)
        make_read_only(preview_txt)

        preview_sc = ttk.Scrollbar(preview_frame, orient="vertical", command=preview_txt.yview, style="Dark.Vertical.TScrollbar")
        preview_sc.pack(side="right", fill="y")
        preview_txt.config(yscrollcommand=preview_sc.set)

        # Build folder structure in Treeview
        tree_nodes = {}
        ignored_dirs = {".git", "__pycache__", ".venv", "venv", "node_modules", ".idea", ".vscode", "dist", "build"}
        valid_exts = {".py", ".js", ".ts", ".html", ".css", ".json", ".sql", ".rs", ".go", ".c", ".cpp", ".h", ".java", ".sh", ".md", ".toml", ".yml", ".yaml"}

        for p in sorted(repo_dir.rglob("*")):
            if any(part in ignored_dirs for part in p.parts):
                continue

            rel = p.relative_to(repo_dir)
            parent_id = str(rel.parent) if rel.parent != Path(".") else ""

            if p.is_dir():
                node_id = str(rel)
                tree_nodes[node_id] = p
                tree.insert(parent_id, "end", node_id, text=f"📁 {p.name}", open=False)
            elif p.is_file() and p.suffix.lower() in valid_exts:
                node_id = str(rel)
                tree_nodes[node_id] = p
                tree.insert(parent_id, "end", node_id, text=f"📄 {p.name}")

        selected_file_content = [""]

        def on_select(event):
            selected = tree.selection()
            if not selected:
                return
            item_path = tree_nodes.get(selected[0])
            if item_path and item_path.is_file():
                try:
                    content = item_path.read_text(encoding="utf-8", errors="replace")
                    selected_file_content[0] = content
                    lines = len(content.splitlines())
                    self.explorer_path_lbl.config(text=f"{item_path.relative_to(repo_dir)} ({lines} lines)")

                    preview_txt.config(state="normal")
                    preview_txt.delete("1.0", tk.END)
                    preview_txt.insert(tk.END, content)
                    preview_txt.config(state="disabled")
                except Exception as e:
                    self.explorer_path_lbl.config(text=f"Error reading file: {e}")

        tree.bind("<<TreeviewSelect>>", on_select)

        # Bottom action bar
        btn_bar = tk.Frame(modal, bg="#18181b", padx=10, pady=10)
        btn_bar.pack(fill="x")

        def load_current_file():
            if selected_file_content[0]:
                self._load_code_into_active_mode(selected_file_content[0])
                modal.destroy()
            else:
                messagebox.showwarning("Select File", "Please select a file from the tree view first.", parent=modal)

        def bundle_project_core():
            """Gathers all primary code files in the repo into tagged blocks

            for architectural and dataflow analysis across modules.
            """
            code_files = [
                p for p in sorted(repo_dir.rglob("*.py"))
                if not any(part in ignored_dirs for part in p.parts)
            ]
            if not code_files:
                code_files = [
                    p for p in sorted(repo_dir.rglob("*"))
                    if p.is_file() and p.suffix.lower() in valid_exts and not any(part in ignored_dirs for part in p.parts)
                ]

            bundle_parts = []
            for cf in code_files[:8]:  # Limit to 8 core modules to stay within context
                try:
                    text = cf.read_text(encoding="utf-8", errors="replace").strip()
                    if text:
                        rel = cf.relative_to(repo_dir)
                        bundle_parts.append(f"# ===== MODULE: {rel} =====\n{text}")
                except Exception:
                    pass

            if bundle_parts:
                composite = "\n\n".join(bundle_parts)
                self._load_code_into_active_mode(composite)
                self._set_status(f"Bundled {len(bundle_parts)} modules into workspace.")
                modal.destroy()

        tk.Button(
            btn_bar,
            text="📥 Load Selected File",
            bg="#2563eb",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padx=12,
            pady=5,
            command=load_current_file,
        ).pack(side="left")

        tk.Button(
            btn_bar,
            text="📦 Bundle Project Architecture",
            bg="#10b981",
            fg="white",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padx=12,
            pady=5,
            command=bundle_project_core,
        ).pack(side="left", padx=8)

        tk.Button(
            btn_bar,
            text="Close",
            bg="#27272a",
            fg="#a1a1aa",
            relief="flat",
            font=("Segoe UI", 9),
            padx=10,
            pady=5,
            command=modal.destroy,
        ).pack(side="right")

    def _load_code_into_active_mode(self, code_text: str):
        """Helper to inject loaded code into whatever tab is currently active."""
        self.cached_snippet = code_text
        self._update_cache_display()

        if self.current_mode == "dissect":
            self.dissect_code_txt.delete("1.0", tk.END)
            self.dissect_code_txt.insert(tk.END, code_text)
        elif self.current_mode == "compress":
            self.compress_raw_txt.delete("1.0", tk.END)
            self.compress_raw_txt.insert(tk.END, code_text)
        elif self.current_mode == "chat":
            self.chat_code_txt.delete("1.0", tk.END)
            self.chat_code_txt.insert(tk.END, code_text)
        elif self.current_mode == "quiz":
            self.quiz_code_txt.delete("1.0", tk.END)
            self.quiz_code_txt.insert(tk.END, code_text)

        self._save_session_state()

    def _copy_compressed_code(self):
        code = self.compress_code_txt.get("1.0", tk.END).strip()
        if code:
            self.clipboard_clear()
            self.clipboard_append(code)
            self._set_status("Compressed code copied to clipboard!")

    def _get_selected_provider(self) -> str:
        choice = self.provider_combo.get()
        return PROVIDER_CHOICES.get(choice, "auto")

    def _set_mode_dissect(self):
        self._stop_autoscroll()
        self._sync_active_snippet_from_ui()
        self.current_mode = "dissect"
        self._update_nav_buttons()

        self.paned_compress.pack_forget()
        self.paned_standard.pack(fill="both", expand=True, padx=8, pady=8)

        self.chat_left_bar.pack_forget()
        self.quiz_left_bar.pack_forget()
        self.dissect_left_bar.pack(fill="x")

        self.chat_right_frame.pack_forget()
        self.quiz_right_frame.pack_forget()
        self.dissect_right_frame.pack(fill="both", expand=True)

        if (
            self.cached_snippet
            and not self.dissect_code_txt.get("1.0", tk.END).strip()
        ):
            self.dissect_code_txt.insert(tk.END, self.cached_snippet)
        self._save_session_state()

    def _set_mode_compress(self):
        self._stop_autoscroll()
        self._sync_active_snippet_from_ui()
        self.current_mode = "compress"
        self._update_nav_buttons()

        self.paned_standard.pack_forget()
        self.paned_compress.pack(fill="both", expand=True, padx=8, pady=8)

        if (
            self.cached_snippet
            and not self.compress_raw_txt.get("1.0", tk.END).strip()
        ):
            self.compress_raw_txt.insert(tk.END, self.cached_snippet)
        self._save_session_state()

    def _set_mode_chat(self):
        self._stop_autoscroll()
        self._sync_active_snippet_from_ui()
        self.current_mode = "chat"
        self._update_nav_buttons()

        self.paned_compress.pack_forget()
        self.paned_standard.pack(fill="both", expand=True, padx=8, pady=8)

        self.dissect_left_bar.pack_forget()
        self.quiz_left_bar.pack_forget()
        self.chat_left_bar.pack(fill="x")

        self.dissect_right_frame.pack_forget()
        self.quiz_right_frame.pack_forget()
        self.chat_right_frame.pack(fill="both", expand=True)

        if (
            self.cached_snippet
            and not self.chat_code_txt.get("1.0", tk.END).strip()
        ):
            self.chat_code_txt.insert(tk.END, self.cached_snippet)
        self._save_session_state()

    def _set_mode_quiz(self):
        self._stop_autoscroll()
        self._sync_active_snippet_from_ui()
        self.current_mode = "quiz"
        self._update_nav_buttons()

        self.paned_compress.pack_forget()
        self.paned_standard.pack(fill="both", expand=True, padx=8, pady=8)

        self.dissect_left_bar.pack_forget()
        self.chat_left_bar.pack_forget()
        self.quiz_left_bar.pack(fill="x")

        self.dissect_right_frame.pack_forget()
        self.chat_right_frame.pack_forget()
        self.quiz_right_frame.pack(fill="both", expand=True)

        if (
            self.cached_snippet
            and not self.quiz_code_txt.get("1.0", tk.END).strip()
        ):
            self.quiz_code_txt.insert(tk.END, self.cached_snippet)
        self._save_session_state()

    def _update_nav_buttons(self):
        modes = [
            ("dissect", self.btn_dissect, "#3b82f6"),
            ("compress", self.btn_compress, "#e11d48"),
            ("chat", self.btn_chat, "#8b5cf6"),
            ("quiz", self.btn_quiz, "#10b981"),
        ]
        for name, btn, active_color in modes:
            if self.current_mode == name:
                btn.config(bg=active_color, fg="white")
            else:
                btn.config(bg="#3f3f46", fg="#d4d4d8")

    def _set_status(self, text: str):
        self.status_lbl.config(text=text)

    def _display_formatted_response(
        self, raw_markdown: str, meta: dict, append: bool = False
    ):
        self.output_txt.config(state="normal")
        if not append:
            self.output_txt.delete("1.0", tk.END)
        else:
            self.output_txt.insert(tk.END, "\n" + "─" * 60 + "\n\n", "divider")

        provider = meta.get("provider") or "unknown"
        model = meta.get("model") or "custom"
        chain = meta.get("chain", [])

        badge_text = f" MODEL SELECTED: {provider.upper()} // {model} "
        self.output_txt.insert(tk.END, badge_text, "meta_badge")
        self.output_txt.insert(tk.END, "\n")

        if chain:
            for p, err in chain:
                self.output_txt.insert(
                    tk.END, f"  ↳ fallback: {p} unavailable ({err})\n", "meta_trail"
                )

        self.output_txt.insert(tk.END, "─" * 60 + "\n", "divider")
        render_markdown(self.output_txt, raw_markdown)

        if self.current_mode == "dissect":
            self.output_txt.insert(tk.END, "\n\n")
            btn_diag = tk.Button(
                self.output_txt,
                text="📊 Generate Dataflow Diagram (Mermaid)",
                bg="#1e293b",
                fg="#38bdf8",
                activebackground="#3b82f6",
                activeforeground="#ffffff",
                relief="flat",
                font=("Segoe UI", 9, "bold"),
                padx=10,
                pady=5,
                cursor="hand2",
            )
            btn_diag.config(
                command=lambda b=btn_diag: self._trigger_generate_diagram(b)
            )
            self.output_txt.window_create(tk.END, window=btn_diag)
            self.output_txt.insert(tk.END, "\n\n")

        if meta.get("truncated"):
            self.output_txt.insert(
                tk.END,
                "\n\n[⚠️ Note: Output was truncated by the model's token limit. Click 'Continue Output' above to proceed.]\n",
                "meta_trail",
            )
            self.btn_continue.pack(side="left", padx=8)
        else:
            self.btn_continue.pack_forget()

        self.output_txt.config(state="disabled")
        self.output_txt.see(tk.END)
        self._set_status(f"Resolved: {provider} ({model})")

    def _on_action_click(self):
        self._stop_autoscroll()
        if self.current_mode == "dissect":
            text = self.dissect_code_txt.get("1.0", tk.END).strip()
            if text:
                self.cached_snippet = text
                self._update_cache_display()
            if not self.cached_snippet:
                self._display_error("Paste code in the right pane to dissect.")
                return
            self._trigger_dissect(self.cached_snippet)

        elif self.current_mode == "compress":
            text = self.compress_raw_txt.get("1.0", tk.END).strip()
            if text:
                self.cached_snippet = text
                self._update_cache_display()
            if not self.cached_snippet:
                self._display_error("Paste code in the left pane to compress.")
                return
            self._trigger_compress(self.cached_snippet)

        elif self.current_mode == "chat":
            self._trigger_chat_send()

        elif self.current_mode == "quiz":
            if self.awaiting_quiz_answer:
                ans = self.quiz_answer_txt.get("1.0", tk.END).strip()
                if ans:
                    self._trigger_evaluate(ans)
            else:
                text = self.quiz_code_txt.get("1.0", tk.END).strip()
                if text:
                    self.cached_snippet = text
                    self._update_cache_display()
                self._trigger_generate_quiz()

    def _trigger_dissect(self, code: str):
        mode_idx = self.focus_mode_combo.current()
        mode_keys = ["all", "mechanics", "fixes", "gotchas"]
        selected_mode = (
            mode_keys[mode_idx] if 0 <= mode_idx < len(mode_keys) else "all"
        )
        provider = self._get_selected_provider()

        self.btn_run_dissect.config(state="disabled")
        self._set_status(f"Dissecting via {provider}...")
        self.output_txt.config(state="normal")
        self.output_txt.delete("1.0", tk.END)
        self.output_txt.insert(tk.END, "Analyzing code mechanics...\n", "quote")
        self.output_txt.config(state="disabled")

        def worker():
            try:
                res = analyze_code(code, mode=selected_mode, provider=provider)
                meta = get_last_call_info()
                self.after(
                    0,
                    lambda: self._display_formatted_response(
                        res, meta, append=False
                    ),
                )
            except Exception as e:
                self.after(0, lambda: self._handle_error(str(e)))
            finally:
                self.after(
                    0, lambda: self.btn_run_dissect.config(state="normal")
                )

        threading.Thread(target=worker, daemon=True).start()

    def _trigger_generate_diagram(self, button_widget: tk.Button):
        if not self.cached_snippet:
            return

        provider = self._get_selected_provider()
        button_widget.config(
            text="⏳ Rendering Mermaid Diagram...",
            state="disabled",
            bg="#27272a",
            fg="#a1a1aa",
        )
        self._set_status("Synthesizing architecture diagram...")

        def worker():
            try:
                diagram_res = generate_dataflow_diagram(
                    self.cached_snippet, provider=provider
                )

                mermaid_code = ""
                if "```mermaid" in diagram_res:
                    mermaid_code = (
                        diagram_res.split("```mermaid", 1)[1]
                        .split("```", 1)[0]
                        .strip()
                    )
                elif "```" in diagram_res:
                    mermaid_code = (
                        diagram_res.split("```", 1)[1]
                        .split("```", 1)[0]
                        .strip()
                    )
                else:
                    mermaid_code = diagram_res.strip()

                clean_mermaid = sanitize_mermaid(mermaid_code)
                png_bytes, local_svg_file, local_bridge_file = fetch_diagram_data(
                    clean_mermaid, self.workspace_dir
                )

                def ui_render():
                    self.output_txt.config(state="normal")
                    self.output_txt.insert(
                        tk.END, "\n### ARCHITECTURAL DATAFLOW\n\n", "h3"
                    )

                    rendered_image = False
                    if png_bytes:
                        try:
                            if HAS_PIL:
                                img = Image.open(io.BytesIO(png_bytes))
                                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                                    bg_img = Image.new("RGBA", img.size, (24, 24, 27, 255))
                                    bg_img.paste(img, (0, 0), img.convert("RGBA"))
                                    img = bg_img.convert("RGB")

                                max_w = min(560, max(300, self.output_txt.winfo_width() - 40))
                                if img.width > max_w:
                                    ratio = max_w / float(img.width)
                                    new_h = int(float(img.height) * ratio)
                                    img = img.resize((max_w, new_h), Image.Resampling.LANCZOS)
                                photo = ImageTk.PhotoImage(img)
                            else:
                                photo = tk.PhotoImage(data=base64.b64encode(png_bytes))

                            self._image_refs.append(photo)
                            self.output_txt.image_create(tk.END, image=photo)
                            self.output_txt.insert(tk.END, "\n\n")
                            rendered_image = True
                        except Exception:
                            rendered_image = False

                    # Button: Open in Mermaid Live Editor via redirect bridge
                    btn_open_live = tk.Button(
                        self.output_txt,
                        text="🔍 Open in Mermaid Live Editor",
                        bg="#0f172a",
                        fg="#38bdf8",
                        activebackground="#1e293b",
                        activeforeground="#ffffff",
                        relief="flat",
                        font=("Segoe UI", 9, "bold"),
                        padx=8,
                        pady=4,
                        cursor="hand2",
                        command=lambda f=local_bridge_file: webbrowser.open(f.as_uri()),
                    )
                    self.output_txt.window_create(tk.END, window=btn_open_live)

                    # Button: Open raw vector SVG
                    btn_open_svg = tk.Button(
                        self.output_txt,
                        text="🌐 Open Vector SVG",
                        bg="#18181b",
                        fg="#a1a1aa",
                        activebackground="#27272a",
                        activeforeground="#ffffff",
                        relief="flat",
                        font=("Segoe UI", 9),
                        padx=8,
                        pady=4,
                        cursor="hand2",
                        command=lambda f=local_svg_file: webbrowser.open(f.as_uri()),
                    )
                    self.output_txt.insert(tk.END, "  ")
                    self.output_txt.window_create(tk.END, window=btn_open_svg)
                    self.output_txt.insert(tk.END, "\n\n")

                    if not rendered_image:
                        render_markdown(
                            self.output_txt,
                            f"```mermaid\n{clean_mermaid}\n```\n",
                        )

                    if "```" in diagram_res:
                        caption_parts = diagram_res.split("```")
                        if len(caption_parts) >= 3 and caption_parts[2].strip():
                            render_markdown(
                                self.output_txt,
                                f"> {caption_parts[2].strip()}\n\n",
                            )

                    button_widget.config(
                        text="✓ Diagram Rendered",
                        bg="#065f46",
                        fg="#6ee7b7",
                        state="disabled",
                    )

                    self.output_txt.config(state="disabled")
                    self.output_txt.see(tk.END)
                    self._set_status(f"Dataflow generated via {provider}")

                self.after(0, ui_render)
            except Exception as e:
                def ui_fail(err_msg=str(e)):
                    button_widget.config(
                        text="⚠️ Diagram Generation Failed",
                        state="normal",
                        bg="#ef4444",
                        fg="#ffffff",
                    )
                    self._set_status(f"Diagram error: {err_msg}")
                    self.output_txt.config(state="normal")
                    self.output_txt.insert(
                        tk.END,
                        f"\n[⚠️ Diagram error: {err_msg}]\n",
                        "meta_trail",
                    )
                    self.output_txt.config(state="disabled")
                    self.output_txt.see(tk.END)

                self.after(0, ui_fail)

        threading.Thread(target=worker, daemon=True).start()

    def _trigger_compress(self, code: str):
        provider = self._get_selected_provider()
        self.btn_run_compress.config(state="disabled")
        self._set_status(f"Compressing via {provider}...")

        self.compress_audit_txt.config(state="normal")
        self.compress_audit_txt.delete("1.0", tk.END)
        self.compress_audit_txt.insert(
            tk.END, "Auditing transformations...\n", "quote"
        )
        self.compress_audit_txt.config(state="disabled")

        self.compress_code_txt.config(state="normal")
        self.compress_code_txt.delete("1.0", tk.END)
        self.compress_code_txt.config(state="disabled")

        def worker():
            try:
                audit, comp_code = compress_code(code, provider=provider)
                meta = get_last_call_info()

                def ui():
                    self.compress_audit_txt.config(state="normal")
                    self.compress_audit_txt.delete("1.0", tk.END)
                    render_markdown(self.compress_audit_txt, audit)
                    if meta.get("truncated"):
                        self.compress_audit_txt.insert(
                            tk.END,
                            "\n\n[⚠️ Note: Truncated. Click 'Continue Output' to resume.]\n",
                            "meta_trail",
                        )
                        self.btn_continue.pack(side="left", padx=8)
                    else:
                        self.btn_continue.pack_forget()
                    self.compress_audit_txt.config(state="disabled")

                    self.compress_code_txt.config(state="normal")
                    self.compress_code_txt.delete("1.0", tk.END)
                    self.compress_code_txt.insert(tk.END, comp_code)
                    self.compress_code_txt.config(state="disabled")
                    self._set_status(f"Compressed via {provider}")

                self.after(0, ui)
            except Exception as e:
                self.after(0, lambda: self._handle_error(str(e)))
            finally:
                self.after(
                    0, lambda: self.btn_run_compress.config(state="normal")
                )

        threading.Thread(target=worker, daemon=True).start()

    def _trigger_continue(self):
        provider = self._get_selected_provider()
        self.btn_continue.config(state="disabled")
        self._set_status("Continuing stream...")

        def worker():
            try:
                continued_chunk = call_continue(provider=provider)
                meta = get_last_call_info()

                def ui():
                    if self.current_mode == "compress":
                        self.compress_audit_txt.config(state="normal")
                        self.compress_audit_txt.insert(
                            tk.END, "\n" + continued_chunk
                        )
                        if not meta.get("truncated"):
                            self.btn_continue.pack_forget()
                        self.compress_audit_txt.config(state="disabled")
                    else:
                        self.output_txt.config(state="normal")
                        self.output_txt.insert(tk.END, "\n" + continued_chunk)
                        if not meta.get("truncated"):
                            self.btn_continue.pack_forget()
                        self.output_txt.config(state="disabled")

                    self._set_status("Continued response appended.")

                self.after(0, ui)
            except Exception as e:
                self.after(0, lambda: self._handle_error(str(e)))
            finally:
                self.after(0, lambda: self.btn_continue.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _trigger_chat_send(self):
        code = self.chat_code_txt.get("1.0", tk.END).strip()
        query = self.chat_query_txt.get("1.0", tk.END).strip()

        if code:
            self.cached_snippet = code
            self._update_cache_display()

        active_code = self.cached_snippet
        if not active_code and not query:
            self._display_error(
                "Provide code or enter a question to start chatting."
            )
            return

        provider = self._get_selected_provider()

        if len(self.chat_history) == 0:
            user_msg = (
                "You are an experienced, peer-level software engineer and mentor pair programming with me. "
                "Explain code like an insightful peer: direct, practical, and grounded in concrete runtime state "
                "(e.g., 'Suppose X is... then Y becomes...'). Avoid robotic fluff.\n\n"
                f"--- CODE SNIPPET ---\n{active_code}\n--- END CODE ---\n\n"
                f"MY QUESTION:\n{query or 'Walk me through what this code is doing and why.'}"
            )
            display_user_query = (
                query or "Walk me through what this code is doing and why."
            )
        else:
            if code and code != self.cached_snippet:
                user_msg = f"[Updated Code Snippet]:\n```python\n{code}\n```\n\n{query}"
            else:
                user_msg = query
            display_user_query = query

        self.chat_history.append({"role": "user", "content": user_msg})

        self.output_txt.config(state="normal")
        self.output_txt.insert(
            tk.END, f"\n\nYOU: {display_user_query}\n", "chat_user_badge"
        )
        self.output_txt.insert(tk.END, "Thinking...\n", "quote")
        self.output_txt.config(state="disabled")
        self.output_txt.see(tk.END)

        self.chat_query_txt.delete("1.0", tk.END)
        self.btn_chat_send.config(state="disabled")
        self._set_status(f"Talking to {provider}...")

        def worker():
            try:
                response = call_llm(self.chat_history, provider=provider)
                self.chat_history.append(
                    {"role": "assistant", "content": response}
                )
                self._save_current_chat()
                meta = get_last_call_info()

                def ui_update():
                    self.output_txt.config(state="normal")
                    idx = self.output_txt.search("Thinking...\n", "1.0", tk.END)
                    if idx:
                        self.output_txt.delete(idx, f"{idx}+12c")
                    self.output_txt.config(state="disabled")
                    self._display_formatted_response(
                        response, meta, append=True
                    )

                self.after(0, ui_update)
            except Exception as e:
                self.after(0, lambda: self._handle_error(str(e)))
            finally:
                self.after(0, lambda: self.btn_chat_send.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _trigger_generate_quiz(self):
        snippet_input = self.quiz_code_txt.get("1.0", tk.END).strip()
        if snippet_input:
            self.cached_snippet = snippet_input
            self._update_cache_display()

        snippet = self.cached_snippet
        if not snippet:
            self._display_error("Paste code snippet to generate a challenge.")
            return

        level = self.level_combo.current() + 1
        provider = self._get_selected_provider()

        self.active_level = level
        self.btn_quiz_action.config(state="disabled")
        self._set_status(f"Generating Level {level} challenge...")

        self.output_txt.config(state="normal")
        self.output_txt.delete("1.0", tk.END)
        self.output_txt.insert(
            tk.END,
            f"Designing grounded Level {level} challenge from snippet...\n",
            "quote",
        )
        self.output_txt.config(state="disabled")

        def worker():
            try:
                data = generate_question_from_snippet(
                    snippet, level=level, provider=provider
                )
                self.active_concept_name = data["concept_name"]
                self.active_question = data["question"]
                meta = get_last_call_info()

                preview = (
                    f"### TACTICAL CHALLENGE: {self.active_concept_name} (Level {level}/6)\n\n"
                    f"{self.active_question}\n\n---\n"
                    f"*Type your answer in the right box and click Submit (Ctrl+Enter).*"
                )

                def ui_update():
                    self._display_formatted_response(preview, meta, append=False)
                    self.awaiting_quiz_answer = True
                    self.quiz_mid_lbl.config(
                        text="TYPE YOUR ANSWER BELOW (INPUT):"
                    )
                    self.btn_quiz_action.config(
                        text="Submit", bg="#10b981", state="normal"
                    )
                    self.quiz_answer_txt.delete("1.0", tk.END)

                self.after(0, ui_update)
            except Exception as e:
                self.after(0, lambda: self._handle_error(str(e)))
                self.after(
                    0, lambda: self.btn_quiz_action.config(state="normal")
                )

        threading.Thread(target=worker, daemon=True).start()

    def _trigger_evaluate(self, user_answer: str):
        if not self.active_question or not self.awaiting_quiz_answer:
            return

        provider = self._get_selected_provider()
        self.btn_quiz_action.config(state="disabled")
        self.btn_quiz_submit.config(state="disabled")
        self._set_status("Grading answer...")

        def worker():
            try:
                res = evaluate_answer(
                    self.active_concept_name,
                    self.active_level,
                    self.cached_snippet,
                    self.active_question,
                    user_answer,
                    provider=provider,
                )
                meta = get_last_call_info()
                verdict = res.get("verdict", "FAIL")
                critique = res.get("engine_postmortem", "")
                ideal = res.get("ideal_answer", "")

                output = (
                    f"### VERDICT: {verdict}\n\n"
                    f"### ENGINE POST-MORTEM\n{critique}\n\n"
                    f"### IDEAL ANSWER\n{ideal}\n"
                )

                if verdict == "PASS":
                    save_concept_level(
                        self.active_concept_name, self.active_level
                    )
                    output += (
                        f"\n---\n**Mastery Logged**: `{self.active_concept_name}` "
                        f"verified at Level {self.active_level}/6 in `concepts.json`!\n"
                    )

                def ui_update():
                    self._display_formatted_response(output, meta, append=False)
                    self.awaiting_quiz_answer = False
                    self.quiz_mid_lbl.config(
                        text="YOUR ANSWER (Submit after generating challenge):"
                    )
                    self.btn_quiz_action.config(
                        text="Generate", bg="#10b981", state="normal"
                    )
                    self.btn_quiz_submit.config(state="normal")
                    self.quiz_answer_txt.delete("1.0", tk.END)

                self.after(0, ui_update)
            except Exception as e:
                self.after(0, lambda: self._handle_error(str(e)))
                self.after(
                    0, lambda: self.btn_quiz_action.config(state="normal")
                )
                self.after(
                    0, lambda: self.btn_quiz_submit.config(state="normal")
                )

        threading.Thread(target=worker, daemon=True).start()

    def _display_error(self, message: str):
        self.output_txt.config(state="normal")
        self.output_txt.delete("1.0", tk.END)
        self.output_txt.insert(tk.END, f"[!] {message}\n", "meta_trail")
        self.output_txt.config(state="disabled")

    def _handle_error(self, err_msg: str):
        self.output_txt.config(state="normal")
        self.output_txt.delete("1.0", tk.END)
        self.output_txt.insert(tk.END, f"[!] Error:\n{err_msg}\n", "meta_trail")
        self.output_txt.config(state="disabled")
        self._set_status("Failed")


if __name__ == "__main__":
    app = LearnerApp()
    app.mainloop()