"""
Eval runner for the Companies House MCP server.

For each task in tasks.json:
  1. Spawn server.py as a subprocess.
  2. Connect via MCP client, list its tools.
  3. Feed those tools to Claude via the Anthropic API.
  4. Run an agentic loop: Claude picks a tool -> we execute it via MCP -> feed
     result back -> repeat until Claude produces a final text answer.
  5. Grade:
       - "verifiable" tasks: assert on substrings + expected tools used
       - "judged" tasks: LLM-as-judge on a rubric
  6. Print a per-task result and an aggregate score.

Requires two env vars:
    ANTHROPIC_API_KEY         (for driving/judging)
    COMPANIES_HOUSE_API_KEY   (for the server to hit the real API)

Run with:
    .venv/bin/python evals/eval_runner.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).parent.parent
TASKS_FILE = Path(__file__).parent / "tasks.json"
DRIVER_MODEL = "claude-opus-4-7"     # driver: the model actually using the tools
JUDGE_MODEL = "claude-opus-4-7"      # judge: grades open-ended answers
MAX_TURNS = 12                       # cap agentic loop to avoid runaways


@dataclass
class TaskResult:
    task_id: str
    passed: bool
    reason: str
    final_answer: str
    tools_used: list[str] = field(default_factory=list)
    turn_count: int = 0


def _mcp_tools_to_anthropic_schema(tools: list) -> list[dict]:
    """Convert MCP tool definitions into the shape the Anthropic API expects."""
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema,
        }
        for t in tools
    ]


async def run_task(session: ClientSession, tools_schema: list[dict], task_text: str) -> tuple[str, list[str], int]:
    """Run the agentic loop for one task. Returns (final_answer, tools_used, turns)."""
    client = Anthropic()
    messages: list[dict[str, Any]] = [{"role": "user", "content": task_text}]
    tools_used: list[str] = []

    for turn in range(MAX_TURNS):
        response = client.messages.create(
            model=DRIVER_MODEL,
            max_tokens=2048,
            tools=tools_schema,
            messages=messages,
        )

        # Append assistant reply to conversation
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            final = "".join(b.text for b in response.content if b.type == "text")
            return final, tools_used, turn + 1

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tools_used.append(block.name)
                try:
                    result = await session.call_tool(block.name, block.input)
                    # Flatten tool result content to text for feeding back
                    text_parts = [c.text for c in result.content if hasattr(c, "text")]
                    payload = "\n".join(text_parts) if text_parts else "(no textual content)"
                except Exception as e:
                    payload = f"Tool error: {e}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": payload[:8000],  # cap to avoid runaway context
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        # Any other stop reason: bail out
        final = "".join(b.text for b in response.content if b.type == "text")
        return final, tools_used, turn + 1

    return "Hit MAX_TURNS without finishing.", tools_used, MAX_TURNS


def grade_verifiable(task: dict, answer: str, tools_used: list[str]) -> tuple[bool, str]:
    lower = answer.lower()
    if "must_contain_all" in task:
        missing = [s for s in task["must_contain_all"] if s.lower() not in lower]
        if missing:
            return False, f"answer missing required substrings: {missing}"
    if "must_contain_any" in task:
        if not any(s.lower() in lower for s in task["must_contain_any"]):
            return False, f"answer missing any of: {task['must_contain_any']}"
    if "expected_tools_include" in task:
        missing = [t for t in task["expected_tools_include"] if t not in tools_used]
        if missing:
            return False, f"expected tools not called: {missing}"
    return True, "all checks passed"


def grade_judged(task: dict, answer: str) -> tuple[bool, str]:
    client = Anthropic()
    prompt = f"""You are grading an AI assistant's answer against a rubric.

TASK: {task["task"]}

RUBRIC: {task["rubric"]}

ANSWER:
{answer}

Reply with a JSON object only, no other text:
{{"passed": true|false, "reason": "<one-sentence explanation>"}}"""
    response = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    # Strip markdown fences if the judge added them
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        verdict = json.loads(raw)
        return bool(verdict.get("passed")), str(verdict.get("reason", ""))
    except json.JSONDecodeError:
        return False, f"could not parse judge response: {raw[:200]}"


async def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("COMPANIES_HOUSE_API_KEY"):
        print("ERROR: COMPANIES_HOUSE_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    tasks = json.loads(TASKS_FILE.read_text())
    print(f"Running {len(tasks)} tasks against {DRIVER_MODEL}\n")

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "server.py")],
        env={**os.environ},
    )

    results: list[TaskResult] = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            tools_schema = _mcp_tools_to_anthropic_schema(mcp_tools)
            print(f"Server exposes {len(mcp_tools)} tools\n")

            for task in tasks:
                print(f"[{task['id']}] {task['task'][:80]}...")
                try:
                    answer, tools_used, turns = await run_task(
                        session, tools_schema, task["task"]
                    )
                except Exception as e:
                    results.append(TaskResult(task["id"], False, f"exception: {e}", ""))
                    print(f"  FAIL (exception): {e}\n")
                    continue

                if task["type"] == "verifiable":
                    passed, reason = grade_verifiable(task, answer, tools_used)
                elif task["type"] == "judged":
                    passed, reason = grade_judged(task, answer)
                else:
                    passed, reason = False, f"unknown task type: {task['type']}"

                results.append(TaskResult(
                    task["id"], passed, reason, answer, tools_used, turns
                ))
                verdict = "PASS" if passed else "FAIL"
                print(f"  {verdict} ({turns} turns, tools: {tools_used})")
                print(f"  reason: {reason}\n")

    # Summary
    print("=" * 60)
    passed = sum(1 for r in results if r.passed)
    print(f"RESULTS: {passed}/{len(results)} passed")
    for r in results:
        mark = "✓" if r.passed else "✗"
        print(f"  {mark} {r.task_id}: {r.reason}")


if __name__ == "__main__":
    asyncio.run(main())
