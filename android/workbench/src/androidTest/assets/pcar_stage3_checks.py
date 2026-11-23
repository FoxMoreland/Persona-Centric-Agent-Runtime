"""Run existing transport-double coverage on Android's embedded Python."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

root = Path(test_root).resolve(strict=True)
tempfile.tempdir = str(root)
os.environ["PERSONA_RUNTIME_HOME"] = str(root / "runtime")
sys.path.insert(0, str(root))
names = [
    "tests.test_import_prompts",
    "tests.test_import_history_service",
    "tests.test_import_history_memory",
    "tests.test_lookup_target.TargetRoute.test_standard_authorization_never_sends_context_to_new_model",
    "tests.test_lookup_target.TargetRoute.test_lookup_across_chat_and_reopen",
]
suite = unittest.defaultTestLoader.loadTestsFromNames(names)
trace = io.StringIO()
summary = unittest.TextTestRunner(stream=trace, verbosity=1).run(suite)
result = json.dumps({
    "passed": summary.wasSuccessful(), "tests": summary.testsRun, "skipped": len(summary.skipped),
    "errors": [case.id() for case, _ in summary.errors],
    "failures": [case.id() for case, _ in summary.failures],
    "details": trace.getvalue() if not summary.wasSuccessful() else "",
}, ensure_ascii=True)

