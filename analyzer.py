import sys
from providers import call_llm

PROMPT_TEMPLATES = {
    "all": """You are an experienced, peer-level software engineer and mentor. 
Dissect the following Python snippet:

--- CODE START ---
{code}
--- CODE END ---

Follow this EXACT voice, structure, and pacing:

### 1. SYNTAX VALIDATION
- If valid: State "**STATUS: VALID**"
- If invalid: State "**STATUS: INVALID**", point out the 1-2 exact syntax issues plainly, and provide the corrected snippet before dissecting.

### 2. LINE-BY-LINE MECHANICS
Break down the code line-by-line using this exact conversational cadence:

[Number]. [Punchy Title of What This Line Accomplishes]
`[code line]`
Explain in conversational plain English what this gives access to or does.
Suppose:
[Concrete dummy input / mock state]
Then:
[What variable or expression evaluates to]
Why this way?
[1-2 sentences on why this syntax or idiom was used instead of a naive way]

### 3. THE ARCHITECTURAL PICTURE
1. Dataflow Diagram:
A clean, minimal vertical text/ASCII flow of how data moves through this logic:
input -> step 1 -> step 2 -> output

2. The Old Way vs. The Clean Way:
Show a quick 2-line contrast:
- The old/naive way: `[quick snippet of hardcoded/clunky alternative]`
- Your architecture: `[why this snippet's approach is superior]`
""",
    "mechanics": """You are an experienced code mentor walking through Python mechanics.

--- CODE START ---
{code}
--- CODE END ---

Check syntax first. If invalid, state **STATUS: INVALID**, show the corrected snippet, and explain the working code.

Walk through every logical line using this exact rhythmic structure:

1. [Action Title]
`[code line]`
What it does in plain English.
Suppose:
[Concrete dummy data]
Then:
[What the runtime state or variable becomes]
Why?
[Why the syntax is structured like this]

End with a quick vertical ASCII dataflow showing how data moves through the snippet, followed by a 2-line "The old naive way vs. Your clean way" comparison.
""",
    "fixes": """You are a precision code debugger and reviewer.
Inspect the following snippet strictly for syntax errors, typos, bugs, and anti-patterns:

--- CODE START ---
{code}
--- CODE END ---

CRITICAL FORMATTING RULES:
1. DO NOT output introductory paragraphs, greetings, or overview markdown tables.
2. Jump directly into Section 1.
3. Be blunt, direct, and token-efficient.

Format strictly as follows:

### 1. IDENTIFIED DEFECTS (Total: [Count])
- State the total count of distinct errors/defects.
- List each issue as a concise bullet point stating plainly what is wrong without narrative padding (e.g., "• Line 1: 'pirnt' typo instead of built-in 'print' (NameError)", "• Line 4: Missing colon at end of 'def' statement (SyntaxError)").

### 2. TARGETED FIXES
Provide the corrected code block. Every single corrected line or replacement MUST contain a targeted comment (`# Line X fix: ...` or `# Changed: ...`) pointing out where the correction belongs and what was replaced.

### 3. EXPLANATION
In 2-4 concise sentences, explain the mechanics of why these fixes resolve the syntax or runtime failure.
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
}


def analyze_code(code: str, mode: str = "all", provider: str = None) -> str:
    template = PROMPT_TEMPLATES.get(mode, PROMPT_TEMPLATES["all"])
    prompt = template.format(code=code)
    return call_llm(prompt, provider=provider)