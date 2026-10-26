from app import log_parser
from app.models import FailureCategory


def test_clean_strips_ansi_and_sections():
    raw = "section_start:1700000000:step_script\r\x1b[0K\x1b[31mERROR\x1b[0m boom\r\n"
    assert log_parser.clean(raw).strip() == "ERROR boom"


def test_excerpt_keeps_error_window_and_tail():
    lines = [f"noise line {i}" for i in range(5000)]
    lines[1000] = "Traceback (most recent call last):"
    out = log_parser.excerpt("\n".join(lines), max_chars=20_000)
    assert "Traceback" in out
    assert "noise line 4999" in out
    assert "[snip]" in out
    assert len(out) <= 20_100


def test_classify_categories():
    assert log_parser.classify("FAILED tests/test_a.py::test_x - AssertionError") == FailureCategory.TEST_FAILURE
    assert log_parser.classify("ERROR: ResolutionImpossible: conflicting") == FailureCategory.DEPENDENCY_CONFLICT
    assert log_parser.classify("Killed\nexit code 137") == FailureCategory.MEMORY
    assert log_parser.classify("ERROR: Job failed (system failure): no space left on device") == FailureCategory.INFRA_FLAKE
    assert log_parser.classify("Error: Objects have changed outside of Terraform") == FailureCategory.DEPLOYMENT_DRIFT
    assert log_parser.classify("all good") == FailureCategory.UNKNOWN


def test_referenced_paths_ignores_third_party_and_strips_build_dir():
    log = (
        'File "/usr/lib/python3.12/site-packages/x.py", line 3\n'
        'File "/builds/group/app/src/parser.py", line 42, in parse\n'
        "tests/test_parser.py:17: AssertionError\n"
    )
    paths = log_parser.referenced_paths(log)
    assert "src/parser.py" in paths
    assert "tests/test_parser.py" in paths
    assert not any("site-packages" in p for p in paths)
