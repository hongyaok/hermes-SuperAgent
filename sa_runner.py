"""Launch headless ``hermes chat`` subprocesses for Superagent sessions.

Hermes' subagent lifecycle requires an active parent agent bound to a chat turn,
so the dashboard backend cannot start a subagent from an HTTP request. The
supported pattern in this Hermes version is the one the built-in kanban
dispatcher uses: spawn a headless ``hermes chat`` process and read its
stream-json output on stdout.

Each turn is one subprocess and one daemon thread. Workers never commit or
push: they leave changes in the working tree for a human to review.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

try:  # normal: imported as part of the plugin package
    from . import sa_chats
except ImportError:  # top-level import (dashboard backend, pytest)
    import sa_chats  # type: ignore[no-redef]

# How much stderr is kept for the error message of a failed turn.
STDERR_LIMIT = 4000
# Tool arguments are shown in the session timeline; keep them one line.
TOOL_DETAIL_LIMIT = 160


def parse_event(line: str) -> Optional[dict]:
    """Return one stream-json event, or None for anything that is not an object.

    The CLI also prints plain lines (for example a trailing ``session_id: ...``
    summary), so callers must tolerate junk instead of crashing.
    """
    if not isinstance(line, str):
        return None
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def build_prompt(request: str, *, repo: str, session_id: str, source: str = "desktop") -> str:
    """Instructions for the first turn of a session (resume turns send raw text)."""
    return f"""You are one Superagent worker session ({session_id}), started from {source}.

Request:
{(request or "").strip()}

