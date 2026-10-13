"""Guard the public MCP agent guidance against brittle query recipes."""

import ast
from pathlib import Path
import unittest


class AgentWorkflowInstructionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "server.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "INSTRUCTIONS"
                    for target in node.targets)
        )
        cls.instructions = ast.literal_eval(assignment.value)

    def test_instructions_describe_evidence_workflow_and_limits(self):
        for phrase in (
            "natural language",
            "find_papers",
            "search_research_paper",
            "get_paper",
            "read the relevant section",
            "get_code_or_math_spec",
            "compare_methods",
            "get_research_bundle",
            "validate_project",
            "limitations",
            "inference",
            "Abstract-only",
            "never reconstruct or invent",
            "or probabilities",
            "avoid repeated",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.instructions)

    def test_instructions_do_not_require_repeated_rephrasings(self):
        self.assertNotIn("two or three", self.instructions)


if __name__ == "__main__":
    unittest.main()
