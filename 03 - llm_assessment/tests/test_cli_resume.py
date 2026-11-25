"""Check that resume is explicit, non-destructive and cannot create a new run."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_assessment


class ResumeCliTests(unittest.TestCase):
    def invoke(self, *, prepared=True, remaining=0, provider=None):
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            root = Path(temporary)
            audit = root / "results/_audit"
            audit.mkdir(parents=True)
            if prepared:
                (audit / "run_manifest.json").write_text("{}")
            (audit / "resume_status.json").write_text(json.dumps({"providers": {
                "openai": {"remaining": remaining}, "mistral": {"remaining": 1}}}))
            args = ["run_assessment.py", "resume", "--config", "frozen.json"]
            if provider:
                args.extend(["--provider", provider])
            stack.enter_context(patch("sys.argv", args))
            stack.enter_context(patch.object(run_assessment, "load_config", return_value=({"assessment_mode": "case", "output_dir": "results"}, root)))
            credentials = stack.enter_context(patch.object(run_assessment, "_load_credentials"))
            run = stack.enter_context(patch.object(run_assessment, "run", return_value={"processed": 0}))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            status = run_assessment.main()
            return status, run.call_args, credentials.call_count

    def test_resume_retries_explicitly_and_can_limit_one_provider(self):
        status, call, credentials = self.invoke(provider="openai")
        self.assertEqual(status, 0)
        self.assertEqual(call.args, ("frozen.json", "openai", None, True))
        self.assertEqual(credentials, 1)

    def test_unfinished_requested_providers_return_a_distinct_status(self):
        self.assertEqual(self.invoke()[0], 3)
        self.assertEqual(self.invoke(provider="openai", remaining=1)[0], 3)

    def test_resume_cannot_prepare_a_new_experiment_or_load_credentials_for_it(self):
        status, call, credentials = self.invoke(prepared=False)
        self.assertEqual((status, call, credentials), (2, None, 0))

    def test_resume_cannot_delete_outputs(self):
        with patch("sys.argv", ["run_assessment.py", "--delete", "resume"]), patch.object(run_assessment, "clear_outputs") as delete:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                run_assessment.main()
            delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
