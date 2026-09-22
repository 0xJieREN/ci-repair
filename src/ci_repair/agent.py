"""Separate "the agent believes it is done" from "the system ends the repair".

The upstream mini-SWE-agent loop is kept. Two deterministic hooks are added outside the
model's control: a submission gate (the submit sentinel is only a *request*) and a
verifier probe after each step that changed the patch, which ends the session as soon as
fresh-environment verification passes. Limits report which budget ran out.
"""

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import InterruptAgentFlow, LimitsExceeded, Submitted, TimeExceeded


class AgentExit(str, Enum):
    """Why the agent loop ended."""

    SUBMITTED = "SUBMITTED"  # agent requested submission and the gate accepted it
    SUBMITTED_NO_PATCH = "SUBMITTED_NO_PATCH"
    EARLY_STOP_VERIFIED = "EARLY_STOP_VERIFIED"  # system stopped: verification passed
    SUBMISSION_REJECTED = "SUBMISSION_REJECTED"  # too many rejected submissions
    STEP_LIMIT = "STEP_LIMIT"
    COST_LIMIT = "COST_LIMIT"
    WALL_TIME_LIMIT = "WALL_TIME_LIMIT"
    FORMAT_ERROR = "FORMAT_ERROR"
    ERROR = "ERROR"


class StopReason(str, Enum):
    """Final, system-level reason a repair attempt ended."""

    VERIFIED_PASS = "VERIFIED_PASS"
    NO_PATCH = "NO_PATCH"
    POLICY_DENIED = "POLICY_DENIED"
    BASELINE_NOT_REPRODUCED = "BASELINE_NOT_REPRODUCED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    UNSUPPORTED_ENVIRONMENT = "UNSUPPORTED_ENVIRONMENT"
    STALE_SOURCE = "STALE_SOURCE"
    STEP_LIMIT = "STEP_LIMIT"
    COST_LIMIT = "COST_LIMIT"
    WALL_TIME_LIMIT = "WALL_TIME_LIMIT"
    EXECUTION_ERROR = "EXECUTION_ERROR"


LIMIT_EXITS = {
    AgentExit.STEP_LIMIT: StopReason.STEP_LIMIT,
    AgentExit.COST_LIMIT: StopReason.COST_LIMIT,
    AgentExit.WALL_TIME_LIMIT: StopReason.WALL_TIME_LIMIT,
}


def final_stop_reason(
    *, verified: bool, policy_denied: bool, patch: bool, agent_exit
) -> StopReason:
    """Outcome first; a budget exit only explains an unsuccessful repair."""
    if verified:
        return StopReason.VERIFIED_PASS
    if policy_denied:
        return StopReason.POLICY_DENIED
    try:
        exit_value = AgentExit(agent_exit)
    except ValueError:
        exit_value = None
    if exit_value in LIMIT_EXITS:
        return LIMIT_EXITS[exit_value]
    if not patch:
        return StopReason.NO_PATCH
    return StopReason.VERIFICATION_FAILED


@dataclass(frozen=True)
class GateResult:
    accept: bool
    feedback: str = ""
    exit: AgentExit = AgentExit.SUBMITTED


def _exit(reason: AgentExit, content: str = "") -> dict:
    return {
        "role": "exit",
        "content": content or reason.value,
        "extra": {"exit_status": reason.value, "submission": ""},
    }


class RepairAgent(DefaultAgent):
    def __init__(
        self,
        *args,
        gate: Callable[[str], GateResult],
        early_stop: bool = True,
        max_rejected_submissions: int = 2,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.gate = gate
        self.early_stop = early_stop
        self.max_rejected_submissions = max_rejected_submissions
        self.steps_executed = 0
        self.submissions = 0
        self.rejected_submissions = 0
        self.command_seconds = 0.0

    def query(self) -> dict:
        # Upstream folds step and cost into one LimitsExceeded; reports need which one.
        if 0 < self.config.step_limit <= self.n_calls:
            raise LimitsExceeded(_exit(AgentExit.STEP_LIMIT))
        if 0 < self.config.cost_limit <= self.cost:
            raise LimitsExceeded(_exit(AgentExit.COST_LIMIT))
        if 0 < self.config.wall_time_limit_seconds <= int(time.time() - self._start_time):
            raise TimeExceeded(_exit(AgentExit.WALL_TIME_LIMIT))
        return super().query()

    def execute_actions(self, message: dict) -> list[dict]:
        outputs = []
        for action in message.get("extra", {}).get("actions", []):
            started = time.monotonic()
            try:
                output = self.env.execute(action)
            except Submitted:
                output = None
            # Agent command time only; gate verification time is accounted by the orchestrator.
            self.command_seconds += time.monotonic() - started
            if output is not None:
                outputs.append(output)
                continue
            self.submissions += 1
            result = self.gate("submit")
            if result.accept:
                raise InterruptAgentFlow(_exit(result.exit))
            self.rejected_submissions += 1
            if self.rejected_submissions >= self.max_rejected_submissions:
                raise InterruptAgentFlow(_exit(AgentExit.SUBMISSION_REJECTED))
            outputs.append({"output": result.feedback, "returncode": 1, "exception_info": ""})
        self.steps_executed += 1
        messages = self.add_messages(
            *self.model.format_observation_messages(message, outputs, self.get_template_vars())
        )
        if self.early_stop:
            result = self.gate("probe")
            if result.accept:
                raise InterruptAgentFlow(_exit(AgentExit.EARLY_STOP_VERIFIED))
        return messages

    def exit_status(self) -> str:
        status = self.messages[-1].get("extra", {}).get("exit_status", "") if self.messages else ""
        return AgentExit.FORMAT_ERROR.value if status == "RepeatedFormatError" else status

    def models_used(self) -> list[str]:
        names = set()
        for message in self.messages:
            response = message.get("extra", {}).get("response", {})
            if isinstance(response, dict) and isinstance(response.get("model"), str):
                names.add(response["model"])
        return sorted(names)
