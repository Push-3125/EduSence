"""Concrete Antigravity integration adapters conforming to the AntigravityAdapter contract."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Any

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
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd or REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            shell=isinstance(cmd, str),
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
    elif mode == "sdk":
        return AntigravitySDKAdapter()
    elif mode == "cli":
        return AntigravityCLIAdapter()

    # auto mode: check SDK then CLI, fallback to InteractiveFallbackAdapter
    sdk = AntigravitySDKAdapter()
    if sdk.is_available()[0]:
        return sdk

    cli = AntigravityCLIAdapter()
    if cli.is_available()[0]:
        return cli

    return InteractiveFallbackAdapter()