Rules:
- Work only inside the repository: {repo or "the current working directory"}. Do not touch files outside it.
- Make the smallest change that satisfies the request; follow the repository's existing style.
- Run the relevant tests or checks. If there are none, say what you checked instead.
- Do NOT commit, push, merge, or open a pull request. Leave changes in the working tree.
- If the request needs a design decision, ask one concrete question rather than guessing.
- Finish with a summary of at most 200 words: what changed, files touched, checks run, and anything unresolved.
"""


def resolve_hermes_bin(explicit: Optional[str] = None) -> str:
    """Find the hermes executable: explicit arg, env override, then PATH."""
    candidate = (explicit or os.environ.get("SUPERAGENT_HERMES_BIN") or "").strip()
    if not candidate:
        candidate = shutil.which("hermes") or ""
    if not candidate:
        raise RuntimeError(
            "hermes executable not found; install hermes or set SUPERAGENT_HERMES_BIN"
        )
    return candidate


def _terminate_tree(proc: Any) -> None:
    """Stop a worker and its children (kept small so tests can patch it)."""
    if proc is None:
        return
    pid = getattr(proc, "pid", None)
    if os.name == "nt" and pid:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
            return
        except Exception:
            pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _drain_stderr(proc: Any, sink: list[str]) -> None:
    """Read stderr in the background so a chatty child cannot fill the pipe."""
    total = 0
    try:
        for line in proc.stderr:
            if total >= STDERR_LIMIT:
                continue  # keep draining, stop storing
            text = str(line)
            sink.append(text[: STDERR_LIMIT - total])
            total += len(text)
    except Exception:
        return


def _text_of(event: dict) -> str:
    """Best-effort assistant text from a stream-json event."""
    text = event.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                part.get("text")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            joined = "\n".join(parts).strip()
            if joined:
                return joined
    return ""


def _tool_detail(event: dict) -> str:
    """One-line ``tool: args`` summary for the session timeline."""
    name = event.get("tool") or event.get("name") or event.get("tool_name") or "tool"
    args = event.get("args")
    if args is None:
        args = event.get("input")
    if isinstance(args, str):
        text = args
    elif args is None:
        text = ""
    else:
        try:
            text = json.dumps(args, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(args)
    text = " ".join(text.split())
    if len(text) > TOOL_DETAIL_LIMIT:
        text = text[: TOOL_DETAIL_LIMIT - 1] + "…"
    return f"{name}: {text}" if text else str(name)


def _stderr_summary(text: str) -> str:
    """Drop the CLI's session trailer lines; keep the real diagnostics."""
    lines = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("session_id:"):
            continue
        if lowered.startswith("session:") and len(stripped.split()) == 2:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _session_id_from_stderr(text: str) -> str:
    """The CLI prints ``session_id: local-...`` on stderr at the end of a run."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.lower().startswith("session_id:"):
            value = line.split(":", 1)[1].strip()
            if value:
                return value
    return ""


def _result_metrics(event: dict) -> dict:
    """tokens / duration / session id / final text from a ``type=result`` event."""
    metrics: dict[str, Any] = {}
    if not isinstance(event, dict):
        return metrics
    session_id = event.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        metrics["session_id"] = session_id.strip()
    tokens = event.get("tokens")
    total: Any = None
    if isinstance(tokens, dict):
        total = tokens.get("total") or tokens.get("output")
    elif isinstance(tokens, (int, float)):
        total = tokens
    if isinstance(total, (int, float)):
        metrics["tokens"] = int(total)
    duration = event.get("duration_ms")
    if isinstance(duration, (int, float)):
        metrics["duration_ms"] = int(duration)
    text = event.get("text")
    if isinstance(text, str) and text.strip():
        metrics["text"] = text.strip()
    error = event.get("error")
    if isinstance(error, str) and error.strip():
        metrics["error"] = error.strip()
    return metrics


class Runner:
    """Runs one headless ``hermes chat`` turn per launch, at most N at a time."""

    def __init__(
        self,
        store_or_chatstore: Any,
        *,
        hermes_bin: Optional[str] = None,
        max_workers: int = 2,
        toolsets: tuple[str, ...] = ("file", "terminal"),
        yolo: bool = True,
        model: str = "",
        provider: str = "",
        repo: str = "",
        max_turns: int = 120,
        env: Optional[dict] = None,
        source_tag: Optional[str] = None,
    ):
        if hasattr(store_or_chatstore, "create_session"):
            self.chat = store_or_chatstore
        else:  # a plain sa_store.Store: reuse the same database file
            self.chat = sa_chats.ChatStore(getattr(store_or_chatstore, "path"))
        self.hermes_bin = hermes_bin or ""
        self.max_workers = max(1, int(max_workers or 1))
        self.toolsets = tuple(toolsets or ("file", "terminal"))
        self.yolo = bool(yolo)
        self.model = model or ""
        self.provider = provider or ""
        self.repo = repo or ""
        self.max_turns = max(1, int(max_turns or 1))
        self.env = dict(env or {})
        # The gateway only accepts source tags it advertises (cli/tool/oneshot
        # on this version), so `superagent` cannot be used here. "tool" is the
        # documented tag for third-party integrations: hidden from user lists.
        self.source_tag = (source_tag or os.environ.get("SUPERAGENT_CHAT_SOURCE") or "tool").strip() or "tool"

        self._sem = threading.Semaphore(self.max_workers)
        self._lock = threading.Lock()
        self._procs: dict[str, Any] = {}
        self._started: dict[str, float] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancelled: set[str] = set()
        self._resolved_bin: Optional[str] = None

    # -- public API ------------------------------------------------------

    def binary(self) -> str:
        """Resolved hermes executable (raises RuntimeError when missing)."""
        if not self._resolved_bin:
            self._resolved_bin = resolve_hermes_bin(self.hermes_bin)
        return self._resolved_bin

    def available(self) -> tuple[bool, str]:
        try:
            return True, self.binary()
        except RuntimeError as exc:
            return False, str(exc)

    def launch_turn(
        self,
        session_id: str,
        prompt: str,
        *,
        resume_session_id: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        repo: Optional[str] = None,
    ) -> dict:
        """Start one turn in a daemon thread; raises RuntimeError if it cannot."""
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        binary = self.binary()
        repo_path = (repo if repo is not None else self.repo) or ""
        if repo_path and not Path(repo_path).is_dir():
            raise RuntimeError(f"repository path does not exist: {repo_path}")

        session = self.chat.get_session(session_id, message_limit=0) or {}
        resume = (resume_session_id or "").strip() or None
        if resume:
            worker_prompt = prompt
        else:
            worker_prompt = build_prompt(
                prompt,
                repo=repo_path,
                session_id=session_id,
                source=session.get("source") or "desktop",
            )

        fields: dict[str, Any] = {"status": sa_chats.QUEUED, "error": None, "pid": None}
        if model:
            fields["model"] = model
        elif self.model:
            fields["model"] = self.model
        if provider:
            fields["provider"] = provider
        elif self.provider:
            fields["provider"] = self.provider
        self.chat.update_session(session_id, **fields)
        self.chat.add_event(session_id, "queued", "waiting for a worker slot")

        thread = threading.Thread(
            target=self._run_turn,
            args=(session_id, worker_prompt),
            kwargs={
                "binary": binary,
                "resume": resume,
                "model": model or self.model,
                "provider": provider or self.provider,
                "repo": repo_path,
            },
            name=f"superagent-{session_id}",
            daemon=True,
        )
        with self._lock:
            self._threads[session_id] = thread
            self._started[session_id] = time.time()
        thread.start()
        return {"session_id": session_id, "resume": bool(resume), "command": binary}

    def stop(self, session_id: str) -> bool:
        """Cancel a running (or queued) turn. Returns True if a process was killed."""
        with self._lock:
            self._cancelled.add(session_id)
            proc = self._procs.get(session_id)
            thread = self._threads.get(session_id)
            if proc is None and (thread is None or not thread.is_alive()):
                self._cancelled.discard(session_id)  # nothing will consume the flag
        killed = False
        if proc is not None and proc.poll() is None:
            _terminate_tree(proc)
            killed = True
        self.chat.update_session(
            session_id,
            status=sa_chats.CANCELLED,
            error="cancelled by user",
            finished_at=time.time(),
            pid=None,
        )
        self.chat.add_event(session_id, "cancelled", "stop requested")
        return killed

    def active(self) -> list[dict]:
        """Sessions with a live process, ready for a status chip or grid."""
        with self._lock:
            items = []
            for session_id, proc in self._procs.items():
                if proc is None or proc.poll() is not None:
                    continue
                items.append(
                    {
                        "session_id": session_id,
                        "pid": getattr(proc, "pid", None),
                        "started_at": self._started.get(session_id),
                    }
                )
        return items

    # -- internals -------------------------------------------------------

    def _run_turn(
        self,
        session_id: str,
        prompt: str,
        *,
        binary: str,
        resume: Optional[str],
        model: str,
        provider: str,
        repo: str,
    ) -> None:
        with self._sem:
            if self._take_cancelled(session_id):
                self._finish(session_id, sa_chats.CANCELLED, error="cancelled before start")
                return
            try:
                self._execute(
                    session_id,
                    prompt,
                    binary=binary,
                    resume=resume,
                    model=model,
                    provider=provider,
                    repo=repo,
                )
            except Exception as exc:  # never leak a dead thread
                self._finish(session_id, sa_chats.FAILED, error=f"runner error: {exc}")

    def _execute(
        self,
        session_id: str,
        prompt: str,
        *,
        binary: str,
        resume: Optional[str],
        model: str,
        provider: str,
        repo: str,
    ) -> None:
        argv = self._build_argv(
            binary, prompt, resume_session_id=resume, model=model, provider=provider, repo=repo
        )
        self.chat.add_event(session_id, "started", " ".join(argv))

        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in self.env.items()})
        stderr_lines: list[str] = []
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=repo or None,
                env=env,
            )
        except Exception as exc:
            self._finish(session_id, sa_chats.FAILED, error=f"cannot start hermes: {exc}")
            return

        with self._lock:
            self._procs[session_id] = proc
        # Register the process before flipping to `running`, so a `running`
        # session always has a pid and shows up in active().
        self.chat.update_session(
            session_id, status=sa_chats.RUNNING, started_at=time.time(), pid=getattr(proc, "pid", None)
        )

        reader = threading.Thread(
            target=_drain_stderr, args=(proc, stderr_lines), name=f"superagent-stderr-{session_id}", daemon=True
        )
        reader.start()

        exit_code: Optional[int] = None
        read_error = ""
        result: dict = {}
        buffer: list[str] = []
        wrote_text = False

        def flush_text() -> None:
            """Turn the buffered ``text`` deltas into one assistant message."""
            nonlocal wrote_text
            if not buffer:
                return
            text = "".join(buffer).strip()
            buffer.clear()
            if text:
                self.chat.add_message(session_id, "assistant", text)
                wrote_text = True

        try:
            for line in proc.stdout:
                event = parse_event(line)
                if event is None:
                    continue
                kind = str(event.get("type") or "")
                if kind == "system":
                    self._on_system(session_id, event)
                elif kind == "text":
                    fragment = event.get("text")
                    if isinstance(fragment, str) and fragment:
                        buffer.append(fragment)
                elif kind in ("assistant", "message"):
                    text = _text_of(event)
                    if text:
                        flush_text()
                        self.chat.add_message(session_id, "assistant", text)
                        wrote_text = True
                elif kind in ("tool_use", "tool"):
                    flush_text()
                    self.chat.add_event(session_id, "tool", _tool_detail(event))
                elif kind == "tool_result":
                    if event.get("is_error"):
                        flush_text()
                        self.chat.add_event(session_id, "tool_error", _tool_detail(event))
                elif kind == "result":
                    result = event
                elif kind == "error":
                    flush_text()
                    self.chat.add_event(session_id, "error", _text_of(event) or str(event.get("error") or ""))
        except Exception as exc:
            read_error = str(exc)
        finally:
            try:
                exit_code = proc.wait(timeout=10)
            except Exception:
                exit_code = None
            reader.join(timeout=2)
        flush_text()

        metrics = _result_metrics(result)
        fields: dict[str, Any] = {"finished_at": time.time()}
        if metrics.get("session_id"):
            fields["hermes_session_id"] = metrics["session_id"]
        if metrics.get("tokens") is not None:
            fields["tokens"] = metrics["tokens"]
        if metrics.get("duration_ms") is not None:
            fields["duration_ms"] = metrics["duration_ms"]
        if not wrote_text and metrics.get("text"):
            self.chat.add_message(session_id, "assistant", metrics["text"])

        stderr_tail = "".join(stderr_lines)
        stderr_session_id = _session_id_from_stderr(stderr_tail)
        if stderr_session_id and not fields.get("hermes_session_id"):
            fields["hermes_session_id"] = stderr_session_id

        cancelled = self._take_cancelled(session_id)
        if cancelled:
            status = sa_chats.CANCELLED
            error = "cancelled by user"
        elif read_error:
            status = sa_chats.FAILED
            error = f"could not read worker output: {read_error}"
        elif exit_code == 0:
            status = sa_chats.REVIEW
            error = None
        else:
            tail = _stderr_summary(stderr_tail)
            status = sa_chats.FAILED
            reason = metrics.get("error") or tail or f"hermes exited with code {exit_code}"
            error = reason[-STDERR_LIMIT:]

        if metrics.get("tokens") is not None or metrics.get("duration_ms") is not None:
            self.chat.add_event(
                session_id,
                "result",
                f"exit={exit_code} tokens={metrics.get('tokens')} duration_ms={metrics.get('duration_ms')}",
            )
        self._finish(session_id, status, error=error, **fields)

    def _on_system(self, session_id: str, event: dict) -> None:
        fields: dict[str, Any] = {}
        hermes_session_id = event.get("session_id")
        if isinstance(hermes_session_id, str) and hermes_session_id.strip():
            fields["hermes_session_id"] = hermes_session_id.strip()
        model = event.get("model")
        if isinstance(model, str) and model.strip():
            fields["model"] = model.strip()
        if fields:
            self.chat.update_session(session_id, **fields)
        self.chat.add_event(session_id, "system", str(event.get("subtype") or "init"))

    def _build_argv(
        self,
        binary: str,
        prompt: str,
        *,
        resume_session_id: Optional[str] = None,
        model: str = "",
        provider: str = "",
        repo: str = "",
    ) -> list[str]:
        argv = [
            binary,
            "chat",
            "-q",
            prompt,
            "-Q",
            "--format",
            "stream-json",
            "--source",
            self.source_tag,
            "-t",
            ",".join(self.toolsets),
        ]
        if self.yolo:
            argv.append("--yolo")
        if self.max_turns:
            argv += ["--max-turns", str(int(self.max_turns))]
        if model:
            argv += ["-m", model]
        if provider:
            argv += ["--provider", provider]
        if repo:
            argv += ["--in", repo]
        if resume_session_id:
            argv += ["--resume", resume_session_id]
        return argv

    def _finish(self, session_id: str, status: str, *, error: Optional[str] = None, **extra: Any) -> None:
        fields = {"status": status, "error": error, "pid": None}
        fields.update(extra)
        # Write the event first: once the new status is visible, its audit
        # trail already contains the line that explains it.
        self.chat.add_event(session_id, status, (error or status)[:500])
        self.chat.update_session(session_id, **fields)
        with self._lock:
            self._procs.pop(session_id, None)
            self._threads.pop(session_id, None)
            self._started.pop(session_id, None)

    def _take_cancelled(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._cancelled:
                self._cancelled.discard(session_id)
                return True
        return False
