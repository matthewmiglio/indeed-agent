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


def parse_no_tune_flag(args: list[str]) -> tuple[bool, list[str]]:
    """Extract --no-tune flag. Returns (tune_enabled, remaining_args)."""
    if "--no-tune" in args:
        return False, [a for a in args if a != "--no-tune"]
    return True, args


def preflight_warnings():
    """Print LOUD warnings for any unmet setup prerequisite. Never blocks.

    The three setup steps are: (1) profile questionnaire, (2) skills builder,
    (3) Indeed login. Any run is allowed to proceed even if some are missing —
    we just warn loudly so the user knows why it may misbehave.
    """
    import browser
    profile = load_profile()
    problems = []

    _, _, missing = check_completeness(profile)
    if missing:
        problems.append(
            f"PROFILE INCOMPLETE - {len(missing)} required field(s) missing: "
            f"{', '.join(missing)}.  Fix:  python src/main.py profile")

    if not profile.get("non_skills"):
        problems.append(
            "SKILLS NOT BUILT - no `non_skills` denylist in your profile. Tuned "
            "resumes may claim skills you don't have.  Fix:  python src/main.py skills")

    prof_dir = browser.PROFILE_DIR
    logged_in = os.path.isdir(prof_dir) and any(os.scandir(prof_dir))
    if not logged_in:
        problems.append(
            "NOT LOGGED INTO INDEED - no saved browser session. Applications will "
            "fail to submit.  Fix:  python src/main.py login")

    if problems:
        bar = "!" * 66
        print("\n" + bar)
        print("  SETUP WARNING - proceeding anyway, but these are NOT ready:")
        for p in problems:
            print(f"    - {p}")
        print(bar + "\n")


def main():
    """Parse CLI args and run the appropriate mode."""
    model = "claude-haiku-4-5"
    args = sys.argv[1:]

    # Login mode
    if args and args[0] == "login":
        asyncio.run(login_session())
        return

    # Profile mode
    if args and args[0] == "profile":
        collect_all()
        return

    # Skills questionnaire mode (setup step 2) — web UI that builds the
    # skills allowlist + non_skills denylist into the profile.
    if args and args[0] == "skills":
        from skills_questionnaire import serve
        try:
            serve(apply=True)  # stops itself once you click Save
        except KeyboardInterrupt:
            print("\nSkills questionnaire closed.")
        return

    # Profile questionnaire GUI (web equivalent of `profile`).
    if args and args[0] == "profile-gui":
        from profile_questionnaire import serve_profile
        try:
            serve_profile()  # stops itself once you click Save
        except KeyboardInterrupt:
            print("\nProfile questionnaire closed.")
        return

    # Extract --debug, --dry-run, and --resume flags if present
    debug, args = parse_debug_flag(args)
    dry_run, args = parse_dry_run_flag(args)
    tune, args = parse_no_tune_flag(args)
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

        # Loud, non-blocking setup check — warns but always proceeds.
        preflight_warnings()

        print(f"\n> {prompt}\n")
        asyncio.run(run_agent(prompt, resume_path=resume_path, model=model, dry_run=dry_run, tune=tune))
        return

    # Interactive mode
    print(BANNER)
    print_status()
    preflight_warnings()
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
        asyncio.run(run_agent(prompt, resume_path=resume_path, model=model, tune=tune))
        print()


if __name__ == "__main__":
    main()
