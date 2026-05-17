# indeed-agent

Autonomous browser agent that walks Indeed Easy Apply listings end-to-end. Searches jobs from a natural-language prompt, scores matches, fills application forms via a keyword map with a local Mistral/Ollama LLM fallback, and stops at submit in dry-run mode.

## What it does

Give it a prompt like:

```
apply to 10 graphic design jobs in Farmington, MI
```

…and it will:

1. Parse the prompt (role + location + count) via `prompt_parser.py`
2. Search Indeed and collect job cards (`job_searcher.py`)
3. Score each listing against your profile (`job_scorer.py`)
4. Walk the Easy Apply form step-by-step (`form_filler.py`), answering questions from `data/user_profile.yaml` first, then falling back to a local Mistral model via Ollama (`llm_answerer.py`)
5. Stop at the submit button in `--dry-run` mode (never submits real applications unless explicitly allowed)

## Screenshots

Job listing detected on Indeed:

![Job listing](images/job-listing1.png)

The agent walking an Easy Apply form:

![Application QA 1](images/application-QA1.png)

![Application QA 2](images/application-QA2.png)

![Application QA 3](images/application-QA3.png)

Final review / would-submit step (dry-run halts here):

![Submit application](images/submit-application1.png)

## Quick start

First run — seed your persistent Indeed session:

```powershell
poetry install
poetry run python src/main.py login
```

### Live mode (actually applies)

```powershell
poetry run python src/main.py "apply to 10 graphic design jobs in Farmington, MI" `
  --resume "C:\path\to\resume.pdf"
```

This submits real applications. Each Easy Apply walk ends by clicking the real submit button.

### Dry-run (test / debug)

Add `--dry-run` to walk the whole flow without submitting. The submit button becomes a no-op that logs intent and dumps the final page as `*-would-submit.*` in `data/debug/`. Useful for verifying selectors and field handling on a new job category before turning it loose.

```powershell
poetry run python src/main.py "apply to 10 graphic design jobs in Farmington, MI" `
  --resume "C:\path\to\resume.pdf" --dry-run
```

### Other flags

- `--debug` — dump every page as HTML + PNG to `data/debug/` (implied by `--dry-run`).
- `--resume <path>` — resume PDF to upload during the application.

## Architecture

| File | Role |
|---|---|
| `src/main.py` | CLI entry. Parses `--debug`, `--dry-run`, `--resume`. |
| `src/agent.py` | Orchestrator: parse → search → score → apply. |
| `src/browser.py` | Playwright launch, page dumping, screenshots. |
| `src/job_searcher.py` | Indeed search + listing collection + job-detail extraction. |
| `src/form_analyzer.py` | Scans form pages for inputs/selects/textareas/radios. |
| `src/form_filler.py` | Step loop, `FIELD_MAP` keyword table, submit gate. |
| `src/llm_answerer.py` | Local Mistral fallback for unknown fields. |
| `src/field_inventory.py` | Per-run dedup + summary of every field seen. |
| `src/ollama_manager.py` | Auto-starts Ollama, ensures `mistral` is pulled. |

## Safety

The default mode submits real applications — this app is built to work. Use `--dry-run` whenever you're iterating on selectors, profile data, or a new job category. It walks the entire flow and stops at submit, logging intent and dumping the final page as `*-would-submit.*`. Once you've verified a run looks clean, drop the flag and go live.

## Requirements

- Python 3.11+ via Poetry
- Playwright (auto-installed via Poetry)
- Ollama running locally with the `mistral` model pulled
