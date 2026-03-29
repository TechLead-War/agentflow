from __future__ import annotations

import unittest

from agentflow.models import AgentType, Task, TaskComplexity
from agentflow.prompts import PromptBuilder


class AgentPromptTests(unittest.TestCase):
    def test_round1_prompt_includes_review_bar_and_system_understanding(self):
        task = Task(
            id="demo-task",
            title="Add feature",
            spec="Update the feature implementation.",
            rationale="Needed for user-facing behavior.",
            files=["app/feature.py"],
            depends_on=[],
            complexity=TaskComplexity.FEATURE,
            agent=AgentType.CODEX,
            reviewer=AgentType.CLAUDE,
        )

        prompt = PromptBuilder.build_agent_prompt(task, feedback=None, round_num=1)

        self.assertIn("# System Understanding", prompt)
        self.assertIn("# Ownership", prompt)
        self.assertIn("# Review Bar", prompt)
        self.assertIn("Does it run/build correctly?", prompt)
        self.assertIn("VERIFY YOUR OWN WORK", prompt)
        self.assertIn("NEVER SUBMIT GARBAGE", prompt)


if __name__ == "__main__":
    unittest.main()
