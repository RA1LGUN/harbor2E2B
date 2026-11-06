#!/usr/bin/env python3
"""Read a native Harbor trial without turning unexecuted/error results into zero."""
import argparse
import hashlib
import json
import math
import sys
from datetime import datetime
from pathlib import Path


def finite_reward(value):
    return type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1


def inspect(path, reward_key="reward", expected=None):
    path = Path(path).resolve()
    result = json.loads((path / "result.json").read_text())
    summary = {"trial": str(path), "status": "UNVERIFIED", "reward": None}
    exception = result.get("exception_info")
    if exception:
        kind = exception.get("exception_type", "")
        status = ("VERIFIER_ERROR" if "Verifier" in kind or "Reward" in kind else
                  "ENVIRONMENT_ERROR" if "Environment" in kind or "Build" in kind else
                  "AGENT_ERROR" if "Agent" in kind else "EXECUTION_ERROR")
        if status != "AGENT_ERROR":
            return {**summary, "status": status, "exception_info": exception}
        # A later, successfully completed verifier can still grade an interrupted agent.
        summary["exception_info"] = exception
    timing = result.get("verifier") or {}
    if not timing.get("started_at") or not timing.get("finished_at"):
        return {**summary, "reason": "Verifier completion timing is missing"}
    try:
        if datetime.fromisoformat(timing["finished_at"]) < datetime.fromisoformat(timing["started_at"]):
            return {**summary, "reason": "Verifier finish precedes its start"}
    except (ValueError, TypeError):
        return {**summary, "reason": "Verifier timing is invalid"}
    rewards = (result.get("verifier_result") or {}).get("rewards", {})
    value = rewards.get(reward_key)
    if not finite_reward(value):
        return {**summary, "reason": "Selected reward is missing or invalid"}
    captures = []
    raw_values = []
    for name in ("reward.txt", "reward.json"):
        file = path / "verifier" / name
        if not file.is_file():
            continue
        raw = file.read_bytes()
        doc = float(raw.decode().strip()) if name.endswith("txt") else json.loads(raw)
        raw_value = doc.get(reward_key) if isinstance(doc, dict) else doc
        if not finite_reward(raw_value):
            return {**summary, "reason": "Invalid raw reward file: " + name}
        raw_values.append(raw_value)
        captures.append({"file": name, "sha256": hashlib.sha256(raw).hexdigest()})
    if not captures or any(raw != value for raw in raw_values):
        return {**summary, "reason": "Raw reward missing or inconsistent with result.json"}
    summary.update(status="SCORED_WITH_AGENT_ERROR" if exception else "SCORED",
                   reward=value, reward_key=reward_key, captures=captures)
    if expected is not None:
        summary.update(expected_reward=expected, expected_reward_matches=value == expected)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trial", type=Path)
    parser.add_argument("--reward-key", default="reward")
    parser.add_argument("--expected-reward", type=float, choices=[0, 1])
    args = parser.parse_args()
    try:
        summary = inspect(args.trial, args.reward_key, args.expected_reward)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        summary = {"status": "UNVERIFIED", "reward": None, "reason": str(error)}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "SCORED" and summary.get("expected_reward_matches", True) else 1


if __name__ == "__main__":
    sys.exit(main())
