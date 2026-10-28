"""Concrete Antigravity integration adapters conforming to the AntigravityAdapter contract."""

import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import time
from typing import Any, Optional

from .base import AntigravityAdapter
from ..models import TaskExecutionSpec, TaskExecutionResult, VerificationResult


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _parse_cmd(cmd_str: str) -> list[str]:
    """Parse shell command string safely preserving quoted arguments."""
    try:
        return shlex.split(cmd_str, posix=(os.name != "nt"))
    except Exception:
        return cmd_str.split()


def _run_shell_cmd(cmd: list[str] | str, cwd: Path | None = None, timeout: int = 60) -> tuple[int, str, str]:
    """Execute a shell command with timeout and return (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    venv_scripts = REPO_ROOT / "backend" / ".venv" / "Scripts"
    if venv_scripts.exists():
        env["PATH"] = f"{venv_scripts};{env.get('PATH', '')}"

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd or REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            shell=isinstance(cmd, str),
            env=env,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


class AntigravitySDKAdapter(AntigravityAdapter):
    """Adapter invoking the official Google Antigravity Python SDK (google-antigravity)."""

    def is_available(self) -> tuple[bool, str]:
        try:
            import google.antigravity  # type: ignore # noqa: F401
        except ImportError:
            return (
                False,
                "The 'google-antigravity' Python package is not installed. "
                "Install it with: pip install google-antigravity",
            )

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return (
                False,
                "GEMINI_API_KEY environment variable is not set. "
                "Obtain a key from https://aistudio.google.com/app/api-keys",
            )

        return True, "google-antigravity SDK is installed and GEMINI_API_KEY is configured."

    def execute_task(self, spec: TaskExecutionSpec) -> TaskExecutionResult:
        ready, reason = self.is_available()
        if not ready:
            return TaskExecutionResult(
                success=False,
                task_id=spec.task_id,
                attempt_number=spec.attempt_number,
                error_message=f"SDK unavailable: {reason}",
                agent_output="",
            )

        # In real SDK mode: initialize Agent with LocalAgentConfig and run
        try:
            # Dynamic import when available
            from google.antigravity import Agent, LocalAgentConfig, CapabilitiesConfig  # type: ignore

            prompt = (
                f"You are the autonomous engineer for EduSense task {spec.task_id} ({spec.title}).\n"
                f"Milestone: {spec.milestone}\n"
                f"Allowed files: {spec.affected_files}\n"
                f"Review feedback (if any): {spec.review_feedback or 'None'}\n"
                f"Execute changes strictly within scope, then run validation commands: {spec.verification_commands}\n"
            )

            # Note: synchronous wrapper for async context if running from synchronous runner
            import asyncio

            async def _run():
                config = LocalAgentConfig(
                    system_instructions="You are an autonomous engineering agent for EduSense.",
                    capabilities=CapabilitiesConfig(),
                )
                async with Agent(config) as agent:
                    resp = await agent.chat(prompt)
                    tokens = []
                    async for token in resp:
                        tokens.append(token)
                    return "".join(tokens)

            output = asyncio.run(_run())

            # Run declared verification commands
            ver_results: list[VerificationResult] = []
            all_passed = True
            for v_cmd in spec.verification_commands:
                code, stdout, stderr = _run_shell_cmd(v_cmd)
                passed = (code == 0)
                if not passed:
                    all_passed = False
                ver_results.append(
                    VerificationResult(
                        command=v_cmd,
                        exit_code=code,
                        stdout=stdout,
                        stderr=stderr,
                        passed=passed,
                    )
                )

            return TaskExecutionResult(
                success=all_passed,
                task_id=spec.task_id,
                attempt_number=spec.attempt_number,
                modified_files=spec.affected_files,
                verification_results=ver_results,
                agent_output=output,
            )
        except Exception as e:
            return TaskExecutionResult(
                success=False,
                task_id=spec.task_id,
                attempt_number=spec.attempt_number,
                error_message=f"Antigravity SDK execution error: {e}",
            )


class AntigravityCLIAdapter(AntigravityAdapter):
    """Adapter invoking the headless Antigravity CLI binary ('agy')."""

    def is_available(self) -> tuple[bool, str]:
        cli_path = shutil.which("agy") or shutil.which("agy.exe")
        if not cli_path:
            return (
                False,
                "Antigravity CLI ('agy') executable is not found in system PATH. "
                "Ensure agy is installed and included in your PATH environment variable.",
            )
        return True, f"Antigravity CLI found at {cli_path}"

    def execute_task(self, spec: TaskExecutionSpec) -> TaskExecutionResult:
        ready, reason = self.is_available()
        if not ready:
            return TaskExecutionResult(
                success=False,
                task_id=spec.task_id,
                attempt_number=spec.attempt_number,
                error_message=f"CLI unavailable: {reason}",
            )

        # Execute headless CLI
        cmd = ["agy", "run", "--prompt", f"Execute task {spec.task_id}: {spec.title}"]
        code, stdout, stderr = _run_shell_cmd(cmd, timeout=300)
        if code != 0:
            return TaskExecutionResult(
                success=False,
                task_id=spec.task_id,
                attempt_number=spec.attempt_number,
                error_message=f"agy CLI exited with code {code}: {stderr}",
                agent_output=stdout,
            )

        # Run verification commands
        ver_results: list[VerificationResult] = []
        all_passed = True
        for v_cmd in spec.verification_commands:
            v_code, v_stdout, v_stderr = _run_shell_cmd(v_cmd)
            passed = (v_code == 0)
            if not passed:
                all_passed = False
            ver_results.append(
                VerificationResult(
                    command=v_cmd,
                    exit_code=v_code,
                    stdout=v_stdout,
                    stderr=v_stderr,
                    passed=passed,
                )
            )

        return TaskExecutionResult(
            success=all_passed,
            task_id=spec.task_id,
            attempt_number=spec.attempt_number,
            modified_files=spec.affected_files,
            verification_results=ver_results,
            agent_output=stdout,
        )


class SimulatedAdapter(AntigravityAdapter):
    """Safe simulation adapter used for dummy task testing and end-to-end verification."""

    def __init__(self, should_fail: bool = False, fail_on_attempt: int | None = None) -> None:
        self.should_fail = should_fail
        self.fail_on_attempt = fail_on_attempt

    def is_available(self) -> tuple[bool, str]:
        return True, "Simulated adapter ready (controlled test mode)."

    def execute_task(self, spec: TaskExecutionSpec) -> TaskExecutionResult:
        if self.should_fail or (self.fail_on_attempt is not None and spec.attempt_number == self.fail_on_attempt):
            return TaskExecutionResult(
                success=False,
                task_id=spec.task_id,
                attempt_number=spec.attempt_number,
                error_message=f"Simulated execution failure on attempt {spec.attempt_number}",
                agent_output="Task failed as commanded by simulation flag.",
            )

        # Write or touch affected files for harmless dummy tasks
        for fpath in spec.affected_files:
            full_path = REPO_ROOT / fpath
            full_path.parent.mkdir(parents=True, exist_ok=True)
            content = f"# Autonomous Task Execution Artifact\n# Task: {spec.task_id}\n# Attempt: {spec.attempt_number}\n# Feedback: {spec.review_feedback or 'None'}\n"
            full_path.write_text(content, encoding="utf-8")

        # Run declared verification commands
        ver_results: list[VerificationResult] = []
        all_passed = True
        for v_cmd in spec.verification_commands:
            v_code, v_stdout, v_stderr = _run_shell_cmd(v_cmd)
            passed = (v_code == 0)
            if not passed:
                all_passed = False
            ver_results.append(
                VerificationResult(
                    command=v_cmd,
                    exit_code=v_code,
                    stdout=v_stdout,
                    stderr=v_stderr,
                    passed=passed,
                )
            )

        return TaskExecutionResult(
            success=all_passed,
            task_id=spec.task_id,
            attempt_number=spec.attempt_number,
            modified_files=spec.affected_files,
            verification_results=ver_results,
            agent_output=f"Task {spec.task_id} successfully executed in simulated mode.",
        )


class AgentAPIAdapter(AntigravityAdapter):
    """Adapter invoking Antigravity Agent API ('agentapi') to launch and monitor real conversations in the App."""

    def __init__(
        self,
        project_id: Optional[str] = None,
        agentapi_exe: Optional[str] = None,
        wait_for_completion: bool = True,
        timeout: int = 600,
        on_conversation_started: Optional[Any] = None,
    ) -> None:
        self.project_id = project_id or os.getenv("ANTIGRAVITY_PROJECT_ID") or "c5449a58-9379-419b-88fd-4b5b1f0d6b00"
        self.agentapi_exe = agentapi_exe or os.getenv("ANTIGRAVITY_AGENTAPI_EXE") or self._find_agentapi_exe()
        self.wait_for_completion = wait_for_completion
        self.timeout = timeout
        self.on_conversation_started = on_conversation_started

    def _find_agentapi_exe(self) -> Optional[str]:
        cand_exe = Path(os.path.expanduser("~")) / "AppData" / "Local" / "Programs" / "antigravity" / "resources" / "bin" / "language_server.exe"
        if cand_exe.exists():
            return str(cand_exe)
        cand_bat = Path(os.path.expanduser("~")) / ".gemini" / "antigravity" / "bin" / "agentapi.bat"
        if cand_bat.exists():
            return str(cand_bat)
        which = shutil.which("agentapi") or shutil.which("agentapi.bat")
        if which:
            return which
        return None

    def is_available(self) -> tuple[bool, str]:
        if not self.agentapi_exe or not os.path.exists(self.agentapi_exe):
            return (
                False,
                f"Antigravity Agent API executable ('language_server.exe' or 'agentapi') not found at {self.agentapi_exe}",
            )
        try:
            cmd = [self.agentapi_exe]
            if self.agentapi_exe.endswith("language_server.exe"):
                cmd.append("agentapi")
            cmd.append("--help")
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if proc.returncode == 0:
                return True, f"Antigravity Agent API available at {self.agentapi_exe} (project: {self.project_id})"
            return False, f"agentapi check failed with code {proc.returncode}: {proc.stderr}"
        except Exception as e:
            return False, f"Failed to invoke agentapi: {e}"

    def build_task_prompt(self, spec: TaskExecutionSpec) -> str:
        prompt = (
            f"You are the autonomous engineering agent assigned to implement task `{spec.task_id}` for EduSense.\n\n"
            f"# Task Details: {spec.task_id} — {spec.title}\n"
            f"- Workspace Root: `d:/LENOVO/Documents/Capstone 1/EduSence`\n"
            f"- Milestone: {spec.milestone}\n"
            f"- Category: {spec.category}\n"
            f"- Dependencies: {spec.dependencies}\n"
            f"- Declared Affected Files: {spec.affected_files}\n"
        )
        if spec.review_feedback:
            prompt += f"\n## Reviewer Feedback to address:\n{spec.review_feedback}\n"
        prompt += (
            f"\n## Strict Engineering Rules:\n"
            f"1. Modify ONLY the declared affected files: {spec.affected_files}.\n"
            f"2. Follow EduSense architecture rules in AGENTS.md, .agents/rules/security.md, and .agents/rules/architecture.md.\n"
            f"3. Add complete tests and verify that the declared verification commands pass:\n"
        )
        for cmd in spec.verification_commands:
            prompt += f"   - `{cmd}`\n"
        prompt += (
            f"4. Do NOT push, merge, or delete files. Conclude your turn with a concise summary of changes and test evidence.\n"
        )
        return prompt

    def execute_task(self, spec: TaskExecutionSpec) -> TaskExecutionResult:
        ready, diag = self.is_available()
        if not ready or not self.agentapi_exe:
            return TaskExecutionResult(
                success=False,
                task_id=spec.task_id,
                attempt_number=spec.attempt_number,
                error_message=diag,
            )

        convo_id = spec.conversation_id
        if not convo_id:
            prompt = self.build_task_prompt(spec)
            title = f"[EduSense] {spec.task_id}: {spec.title}"

            cmd = [self.agentapi_exe]
            if self.agentapi_exe.endswith("language_server.exe"):
                cmd.append("agentapi")
            cmd.extend(["new-conversation", f"--title={title}", prompt])

            env = {
                **os.environ,
                "ANTIGRAVITY_PROJECT_ID": self.project_id,
            }

            try:
                proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
                if proc.returncode != 0:
                    return TaskExecutionResult(
                        success=False,
                        task_id=spec.task_id,
                        attempt_number=spec.attempt_number,
                        error_message=f"agentapi exited with code {proc.returncode}: {proc.stderr}",
                    )

                data = json.loads(proc.stdout)
                convo_id = data.get("response", {}).get("newConversation", {}).get("conversationId")
                if not convo_id:
                    return TaskExecutionResult(
                        success=False,
                        task_id=spec.task_id,
                        attempt_number=spec.attempt_number,
                        error_message=f"No conversationId in agentapi response: {proc.stdout}",
                    )

                print(f"[AgentAPI] Started conversation {convo_id} for task {spec.task_id} in project {self.project_id}.")
            except Exception as e:
                return TaskExecutionResult(
                    success=False,
                    task_id=spec.task_id,
                    attempt_number=spec.attempt_number,
                    error_message=f"Failed to launch conversation via agentapi: {e}",
                )
        else:
            print(f"[AgentAPI] Reusing existing conversation {convo_id} for task {spec.task_id} (idempotent resume).")

        if callable(self.on_conversation_started):
            try:
                self.on_conversation_started(convo_id)
            except Exception as cb_err:
                print(f"[AgentAPI] Callback error: {cb_err}")

        if not self.wait_for_completion:
            return TaskExecutionResult(
                success=True,
                task_id=spec.task_id,
                attempt_number=spec.attempt_number,
                conversation_id=convo_id,
                modified_files=spec.affected_files,
                agent_output=f"Conversation launched: {convo_id}",
            )

        try:
            # Monitor conversation until complete (CASCADE_RUN_STATUS_IDLE or transcript DONE)
            db_path = Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"
            transcript_path = (
                Path.home()
                / ".gemini"
                / "antigravity"
                / "brain"
                / convo_id
                / ".system_generated"
                / "logs"
                / "transcript.jsonl"
            )

            is_done = False
            last_agent_text = ""
            start_poll = time.time()
            time.sleep(6)  # Give agent conversation initial spin-up window

            while time.time() - start_poll < self.timeout:
                time.sleep(5)
                # Check 1: canonical status from conversation_summaries.db
                if db_path.exists():
                    try:
                        con = sqlite3.connect(str(db_path))
                        cur = con.cursor()
                        cur.execute(
                            "SELECT status, not_fully_idle, step_count FROM conversation_summaries WHERE conversation_id = ?",
                            (convo_id,),
                        )
                        row = cur.fetchone()
                        con.close()
                        if row:
                            st, not_idle, steps = row[0], row[1], row[2]
                            if st == "CASCADE_RUN_STATUS_IDLE" and not_idle == 0 and steps > 2:
                                is_done = True
                                break
                    except Exception:
                        pass

                # Check 2: transcript completion (final response without pending tool calls)
                if transcript_path.exists():
                    try:
                        lines = transcript_path.read_text(encoding="utf-8").strip().splitlines()
                        if lines:
                            last_entry = json.loads(lines[-1])
                            has_tools = bool(last_entry.get("tool_calls"))
                            is_planner = (last_entry.get("type") == "PLANNER_RESPONSE")
                            if is_planner and not has_tools and last_entry.get("status") == "DONE":
                                # Agent completed its final turn
                                is_done = True
                                last_agent_text = last_entry.get("content", "")
                                break
                    except Exception:
                        pass

            if not is_done:
                return TaskExecutionResult(
                    success=False,
                    task_id=spec.task_id,
                    attempt_number=spec.attempt_number,
                    conversation_id=convo_id,
                    error_message=f"Agent execution timed out after {self.timeout}s in conversation {convo_id}.",
                )

            # Run declared verification commands
            ver_results: list[VerificationResult] = []
            all_passed = True
            for v_cmd in spec.verification_commands:
                v_code, v_stdout, v_stderr = _run_shell_cmd(v_cmd)
                passed = (v_code == 0)
                if not passed:
                    all_passed = False
                ver_results.append(
                    VerificationResult(
                        command=v_cmd,
                        exit_code=v_code,
                        stdout=v_stdout,
                        stderr=v_stderr,
                        passed=passed,
                    )
                )

            return TaskExecutionResult(
                success=all_passed,
                task_id=spec.task_id,
                attempt_number=spec.attempt_number,
                conversation_id=convo_id,
                modified_files=spec.affected_files,
                verification_results=ver_results,
                agent_output=last_agent_text or f"Agent completed conversation {convo_id}.",
            )

        except Exception as e:
            return TaskExecutionResult(
                success=False,
                task_id=spec.task_id,
                attempt_number=spec.attempt_number,
                error_message=str(e),
            )


# Backward-compatible alias
AntigravityAgentApiAdapter = AgentAPIAdapter


class InteractiveFallbackAdapter(AntigravityAdapter):
    """Fallback adapter reporting precise missing setup when no supported integration is installed."""

    def is_available(self) -> tuple[bool, str]:
        return (
            False,
            "Neither Google Antigravity Python SDK ('google-antigravity') nor "
            "Antigravity CLI ('agy') is installed in this environment.\n"
            "To enable autonomous task execution, please:\n"
            "  1. Install SDK: pip install google-antigravity\n"
            "  2. Configure GEMINI_API_KEY in .env\n"
            "  OR install Antigravity CLI (agy) and add to PATH.\n"
            "For harmless lifecycle validation without external SDKs, run with: --dummy",
        )

    def execute_task(self, spec: TaskExecutionSpec) -> TaskExecutionResult:
        _, diagnostic = self.is_available()
        return TaskExecutionResult(
            success=False,
            task_id=spec.task_id,
            attempt_number=spec.attempt_number,
            error_message=f"Execution blocked: Antigravity runtime unavailable.\n{diagnostic}",
            agent_output="",
        )


def get_antigravity_adapter(mode: str = "auto", **kwargs: Any) -> AntigravityAdapter:
    """Factory creating the appropriate Antigravity adapter."""
    if mode == "simulated" or mode == "dummy":
        return SimulatedAdapter(**kwargs)
    elif mode == "agentapi":
        return AgentAPIAdapter(**kwargs)
    elif mode == "sdk":
        return AntigravitySDKAdapter()
    elif mode == "cli":
        return AntigravityCLIAdapter()

    # auto mode: check AgentAPI first (official Antigravity App mechanism)
    agentapi = AgentAPIAdapter(**kwargs)
    if agentapi.is_available()[0]:
        return agentapi

    sdk = AntigravitySDKAdapter()
    if sdk.is_available()[0]:
        return sdk

    cli = AntigravityCLIAdapter()
    if cli.is_available()[0]:
        return cli

    return InteractiveFallbackAdapter()
