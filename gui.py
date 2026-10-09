import json
from pathlib import Path
import re
import sys
import textwrap
import threading
import tkinter as tk
from tkinter import ttk

from analyzer import analyze_code
from providers import call_llm, get_last_call_info
from quiz import evaluate_answer, generate_question_from_snippet

BASE_DIR = (
    Path(sys.executable).parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
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

    def render_table(table_lines: list):
        rows = []
        for line in table_lines:
            clean_test = (
                line.replace("|", "")
                .replace("-", "")
                .replace(":", "")
                .replace(" ", "")
            )
            if not clean_test:
                continue
            cells = [c.strip() for c in line.strip().split("|")]
            if line.strip().startswith("|") and len(cells) > 1:
                cells = cells[1:]
            if line.strip().endswith("|") and len(cells) > 0 and cells[-1] == "":
                cells = cells[:-1]
            rows.append([c.replace("**", "").replace("`", "") for c in cells])

        if not rows:
            return

        n_cols = max(len(r) for r in rows)
        for r in rows:
            while len(r) < n_cols:
                r.append("")

        col_lens = [
            max((len(rows[r][c]) for r in range(len(rows))), default=8)
            for c in range(n_cols)
        ]
        budget = max(24, 60 - (3 * n_cols + 1))
        weight_sum = sum(max(1, l) for l in col_lens) or 1
        widths = [
            max(8, int(budget * (max(1, l) / weight_sum))) for l in col_lens
        ]

        while sum(widths) > budget:
            idx = widths.index(max(widths))
            if widths[idx] <= 8:
                break
            widths[idx] -= 1

        top_b = "┌" + "┬".join("─" * (w + 2) for w in widths) + "┐\n"
        mid_b = "├" + "┼".join("─" * (w + 2) for w in widths) + "┤\n"
        bot_b = "└" + "┴".join("─" * (w + 2) for w in widths) + "┘\n"

        widget.insert(tk.END, "\n" + top_b, "table_border")

        for r_idx, row in enumerate(rows):
            wrapped_cols = [
                textwrap.wrap(row[c], width=widths[c]) or [""]
                for c in range(n_cols)
            ]
            max_lines = max(len(w) for w in wrapped_cols)
            tag = "table_header" if r_idx == 0 else "table_body"

            for l_idx in range(max_lines):
                widget.insert(tk.END, "│", "table_border")
                for c in range(n_cols):
                    cell_text = (
                        wrapped_cols[c][l_idx]
                        if l_idx < len(wrapped_cols[c])
                        else ""
                    )
                    widget.insert(tk.END, f" {cell_text:<{widths[c]}} ", tag)
                    widget.insert(tk.END, "│", "table_border")
                widget.insert(tk.END, "\n")

            if r_idx == 0 and len(rows) > 1:
                widget.insert(tk.END, mid_b, "table_border")

        widget.insert(tk.END, bot_b + "\n", "table_border")

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

        if line.strip().startswith("|") and line.strip().endswith("|"):
            tbl_lines = []
            while (
                i < len(lines)
                and lines[i].strip().startswith("|")
                and lines[i].strip().endswith("|")
            ):
                tbl_lines.append(lines[i].strip())
                i += 1
            render_table(tbl_lines)
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


class LearnerApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Self Learner")
        self.geometry("1080x880")
        self.configure(bg="#18181b")

        # Persistent Cache
        self.cached_snippet = ""
        self.chat_history = []

        # Quiz State
        self.active_concept_name = ""
        self.active_level = 1
        self.active_question = ""
        self.awaiting_quiz_answer = False

        self._build_ui()
        self._show_welcome_instructions()
        self.after(80, self._init_sash_50_50)

    def _build_ui(self):
        # 1. Top Navigation Bar
        nav_frame = tk.Frame(self, bg="#27272a", padx=12, pady=10)
        nav_frame.pack(fill="x")

        self.btn_dissect = tk.Button(
            nav_frame,
            text="[ Mode: Dissector ]",
            bg="#3b82f6",
            fg="white",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            command=self._set_mode_dissect,
        )
        self.btn_dissect.pack(side="left", padx=4)

        self.btn_chat = tk.Button(
            nav_frame,
            text="[ Mode: Mentor Chat ]",
            bg="#3f3f46",
            fg="#d4d4d8",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            command=self._set_mode_chat,
        )
        self.btn_chat.pack(side="left", padx=4)

        self.btn_quiz = tk.Button(
            nav_frame,
            text="[ Mode: Concept Quiz ]",
            bg="#3f3f46",
            fg="#d4d4d8",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            command=self._set_mode_quiz,
        )
        self.btn_quiz.pack(side="left", padx=4)

        self.cache_chip = tk.Label(
            nav_frame,
            text="Snippet: None",
            bg="#3f3f46",
            fg="#a1a1aa",
            font=("Consolas", 8, "bold"),
            padx=6,
            pady=2,
        )
        self.cache_chip.pack(side="left", padx=8)

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
            width=23,
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

        # 2. Resizable 50/50 Paned Window
        self.paned = tk.PanedWindow(
            self,
            orient=tk.HORIZONTAL,
            bg="#3f3f46",
            sashwidth=6,
            sashrelief="flat",
            bd=0,
            opaqueresize=True,
        )
        self.paned.pack(fill="both", expand=True, padx=8, pady=8)

        # ─────────────────────────────────────────────────────────────
        # LEFT PANE: Output & Explanation Feed (READ-ONLY)
        # ─────────────────────────────────────────────────────────────
        self.left_pane = tk.Frame(self.paned, bg="#18181b")
        self.paned.add(self.left_pane, minsize=340)

        self.left_ctrl_bar = tk.Frame(self.left_pane, bg="#27272a", padx=10, pady=8)
        self.left_ctrl_bar.pack(fill="x")

        # Dissector Controls
        self.dissect_left_bar = tk.Frame(self.left_ctrl_bar, bg="#27272a")
        self.dissect_left_bar.pack(fill="x")

        tk.Label(
            self.dissect_left_bar,
            text="Focus Mode:",
            bg="#27272a",
            fg="#e4e4e7",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=(0, 4))

        self.focus_mode_combo = ttk.Combobox(
            self.dissect_left_bar,
            values=[
                "Unified (Mechanics First)",
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
            text="💬 Mentor Dialogue & Explanations",
            bg="#27272a",
            fg="#a78bfa",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        tk.Button(
            self.chat_left_bar,
            text="Clear Chat",
            bg="#3f3f46",
            fg="#d4d4d8",
            relief="flat",
            font=("Segoe UI", 8),
            command=self._clear_chat_history,
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
            self.quiz_left_bar, values=LEVEL_OPTIONS, state="readonly", width=28
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

        # Read-Only Banner Strip
        out_banner = tk.Frame(self.left_pane, bg="#121214", padx=10, pady=4)
        out_banner.pack(fill="x", pady=(4, 0))
        tk.Label(
            out_banner,
            text="OUTPUT & RESPONSES [READ-ONLY]",
            bg="#121214",
            fg="#71717a",
            font=("Consolas", 8, "bold"),
        ).pack(side="left")

        # Text Output Area (Read-Only)
        out_frame = tk.Frame(self.left_pane, bg="#18181b")
        out_frame.pack(fill="both", expand=True)

        self.output_txt = tk.Text(
            out_frame,
            bg="#121214",
            fg="#d4d4d8",
            insertbackground="#121214",
            relief="flat",
            wrap="word",
            font=("Segoe UI", 10),
            padx=14,
            pady=14,
        )
        self.output_txt.pack(side="left", fill="both", expand=True)

        # Intercept any user keystrokes in output to ensure it remains strictly un-editable
        self.output_txt.bind("<Key>", lambda e: "break")

        scroll_out = tk.Scrollbar(
            out_frame, command=self.output_txt.yview, bg="#18181b", troughcolor="#121214"
        )
        scroll_out.pack(side="right", fill="y")
        self.output_txt.config(yscrollcommand=scroll_out.set)

        self._configure_text_tags()

        # ─────────────────────────────────────────────────────────────
        # RIGHT PANE: Code Snippet & Interactive Inputs (EDITABLE)
        # ─────────────────────────────────────────────────────────────
        self.right_pane = tk.Frame(self.paned, bg="#18181b")
        self.paned.add(self.right_pane, minsize=340)

        # Mode A: Dissector Right View
        self.dissect_right_frame = tk.Frame(self.right_pane, bg="#18181b")
        self.dissect_right_frame.pack(fill="both", expand=True)

        right_top = tk.Frame(self.dissect_right_frame, bg="#27272a", padx=10, pady=8)
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
            font=("Consolas", 10),
            wrap="none",
            padx=10,
            pady=10,
        )
        self.dissect_code_txt.pack(side="left", fill="both", expand=True)

        code_v_scroll = tk.Scrollbar(
            code_box_frame, command=self.dissect_code_txt.yview, bg="#18181b", troughcolor="#121214"
        )
        code_v_scroll.pack(side="right", fill="y")
        self.dissect_code_txt.config(yscrollcommand=code_v_scroll.set)

        code_h_scroll = tk.Scrollbar(
            self.dissect_right_frame,
            orient="horizontal",
            command=self.dissect_code_txt.xview,
            bg="#18181b",
            troughcolor="#121214",
        )
        code_h_scroll.pack(fill="x")
        self.dissect_code_txt.config(xscrollcommand=code_h_scroll.set)

        # Mode B: Chat Right View
        self.chat_right_frame = tk.Frame(self.right_pane, bg="#18181b")

        chat_top = tk.Frame(self.chat_right_frame, bg="#27272a", padx=10, pady=6)
        chat_top.pack(fill="x")
        tk.Label(
            chat_top,
            text="ACTIVE CODE SNIPPET (INPUT)",
            bg="#27272a",
            fg="#38bdf8",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        self.chat_code_txt = tk.Text(
            self.chat_right_frame,
            height=12,
            bg="#121214",
            fg="#f4f4f5",
            insertbackground="white",
            relief="flat",
            font=("Consolas", 10),
            wrap="none",
            padx=8,
            pady=8,
        )
        self.chat_code_txt.pack(fill="both", expand=True, pady=(4, 6))

        chat_mid = tk.Frame(self.chat_right_frame, bg="#27272a", padx=10, pady=6)
        chat_mid.pack(fill="x")
        tk.Label(
            chat_mid,
            text="YOUR QUESTION / INQUIRY (INPUT)",
            bg="#27272a",
            fg="#a78bfa",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        self.chat_query_txt = tk.Text(
            self.chat_right_frame,
            height=8,
            bg="#18181b",
            fg="#f4f4f5",
            insertbackground="white",
            relief="flat",
            font=("Segoe UI", 10),
            padx=8,
            pady=8,
        )
        self.chat_query_txt.pack(fill="x", pady=(4, 6))

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

        # Mode C: Quiz Right View
        self.quiz_right_frame = tk.Frame(self.right_pane, bg="#18181b")

        quiz_top = tk.Frame(self.quiz_right_frame, bg="#27272a", padx=10, pady=6)
        quiz_top.pack(fill="x")
        tk.Label(
            quiz_top,
            text="ACTIVE CODE SNIPPET (INPUT)",
            bg="#27272a",
            fg="#38bdf8",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        self.quiz_code_txt = tk.Text(
            self.quiz_right_frame,
            height=12,
            bg="#121214",
            fg="#f4f4f5",
            insertbackground="white",
            relief="flat",
            font=("Consolas", 10),
            wrap="none",
            padx=8,
            pady=8,
        )
        self.quiz_code_txt.pack(fill="both", expand=True, pady=(4, 6))

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

        self.quiz_answer_txt = tk.Text(
            self.quiz_right_frame,
            height=8,
            bg="#18181b",
            fg="#f4f4f5",
            insertbackground="white",
            relief="flat",
            font=("Segoe UI", 10),
            padx=8,
            pady=8,
        )
        self.quiz_answer_txt.pack(fill="x", pady=(4, 6))

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

        # Global Hotkey
        self.bind("<Control-Return>", lambda e: self._on_action_click())
        self.current_mode = "dissect"

    def _show_welcome_instructions(self):
        """Displays clear instructions on initial launch."""
        welcome_md = """### SELF LEARNER // WORKSPACE GUIDE

**How this layout works:**

1. **Left Pane (Here) -> Responses & Explanations**
   • This feed is **Read-Only** so outputs never get accidentally edited.
   • Line-by-line mechanics, mentor replies, and quiz questions appear here.

2. **Right Pane -> Input Workspace**
   • **Dissector Mode:** Paste the Python code you want dissected into the right editor.
   • **Mentor Chat:** Code goes in the upper box, and your specific question goes in the lower box.
   • **Concept Quiz:** The snippet stays on top, and you type your solution into the bottom box.

3. **Global Hotkey:**
   • Press **Ctrl+Enter** from any active input box to run.
"""
        self.output_txt.config(state="normal")
        self.output_txt.delete("1.0", tk.END)
        render_markdown(self.output_txt, welcome_md)
        self.output_txt.config(state="disabled")

    def _init_sash_50_50(self):
        self.update_idletasks()
        w = self.paned.winfo_width()
        if w > 200:
            self.paned.sash_place(0, w // 2, 0)
        else:
            self.after(80, self._init_sash_50_50)

    def _configure_text_tags(self):
        self.output_txt.tag_configure(
            "normal", font=("Segoe UI", 10), foreground="#d4d4d8"
        )
        self.output_txt.tag_configure(
            "meta_badge",
            font=("Segoe UI", 9, "bold"),
            foreground="#38bdf8",
            background="#0f172a",
        )
        self.output_txt.tag_configure(
            "chat_user_badge",
            font=("Segoe UI", 10, "bold"),
            foreground="#fbbf24",
            background="#27272a",
        )
        self.output_txt.tag_configure(
            "meta_trail", font=("Segoe UI", 8), foreground="#f87171"
        )
        self.output_txt.tag_configure(
            "h1",
            font=("Segoe UI", 14, "bold"),
            foreground="#60a5fa",
            spacing1=8,
            spacing3=4,
        )
        self.output_txt.tag_configure(
            "h2",
            font=("Segoe UI", 12, "bold"),
            foreground="#818cf8",
            spacing1=8,
            spacing3=4,
        )
        self.output_txt.tag_configure(
            "h3",
            font=("Segoe UI", 11, "bold"),
            foreground="#a78bfa",
            spacing1=6,
            spacing3=2,
        )
        self.output_txt.tag_configure(
            "h4",
            font=("Segoe UI", 10, "bold"),
            foreground="#c084fc",
            spacing1=4,
            spacing3=2,
        )
        self.output_txt.tag_configure(
            "bold", font=("Segoe UI", 10, "bold"), foreground="#ffffff"
        )
        self.output_txt.tag_configure(
            "inline_code",
            font=("Consolas", 9),
            foreground="#fb7185",
            background="#27272a",
        )
        self.output_txt.tag_configure(
            "code_block",
            font=("Consolas", 9),
            foreground="#4ade80",
            background="#18181b",
        )
        self.output_txt.tag_configure(
            "divider", font=("Consolas", 9), foreground="#3f3f46"
        )
        self.output_txt.tag_configure(
            "quote", font=("Segoe UI", 10, "italic"), foreground="#94a3b8"
        )
        self.output_txt.tag_configure(
            "table_border", font=("Consolas", 9), foreground="#52525b"
        )
        self.output_txt.tag_configure(
            "table_header",
            font=("Consolas", 9, "bold"),
            foreground="#38bdf8",
            background="#1e293b",
        )
        self.output_txt.tag_configure(
            "table_body", font=("Consolas", 9), foreground="#e4e4e7"
        )

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
            if code:
                self.cached_snippet = code
        elif self.current_mode == "chat":
            code = self.chat_code_txt.get("1.0", tk.END).strip()
            if code:
                self.cached_snippet = code
        elif self.current_mode == "quiz":
            code = self.quiz_code_txt.get("1.0", tk.END).strip()
            if code:
                self.cached_snippet = code
        self._update_cache_display()

    def _clear_cached_snippet(self):
        self.cached_snippet = ""
        self.chat_history = []
        self.active_concept_name = ""
        self.active_question = ""
        self.awaiting_quiz_answer = False
        self._update_cache_display()
        self.dissect_code_txt.delete("1.0", tk.END)
        self.chat_code_txt.delete("1.0", tk.END)
        self.chat_query_txt.delete("1.0", tk.END)
        self.quiz_code_txt.delete("1.0", tk.END)
        self.quiz_answer_txt.delete("1.0", tk.END)
        self.output_txt.config(state="normal")
        self.output_txt.delete("1.0", tk.END)
        self.output_txt.insert(
            tk.END, "Session reset. Cache and chat memory cleared.\n", "quote"
        )
        self.output_txt.config(state="disabled")
        self._set_status("Reset complete")

    def _clear_chat_history(self):
        self.chat_history = []
        self.output_txt.config(state="normal")
        self.output_txt.delete("1.0", tk.END)
        self.output_txt.insert(
            tk.END, "Chat history cleared. Active snippet retained.\n", "quote"
        )
        self.output_txt.config(state="disabled")
        self._set_status("Chat history cleared")

    def _get_selected_provider(self) -> str:
        choice = self.provider_combo.get()
        return PROVIDER_CHOICES.get(choice, "auto")

    def _set_mode_dissect(self):
        self._sync_active_snippet_from_ui()
        self.current_mode = "dissect"
        self.btn_dissect.config(bg="#3b82f6", fg="white")
        self.btn_chat.config(bg="#3f3f46", fg="#d4d4d8")
        self.btn_quiz.config(bg="#3f3f46", fg="#d4d4d8")

        self.chat_left_bar.pack_forget()
        self.quiz_left_bar.pack_forget()
        self.dissect_left_bar.pack(fill="x")

        self.chat_right_frame.pack_forget()
        self.quiz_right_frame.pack_forget()
        self.dissect_right_frame.pack(fill="both", expand=True)

        if self.cached_snippet and not self.dissect_code_txt.get("1.0", tk.END).strip():
            self.dissect_code_txt.insert(tk.END, self.cached_snippet)

    def _set_mode_chat(self):
        self._sync_active_snippet_from_ui()
        self.current_mode = "chat"
        self.btn_chat.config(bg="#8b5cf6", fg="white")
        self.btn_dissect.config(bg="#3f3f46", fg="#d4d4d8")
        self.btn_quiz.config(bg="#3f3f46", fg="#d4d4d8")

        self.dissect_left_bar.pack_forget()
        self.quiz_left_bar.pack_forget()
        self.chat_left_bar.pack(fill="x")

        self.dissect_right_frame.pack_forget()
        self.quiz_right_frame.pack_forget()
        self.chat_right_frame.pack(fill="both", expand=True)

        if self.cached_snippet and not self.chat_code_txt.get("1.0", tk.END).strip():
            self.chat_code_txt.insert(tk.END, self.cached_snippet)

    def _set_mode_quiz(self):
        self._sync_active_snippet_from_ui()
        self.current_mode = "quiz"
        self.btn_quiz.config(bg="#10b981", fg="white")
        self.btn_dissect.config(bg="#3f3f46", fg="#d4d4d8")
        self.btn_chat.config(bg="#3f3f46", fg="#d4d4d8")

        self.dissect_left_bar.pack_forget()
        self.chat_left_bar.pack_forget()
        self.quiz_left_bar.pack(fill="x")

        self.dissect_right_frame.pack_forget()
        self.chat_right_frame.pack_forget()
        self.quiz_right_frame.pack(fill="both", expand=True)

        if self.cached_snippet and not self.quiz_code_txt.get("1.0", tk.END).strip():
            self.quiz_code_txt.insert(tk.END, self.cached_snippet)

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
        self.output_txt.config(state="disabled")
        self.output_txt.see(tk.END)
        self._set_status(f"Resolved: {provider} ({model})")

    def _on_action_click(self):
        if self.current_mode == "dissect":
            text = self.dissect_code_txt.get("1.0", tk.END).strip()
            if text:
                self.cached_snippet = text
                self._update_cache_display()
            if not self.cached_snippet:
                self._display_error("Paste code in the right pane to dissect.")
                return
            self._trigger_dissect(self.cached_snippet)

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
                self.after(0, lambda: self.btn_run_dissect.config(state="normal"))

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
                    self._display_formatted_response(
                        preview, meta, append=False
                    )
                    self.awaiting_quiz_answer = True
                    self.quiz_mid_lbl.config(text="TYPE YOUR ANSWER BELOW (INPUT):")
                    self.btn_quiz_action.config(
                        text="Submit", bg="#10b981", state="normal"
                    )
                    self.quiz_answer_txt.delete("1.0", tk.END)

                self.after(0, ui_update)
            except Exception as e:
                self.after(0, lambda: self._handle_error(str(e)))
                self.after(0, lambda: self.btn_quiz_action.config(state="normal"))

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
                    self._display_formatted_response(
                        output, meta, append=False
                    )
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
                self.after(0, lambda: self.btn_quiz_action.config(state="normal"))
                self.after(0, lambda: self.btn_quiz_submit.config(state="normal"))

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