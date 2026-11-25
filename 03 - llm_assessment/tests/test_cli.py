"""CLI configuration and credential loading without network requests."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_assessment


class CliTests(unittest.TestCase):
    def test_run_project_dotenv_overrides_existing_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text(
                "OPENAI_API_KEY=file-openai\n"
                "ANTHROPIC_API_KEY='file-anthropic'\n"
                "MISTRAL_API_KEY=file-mistral\n",
                encoding="utf-8",
            )
            with patch.object(run_assessment, "__file__", str(root / "run_assessment.py")), \
                 patch.dict(os.environ, {
                     "OPENAI_API_KEY": "shell-openai",
                     "ANTHROPIC_API_KEY": "shell-anthropic",
                     "MISTRAL_API_KEY": "shell-mistral",
                 }, clear=True), \
                 patch.object(run_assessment.sys, "argv", ["run_assessment.py", "run"]), \
                 patch.object(run_assessment, "run", return_value={}) as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_assessment.main(), 0)
                run.assert_called_once_with("config.json", None, None, False)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "file-openai")
                self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "file-anthropic")
                self.assertEqual(os.environ["MISTRAL_API_KEY"], "file-mistral")

    def test_run_uses_environment_for_keys_absent_from_project_dotenv(self):
        for file_contents in (None, "ANTHROPIC_API_KEY=file-anthropic\n"):
            with self.subTest(dotenv_present=file_contents is not None), \
                 tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                if file_contents is not None:
                    (root / ".env").write_text(file_contents, encoding="utf-8")
                with patch.object(run_assessment, "__file__", str(root / "run_assessment.py")), \
                     patch.dict(os.environ, {
                         "OPENAI_API_KEY": "shell-openai",
                         "ANTHROPIC_API_KEY": "shell-anthropic",
                         "MISTRAL_API_KEY": "shell-mistral",
                     }, clear=True), \
                     patch.object(run_assessment.sys, "argv", ["run_assessment.py", "run"]), \
                     patch.object(run_assessment, "run", return_value={}) as run, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(run_assessment.main(), 0)
                    run.assert_called_once_with("config.json", None, None, False)
                    self.assertEqual(os.environ["OPENAI_API_KEY"], "shell-openai")
                    self.assertEqual(os.environ["MISTRAL_API_KEY"], "shell-mistral")
                    self.assertEqual(
                        os.environ["ANTHROPIC_API_KEY"],
                        "file-anthropic" if file_contents is not None else "shell-anthropic",
                    )

    def test_run_empty_dotenv_key_does_not_reuse_environment_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
            with patch.object(run_assessment, "__file__", str(root / "run_assessment.py")), \
                 patch.dict(os.environ, {"OPENAI_API_KEY": "shell-openai"}, clear=True), \
                 patch.object(run_assessment.sys, "argv", ["run_assessment.py", "run"]), \
                 patch.object(run_assessment, "run", return_value={}) as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_assessment.main(), 0)
                run.assert_called_once_with("config.json", None, None, False)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "")

    def test_explicit_config_overrides_default(self):
        with patch.object(run_assessment.sys, "argv", ["run_assessment.py", "analyse", "--config", "other.json"]), \
             patch.object(run_assessment, "analyse", return_value={"model_summary": []}) as analyse, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_assessment.main(), 0)
            analyse.assert_called_once_with("other.json")


if __name__ == "__main__":
    unittest.main()
