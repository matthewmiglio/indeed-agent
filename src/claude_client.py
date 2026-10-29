"""Claude shim — the single entry point for all LLM calls in the bot.

The LLM modules (prompt_parser, job_scorer, llm_answerer, resume_tuner) all do the
same thing: system prompt + user prompt (+ optional image) -> JSON.

Two auth backends, auto-selected:
  - If ANTHROPIC_API_KEY is set -> the Anthropic SDK (fast, cheap, honors `model`).
  - Otherwise -> the local `claude` CLI in headless mode, which uses your Claude Code
    subscription login (no API key needed). This is the default on this machine.

Note on the CLI backend: model selection obeys your Claude Code config. If your
global settings pin a model (settings.json `"model"` + `"switchModelsOnFlag": false`),
every call uses that model regardless of the `model` argument here.
"""

import base64
import json
import os
import subprocess
import tempfile

DEFAULT_MODEL = "claude-haiku-4-5"

# Full model id -> CLI alias (the CLI's --model takes aliases).
_ALIAS = {
    "claude-haiku-4-5": "haiku",
    "claude-sonnet-5": "sonnet",
    "claude-opus-4-8": "opus",
    "claude-fable-5": "fable",
}

_CLI_BASE = ["claude", "-p", "--strict-mcp-config", "--disable-slash-commands"]


def _trim_json(text: str) -> str:
    """Return the outermost {...} object from a model reply (strips fences/prose)."""
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end != -1 else text


def _clean_env() -> dict:
    """Env with parent Claude-Code session vars stripped so the child runs standalone."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("CLAUDE", "ANTHROPIC")) and k != "AI_AGENT"}


def _via_sdk(system, prompt, model, max_tokens, image_path):
    from anthropic import Anthropic  # lazy: only needed on the key path
    client = Anthropic()
    if image_path:
        b64 = base64.standard_b64encode(open(image_path, "rb").read()).decode()
        user_content = [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png", "data": b64}},
            {"type": "text", "text": prompt},
        ]
    else:
        user_content = prompt
    msg = client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user_content},
                  {"role": "assistant", "content": "{"}],  # prefill forces JSON
    )
    return _trim_json("{" + msg.content[0].text), msg.usage.output_tokens


def _via_cli(system, prompt, model, image_path):
    """Run one completion through the local `claude` CLI (subscription auth)."""
    args = _CLI_BASE + ["--model", _ALIAS.get(model, model), "--system-prompt", system]
    env = _clean_env()
    # Neutral cwd so the child doesn't auto-load this project's CLAUDE.md.
    cwd = tempfile.gettempdir()

    if image_path:
        # Vision requires the stream-json transport (in AND out).
        b64 = base64.standard_b64encode(open(image_path, "rb").read()).decode()
        msg = {"type": "user", "message": {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png", "data": b64}},
            {"type": "text", "text": prompt}]}}
        args += ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
        p = subprocess.run(args, input=json.dumps(msg), capture_output=True,
                           text=True, encoding="utf-8", env=env, cwd=cwd, timeout=180)
        if p.returncode != 0:
            raise RuntimeError(f"claude CLI failed: {p.stderr[:300]}")
        result, out_tokens = None, 0
        for line in p.stdout.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "result":
                result = d.get("result")
                out_tokens = d.get("usage", {}).get("output_tokens", 0)
        if result is None:
            raise RuntimeError("claude CLI returned no result event")
        return _trim_json(result), out_tokens

    args += ["--output-format", "json", prompt]
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", env=env, cwd=cwd, timeout=180)
    if p.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {p.stderr[:300]}")
    d = json.loads(p.stdout)
    return _trim_json(d["result"]), d.get("usage", {}).get("output_tokens", 0)


def chat_json(system: str, prompt: str, model: str = DEFAULT_MODEL,
              max_tokens: int = 1500, image_path: str = None) -> tuple[str, int]:
    """Ask Claude for a JSON object. Returns (raw_json_text, output_tokens).

    Uses the Anthropic SDK if ANTHROPIC_API_KEY is set, else the `claude` CLI
    (subscription). If image_path is given, the PNG is attached (vision).
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _via_sdk(system, prompt, model, max_tokens, image_path)
    return _via_cli(system, prompt, model, image_path)


def demo():
    """Self-check: the shim returns parseable JSON via whichever backend is active."""
    backend = "SDK (API key)" if os.environ.get("ANTHROPIC_API_KEY") else "claude CLI (subscription)"
    raw, toks = chat_json('Return ONLY valid JSON: {"ok": true}', "say ok")
    obj = json.loads(raw)
    assert obj.get("ok") is True, obj
    print(f"ok via {backend} ({toks} output tokens)")


if __name__ == "__main__":
    demo()
