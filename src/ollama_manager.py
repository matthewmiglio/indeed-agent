"""Ollama process and model management.

Provides:
  - is_ollama_running()      check if the local Ollama API is reachable
  - start_ollama()           launch `ollama serve` in the background
  - kill_ollama()            terminate the local Ollama process
  - list_installed_models()  return tag names currently in Ollama inventory
  - verify_models(required)  raise if any required model is missing
  - ensure_ollama_ready(req) one-shot startup helper used at agent boot

Used by main.py at startup to guarantee Ollama is up and the required
models (default: ``mistral``) are pulled before the agent runs.
"""

import os
import socket
import subprocess
import time
import urllib.request
import urllib.error
import json

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
DEFAULT_MODELS = ["mistral"]


def is_ollama_running(timeout: float = 1.5) -> bool:
    """Return True if the Ollama HTTP API responds at OLLAMA_HOST."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError):
        return False


def start_ollama(wait_seconds: float = 15.0) -> bool:
    """Launch `ollama serve` in the background and wait for it to come up.

    Returns True if Ollama is reachable within wait_seconds, else False.
    """
    if is_ollama_running():
        return True

    creationflags = 0
    if os.name == "nt":
        # Detach on Windows so it survives this process and doesn't open a console
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS

    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Ollama is not installed or not on PATH.\n"
            "Install it from https://ollama.com/download and re-run."
        )

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if is_ollama_running():
            return True
        time.sleep(0.5)
    return False


def kill_ollama() -> bool:
    """Terminate the local Ollama process. Returns True if a process was killed."""
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/F", "/IM", "ollama.exe"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.returncode == 0
    else:
        result = subprocess.run(["pkill", "-f", "ollama"], stdout=subprocess.PIPE)
        return result.returncode == 0


def list_installed_models() -> list[str]:
    """Return the list of model tags installed in Ollama (e.g. ['mistral:latest'])."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", [])]
    except (urllib.error.URLError, socket.timeout, OSError, json.JSONDecodeError):
        return []


def verify_models(required: list[str] = None):
    """Raise RuntimeError listing missing models with the exact `ollama pull` fix."""
    required = required or DEFAULT_MODELS
    installed = list_installed_models()

    def _has(model: str) -> bool:
        # Match either exact tag or the base name (e.g. "mistral" matches "mistral:latest")
        for tag in installed:
            if tag == model or tag.split(":")[0] == model:
                return True
        return False

    missing = [m for m in required if not _has(m)]
    if missing:
        pulls = "\n".join(f"  ollama pull {m}" for m in missing)
        raise RuntimeError(
            f"Missing Ollama model(s): {', '.join(missing)}\n"
            f"Fix by running:\n{pulls}"
        )


def ensure_ollama_ready(required: list[str] = None):
    """Boot-time helper: ensure Ollama is running and required models are present.

    Raises RuntimeError with a plain-English fix if anything is missing.
    """
    required = required or DEFAULT_MODELS

    if not is_ollama_running():
        print("  >> Ollama not running — starting it...")
        if not start_ollama():
            raise RuntimeError(
                "Failed to start Ollama within timeout.\n"
                "Try starting it manually: `ollama serve`"
            )
        print("  >> Ollama is up.")

    verify_models(required)


if __name__ == "__main__":
    ensure_ollama_ready()
    print("Ollama ready. Installed models:", list_installed_models())
