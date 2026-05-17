"""Entry point for the OpenClaw Indeed job application agent.

Supports four modes:
  - ``python main.py login``          — open a browser for manual Indeed login
  - ``python main.py profile``        — interactive profile data collection
  - ``python main.py <prompt>``       — run a one-shot agent command
  - ``python main.py``                — interactive REPL loop
"""

import asyncio
import sys
import os

# Add src to path so imports work when run from project root
sys.path.insert(0, os.path.dirname(__file__))

from agent import run_agent
from browser import login_session
from profile_manager import collect_all, print_status, load_profile, check_completeness
from ollama_manager import ensure_ollama_ready

BANNER = """
 ___                    ___ _
/ _ \\ _ __  ___ _ _   / __| |__ ___ __ __
| (_) | '_ \\/ -_) ' \\ | (__| / _` \\ V  V /
\\___/| .__/\\___|_||_| \\___|_\\__,_|\\_/\\_/
     |_|
Indeed Job Application Agent

Type your request, or 'quit' to exit.
"""


def parse_resume_arg(args: list[str]) -> tuple[str | None, list[str]]:
    """Extract --resume path from args. Returns (resume_path, remaining_args)."""
    resume_path = None
    remaining = []
    i = 0
    while i < len(args):
        if args[i] == "--resume" and i + 1 < len(args):
            resume_path = args[i + 1]
            i += 2
        else:
            remaining.append(args[i])
            i += 1
    return resume_path, remaining


def parse_debug_flag(args: list[str]) -> tuple[bool, list[str]]:
    """Extract --debug flag from args. Returns (debug_enabled, remaining_args)."""
    if "--debug" in args:
        return True, [a for a in args if a != "--debug"]
    return False, args


def parse_dry_run_flag(args: list[str]) -> tuple[bool, list[str]]:
    """Extract --dry-run flag. Returns (dry_run, remaining_args)."""
    if "--dry-run" in args:
        return True, [a for a in args if a != "--dry-run"]
    return False, args


def main():
    """Parse CLI args and run the appropriate mode."""
    model = "mistral"
    args = sys.argv[1:]

    # Login mode
    if args and args[0] == "login":
        asyncio.run(login_session())
        return

    # Profile mode
    if args and args[0] == "profile":
        collect_all()
        return

    # Extract --debug, --dry-run, and --resume flags if present
    debug, args = parse_debug_flag(args)
    dry_run, args = parse_dry_run_flag(args)
    if debug:
        import browser
        browser.DEBUG_MODE = True
        print("  [debug] DEBUG_MODE on — pages will be dumped to data/debug/")
    if dry_run:
        import browser, form_filler
        browser.DEBUG_MODE = True
        form_filler.DRY_RUN = True
        print("  [dry-run] No application will be submitted. All pages dumped to data/debug/.")
    resume_path, remaining_args = parse_resume_arg(args)

    # One-shot command
    if remaining_args:
        prompt = " ".join(remaining_args)

        ensure_ollama_ready([model])

        # Quick profile check
        filled, total, missing = check_completeness()
        if missing:
            print(f"\n  WARNING: Profile incomplete ({filled}/{total}). Missing: {', '.join(missing)}")
            print(f"  Run 'python src/main.py profile' to fill in missing data.\n")
            answer = input("  Continue anyway? (yes/no): ").strip().lower()
            if answer not in ("yes", "y"):
                return

        print(f"\n> {prompt}\n")
        asyncio.run(run_agent(prompt, resume_path=resume_path, model=model, dry_run=dry_run))
        return

    # Interactive mode
    print(BANNER)
    ensure_ollama_ready([model])
    print_status()
    print()

    while True:
        try:
            prompt = input("you: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not prompt:
            continue
        if prompt.lower() in ("quit", "exit", "q"):
            print("Bye.")
            break
        if prompt.lower() == "profile":
            collect_all()
            print()
            continue

        print()
        asyncio.run(run_agent(prompt, resume_path=resume_path, model=model))
        print()


if __name__ == "__main__":
    main()
