import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace.defects4j_tools import generate_prompts


class JoernJavaTests(unittest.TestCase):
    def generate(self, environment):
        child_environments = []

        def run_joern(command, **kwargs):
            child_environments.append((command[0], kwargs["env"]))
            if command[0] == "joern-export":
                output = Path(command[-1])
                output.mkdir()
                (output / "export.dot").write_text("digraph {}", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as tmp:
            java_file = Path(tmp) / "Example.java"
            java_file.write_text("class Example {}", encoding="utf-8")
            with patch.dict(os.environ, environment, clear=True):
                original = dict(os.environ)
                with patch.object(generate_prompts.subprocess, "run", side_effect=run_joern), \
                     patch.object(generate_prompts.compressor, "compress_cfg_pdg", return_value="CFG/PDG"):
                    self.assertEqual(generate_prompts.generate_cfg_pdg(java_file), "CFG/PDG")
                self.assertEqual(dict(os.environ), original)
            self.assertEqual((java_file.parent / "cpg/compressed.txt").read_text(), "CFG/PDG")
        self.assertEqual([name for name, _ in child_environments], ["joern-parse", "joern-export"])
        return [env for _, env in child_environments]

    def test_joern_uses_own_java_without_changing_defects4j_environment(self):
        for env in self.generate({"JAVA_HOME": "/opt/java11", "JOERN_JAVA_HOME": "/opt/java21",
                                  "PATH": "/opt/java11/bin:/usr/bin", "OTHER_SETTING": "keep"}):
            self.assertEqual(env["JAVA_HOME"], "/opt/java21")
            self.assertEqual(env["PATH"], "/opt/java21/bin:/opt/java11/bin:/usr/bin")
            self.assertEqual(env["OTHER_SETTING"], "keep")

    def test_without_override_preserves_custom_java_environment(self):
        original = {"JAVA_HOME": "/custom/jdk", "PATH": "/custom/jdk/bin:/usr/bin"}
        for env in self.generate(original):
            self.assertEqual(env, original)

    def test_empty_override_preserves_custom_java_environment(self):
        original = {"JAVA_HOME": "/custom/jdk", "PATH": "/usr/bin", "JOERN_JAVA_HOME": ""}
        for env in self.generate(original):
            self.assertEqual(env, original)


if __name__ == "__main__":
    unittest.main()
