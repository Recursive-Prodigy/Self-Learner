import json
from pathlib import Path
from analyzer import run_analyzer
from quiz import evaluate_answer, generate_question

DATA_FILE = Path("concepts.json")


def load_concepts():
    if not DATA_FILE.exists():
        return []
    with open(DATA_FILE, "r") as f:
        return json.load(f)


def save_concepts(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def run_quiz():
    concepts = load_concepts()
    if not concepts:
        print("No concepts found in concepts.json.")
        return

    print("\n--- TRACKED CONCEPTS ---")
    for i, c in enumerate(concepts):
        print(f"[{i}] {c['name']} (Level {c['level']}/6)")

    choice_raw = input("\nSelect a concept index: ").strip()
    if not choice_raw.isdigit() or int(choice_raw) >= len(concepts):
        print("Invalid selection.")
        return

    target = concepts[int(choice_raw)]

    print(f"\nAnalyzing: {target['name']} | Level {target['level']}")
    print("-" * 50)
    q_text = generate_question(target)
    print(q_text)
    print("-" * 50)

    user_ans = input("\nYour answer:\n> ").strip()
    result = evaluate_answer(target, q_text, user_ans)

    print(f"\nVERDICT: {result.get('verdict', 'FAIL')}")
    print(f"\nENGINE POST-MORTEM:\n{result.get('engine_postmortem', '')}")
    print(f"\nIDEAL ANSWER:\n{result.get('ideal_answer', '')}")

    if result.get("verdict") == "PASS" and target["level"] < 6:
        target["level"] += 1
        save_concepts(concepts)
        print(f"\n[+] Promoted to Level {target['level']}!")
    else:
        print(f"\n[-] Kept at Level {target['level']}.")


def main():
    while True:
        print("\n==============================")
        print("      LEARNER ENGINE v0       ")
        print("==============================")
        print("[1] Dissect raw code line-by-line (Analyzer)")
        print("[2] Test comprehension on tracked concepts (Quiz)")
        print("[q] Quit")

        choice = input("\nSelect mode: ").strip().lower()
        if choice == "1":
            run_analyzer()
        elif choice == "2":
            run_quiz()
        elif choice == "q":
            break


if __name__ == "__main__":
    main()