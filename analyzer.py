import sys
from providers import call_llm

PROMPT_TEMPLATES = {
    "all": """You are an experienced, peer-level software engineer and mentor. 
Dissect the following Python snippet:

--- CODE START ---
{code}
--- CODE END ---

CRITICAL FILTERING & NUMBERING RULES:
1. NEVER count, mention, or explain blank lines. Do NOT write "(Blank line – no action)" or any whitespace filler. Skip blank lines completely.
2. Number your breakdown sequentially (1, 2, 3, ...) based ONLY on meaningful statements.
3. Do NOT include a dataflow diagram here (the user can request one separately).
4. Do NOT show or reconstruct "The Old Way".

Follow this EXACT voice, structure, and pacing:

### 1. SYNTAX VALIDATION
- If valid: State "**STATUS: VALID**"
- If invalid: State "**STATUS: INVALID**", point out the 1-2 exact syntax issues plainly, and provide the corrected snippet before dissecting.

### 2. STATEMENT-BY-STATEMENT MECHANICS
Break down the code statement-by-statement using this exact conversational cadence:

[Number]. [Punchy Title of What This Statement Accomplishes]
`[code statement]`
Explain in conversational plain English what this gives access to or does.
Suppose:
[Concrete dummy input / mock state]
Then:
[What variable or expression evaluates to]
Why this way?
[1-2 sentences on why this syntax or idiom was used instead of a naive way]

### 3. CLEANER ALTERNATIVE (IF APPLICABLE)
- If the snippet is already clean, idiomatic, and optimal, write: "None needed — the snippet is already clean and idiomatic."
- If there is a genuinely cleaner or more Pythonic alternative (e.g. list/dict comprehension, context manager, walrus operator, built-in library): show ONLY the cleaner code block with a 1-2 sentence explanation of why it improves readability or performance.
- DO NOT show or reproduce "The Old Way".
""",
    "diagram": """You are a software architect.
Create a clean, elegant Mermaid.js flowchart illustrating the execution flow and data transformation of this Python snippet:

--- CODE START ---
{code}
--- CODE END ---

CRITICAL MERMAID SYNTAX RULES:
1. Start strictly with `flowchart TD`.
2. Every node label MUST be wrapped in double quotes inside its shape brackets:
   Example: A["Start"] --> B["Prompt User Input"]
   Example: C{"Input Empty?"}
   Example: D["Print Exit Message and End"]
3. NEVER use the '&' symbol anywhere in node names or labels. Use the word 'and' instead.
4. Use standard arrows: A -->|Yes| B
5. Output ONLY the ```mermaid ... ``` code block followed by a 1-sentence caption.
""",
    "mechanics": """You are an experienced code mentor walking through Python mechanics.

--- CODE START ---
{code}
--- CODE END ---

CRITICAL FILTERING & NUMBERING RULES:
1. NEVER count, mention, or explain blank lines. Do NOT write "(Blank line – no action)" or any whitespace filler.
2. Completely skip all empty lines. Move straight to the next executable statement.
3. Number steps sequentially (1, 2, 3, ...) based solely on meaningful statements.

Check syntax first. If invalid, state **STATUS: INVALID**, show the corrected snippet, and explain the working code.

Walk through each statement using this exact rhythmic structure:

1. [Action Title]
`[code statement]`
What it does in plain English.
Suppose:
[Concrete dummy data]
Then:
[What the runtime state or variable becomes]
Why?
[Why the syntax is structured like this]
""",
    "fixes": """You are a precision code debugger and reviewer.
Inspect the following snippet strictly for syntax errors, typos, bugs, and anti-patterns:

--- CODE START ---
{code}
--- CODE END ---

Format strictly as follows:

### 1. IDENTIFIED DEFECTS (Total: [Count])
- State the total count of distinct errors/defects.
- List each issue as a concise bullet point stating plainly what is wrong without narrative padding.

### 2. TARGETED FIXES
Provide the corrected code block with targeted comments.

### 3. EXPLANATION
In 2-4 concise sentences, explain the mechanics of why these fixes resolve the failure.
""",
    "gotchas": """You are an edge-case and performance specialist.
Inspect this snippet strictly for TRADE-OFFS, EDGE CASES, and HIDDEN FAILURE MODES:

--- CODE START ---
{code}
--- CODE END ---

Format with:
### 1. SUBTLE EDGE CASES & FAILURE MODES
List specific inputs or states that cause silent bugs, data corruption, or crashes.

### 2. ARCHITECTURAL TRADE-OFFS
Detail memory vs. CPU trade-offs, scalability limits, or typing constraints.
""",
    "compress": """You are an elite Python performance and refactoring specialist.
Aggressively compress the following Python snippet:

--- RAW CODE ---
{code}
--- END RAW CODE ---

Format strictly into two parts separated by `=== COMPRESSED_CODE ===`:

### 1. REPLACEMENTS & AUDIT (Total Changes: [Count])
- Detail what was exchanged with what.

=== COMPRESSED_CODE ===
[Output ONLY the runnable, compressed Python code here.]
""",
}


def analyze_code(code: str, mode: str = "all", provider: str = None) -> str:
    template = PROMPT_TEMPLATES.get(mode, PROMPT_TEMPLATES["all"])
    prompt = template.replace("{code}", code)
    return call_llm(prompt, provider=provider)


def generate_dataflow_diagram(code: str, provider: str = None) -> str:
    prompt = PROMPT_TEMPLATES["diagram"].replace("{code}", code)
    return call_llm(prompt, provider=provider)


def compress_code(code: str, provider: str = None) -> tuple[str, str]:
    template = PROMPT_TEMPLATES["compress"]
    prompt = template.replace("{code}", code)
    raw = call_llm(prompt, provider=provider)

    if "=== COMPRESSED_CODE ===" in raw:
        audit_part, code_part = raw.split("=== COMPRESSED_CODE ===", 1)
        clean_code = code_part.strip()
        if clean_code.startswith("```python"):
            clean_code = clean_code[9:]
        elif clean_code.startswith("```"):
            clean_code = clean_code[3:]
        if clean_code.endswith("```"):
            clean_code = clean_code[:-3]
        return audit_part.strip(), clean_code.strip()
    return raw.strip(), "# See audit pane for full refactor"