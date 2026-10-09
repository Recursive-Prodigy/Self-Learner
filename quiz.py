import json
import re
from providers import call_llm

LEVEL_DESCRIPTIONS = {
    1: "Recognition (Identify what this specific syntax does mechanically)",
    2: "Explanation (Explain line-by-line runtime behavior, memory, or state)",
    3: "Prediction (Predict exact return value, mutation, or side-effects)",
    4: "Modification (Adapt this specific snippet to change behavior or handle an edge case)",
    5: "Reconstruction (Rewrite this pattern with an idiomatic variation or fix)",
    6: "Transfer (Apply this exact syntax pattern to a new scenario)",
}


def extract_concept_name(code_snippet: str, provider: str = None) -> str:
    """Dynamically names the core Python pattern/syntax found in the snippet."""
    prompt = (
        "Identify the primary Python syntax pattern or mechanism demonstrated in this snippet:\n\n"
        "```python\n"
        + code_snippet
        + "\n```\n\n"
        "Respond with ONLY a concise 2-4 word title for the pattern (e.g., 'Dict Comprehension Whitelist', "
        "'SQLite Parameter Unpacking', 'Generator String Joining'). No punctuation, no extra text."
    )
    name = call_llm(prompt, provider=provider).strip().replace('"', "")
    return name.split("\n")[0][:40]


def generate_question_from_snippet(
    code_snippet: str, level: int = 1, provider: str = None
) -> dict:
    """Generates a question grounded strictly in the pasted code at the chosen level."""
    concept_name = extract_concept_name(code_snippet, provider=provider)

    prompt = (
        "You are a technical code evaluator testing authentic Python syntax comprehension.\n\n"
        "Target Snippet:\n"
        "```python\n"
        + code_snippet
        + "\n```\n\n"
        + f"Identified Pattern: {concept_name}\n"
        + f"Target Level: Level {level}/6 - {LEVEL_DESCRIPTIONS.get(level, '')}\n\n"
        + "CRITICAL RULES:\n"
        + "1. Ground the question 100% in the exact lines, tokens, data structures, and runtime mechanics of the snippet above.\n"
        + f"2. Match the cognitive demand of Level {level}:\n"
        + "   - Level 1: What is this syntax doing mechanically?\n"
        + "   - Level 2: Walk through what happens in memory/state.\n"
        + "   - Level 3: What does this evaluate to, or what breaks if X input is provided?\n"
        + "   - Level 4: How would you change this snippet to do Y?\n"
        + "   - Level 5: Rewrite this snippet to avoid a specific naive flaw.\n"
        + "   - Level 6: How does this exact pattern solve a related task?\n"
        + "3. Keep it punchy and direct. Do NOT include the answer.\n"
    )
    q_text = call_llm(prompt, provider=provider).strip()
    return {"concept_name": concept_name, "level": level, "question": q_text}


def evaluate_answer(
    concept_name: str,
    level: int,
    snippet: str,
    question: str,
    user_answer: str,
    provider: str = None,
) -> dict:
    prompt = (
        "You are an exacting senior software engineer grading a developer's syntax comprehension.\n\n"
        "Snippet:\n"
        "```python\n"
        + snippet
        + "\n```\n\n"
        + f"Pattern: {concept_name} (Testing at Level {level}/6)\n"
        + f"Question: {question}\n"
        + f"User Answer: {user_answer}\n\n"
        + "Evaluate whether this answer demonstrates authentic understanding of the syntax and execution mechanics or superficial guesswork.\n"
        + "Respond strictly in valid JSON format with three keys:\n"
        + "{\n"
        + '  "verdict": "PASS" or "FAIL",\n'
        + '  "engine_postmortem": "A 2-4 sentence review of why the candidate\'s answer is accurate or where their mental model failed regarding the syntax/runtime.",\n'
        + '  "ideal_answer": "A concise, master-level answer demonstrating idiomatic code or explanation."\n'
        + "}\n"
    )
    raw = call_llm(prompt, provider=provider)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass

    return {
        "verdict": "PASS" if "PASS" in raw.upper() else "FAIL",
        "engine_postmortem": raw,
        "ideal_answer": "Refer to post-mortem above.",
    }