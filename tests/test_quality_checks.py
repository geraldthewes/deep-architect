"""Unit tests for deep_architect.quality_checks."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from deep_architect.models.checks import CheckProfile, LLMRulesConfig, QualityChecksConfig
from deep_architect.quality_checks import (
    CheckBaseline,
    CheckFailure,
    autodetect_checks,
    capture_baseline,
    load_quality_checks,
    match_profiles,
    new_failures,
    run_checks,
)

_PY = sys.executable

# ---------------------------------------------------------------------------
# Model round-trip
# ---------------------------------------------------------------------------


class TestModelRoundTrip:

    def test_check_profile_round_trip(self) -> None:
        profile = CheckProfile(
            name="backend", paths=["backend/**"], commands=["ruff check {files}"], timeout=60
        )
        restored = CheckProfile.model_validate(profile.model_dump())
        assert restored == profile

    def test_quality_checks_config_round_trip(self) -> None:
        config = QualityChecksConfig(
            profiles=[CheckProfile(name="root", paths=["**"], commands=["ruff check {files}"])],
            llm_rules=LLMRulesConfig(source=".opencodereview/rule.json"),
        )
        restored = QualityChecksConfig.model_validate(config.model_dump())
        assert restored == config

    def test_default_values(self) -> None:
        config = QualityChecksConfig()
        assert config.profiles == []
        assert config.llm_rules is None
        assert config.auto_detected is False


# ---------------------------------------------------------------------------
# load_quality_checks — explicit TOML
# ---------------------------------------------------------------------------


class TestLoadQualityChecks:

    def test_full_file(self, tmp_path: Path) -> None:
        (tmp_path / ".quality-checks.toml").write_text(
            """
[[profile]]
name = "backend-api"
paths = ["backend/fastapi/**"]
commands = [
  "ruff check backend/fastapi/src/",
  "mypy backend/fastapi/src/",
]

[llm_rules]
source = ".opencodereview/rule.json"
"""
        )

        config = load_quality_checks(tmp_path)

        assert config.auto_detected is False
        assert len(config.profiles) == 1
        assert config.profiles[0].name == "backend-api"
        assert config.profiles[0].paths == ["backend/fastapi/**"]
        assert config.profiles[0].commands == [
            "ruff check backend/fastapi/src/",
            "mypy backend/fastapi/src/",
        ]
        assert config.llm_rules is not None
        assert config.llm_rules.source == ".opencodereview/rule.json"

    def test_minimal_file(self, tmp_path: Path) -> None:
        (tmp_path / ".quality-checks.toml").write_text(
            """
[[profile]]
name = "root"
paths = ["**"]
commands = ["ruff check {files}"]
"""
        )

        config = load_quality_checks(tmp_path)

        assert len(config.profiles) == 1
        assert config.profiles[0].timeout == 120
        assert config.llm_rules is None

    def test_missing_llm_rules_section(self, tmp_path: Path) -> None:
        (tmp_path / ".quality-checks.toml").write_text(
            """
[[profile]]
name = "root"
paths = ["**"]
commands = ["ruff check {files}"]
"""
        )

        config = load_quality_checks(tmp_path)
        assert config.llm_rules is None

    def test_malformed_toml_raises(self, tmp_path: Path) -> None:
        (tmp_path / ".quality-checks.toml").write_text("not valid [[[ toml")

        with pytest.raises(ValueError, match="Malformed"):
            load_quality_checks(tmp_path)

    def test_invalid_profile_raises(self, tmp_path: Path) -> None:
        (tmp_path / ".quality-checks.toml").write_text(
            """
[[profile]]
name = "root"
"""
        )

        with pytest.raises(ValueError, match="Invalid"):
            load_quality_checks(tmp_path)

    def test_override_path(self, tmp_path: Path) -> None:
        custom = tmp_path / "custom.toml"
        custom.write_text(
            """
[[profile]]
name = "root"
paths = ["**"]
commands = ["ruff check {files}"]
"""
        )

        config = load_quality_checks(tmp_path, override=custom)
        assert len(config.profiles) == 1

    def test_missing_file_falls_back_to_autodetect(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            """
[tool.ruff]
line-length = 100
"""
        )

        config = load_quality_checks(tmp_path)
        assert config.auto_detected is True
        assert len(config.profiles) == 1


# ---------------------------------------------------------------------------
# autodetect_checks
# ---------------------------------------------------------------------------


class TestAutodetectChecks:

    def test_root_only_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            """
[tool.ruff]
line-length = 100

[tool.mypy]
strict = true
"""
        )

        config = autodetect_checks(tmp_path)

        assert config.auto_detected is True
        assert len(config.profiles) == 1
        profile = config.profiles[0]
        assert profile.paths == ["**"]
        assert profile.commands == ["ruff check {files}", "mypy {files}"]

    def test_nested_pyprojects_plant_tracking_shape(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            """
[tool.ruff]
line-length = 100
[tool.bandit]
"""
        )
        backend_dir = tmp_path / "backend" / "fastapi"
        backend_dir.mkdir(parents=True)
        (backend_dir / "pyproject.toml").write_text(
            """
[tool.ruff]
line-length = 100
[tool.mypy]
"""
        )
        service_dir = tmp_path / "packages" / "plant_service"
        service_dir.mkdir(parents=True)
        (service_dir / "pyproject.toml").write_text(
            """
[tool.ruff]
select = ["E", "F"]
[project]
dependencies = ["bandit>=1.8"]
"""
        )

        config = autodetect_checks(tmp_path)

        assert len(config.profiles) == 3
        names = {p.name for p in config.profiles}
        assert "backend/fastapi" in names
        assert "packages/plant_service" in names

        service_profile = next(p for p in config.profiles if p.name == "packages/plant_service")
        assert "ruff check {files}" in service_profile.commands
        assert "bandit {files}" in service_profile.commands

    def test_pyproject_with_no_tool_sections_emits_no_profile(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            """
[project]
name = "foo"
"""
        )

        config = autodetect_checks(tmp_path)
        assert config.profiles == []

    def test_skip_dirs_honored(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")
        venv_dir = tmp_path / ".venv" / "lib"
        venv_dir.mkdir(parents=True)
        (venv_dir / "pyproject.toml").write_text("[tool.ruff]\n")
        hidden_dir = tmp_path / ".hidden"
        hidden_dir.mkdir()
        (hidden_dir / "pyproject.toml").write_text("[tool.ruff]\n")

        config = autodetect_checks(tmp_path)
        assert len(config.profiles) == 1

    def test_never_autodetects_pytest(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            """
[tool.ruff]
[project]
dependencies = ["pytest>=8.0"]
"""
        )

        config = autodetect_checks(tmp_path)
        for profile in config.profiles:
            assert not any("pytest" in cmd for cmd in profile.commands)

    def test_rule_json_presence_sets_llm_rules(self, tmp_path: Path) -> None:
        ocr_dir = tmp_path / ".opencodereview"
        ocr_dir.mkdir()
        (ocr_dir / "rule.json").write_text("[]")
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")

        config = autodetect_checks(tmp_path)
        assert config.llm_rules is not None
        assert config.llm_rules.source == ".opencodereview/rule.json"

    def test_no_rule_json_leaves_llm_rules_none(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")

        config = autodetect_checks(tmp_path)
        assert config.llm_rules is None


# ---------------------------------------------------------------------------
# match_profiles
# ---------------------------------------------------------------------------


class TestMatchProfiles:

    def test_single_profile_match(self, tmp_path: Path) -> None:
        config = QualityChecksConfig(
            profiles=[CheckProfile(name="backend", paths=["backend/**"], commands=["ruff"])]
        )
        f = tmp_path / "backend" / "app.py"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n")

        matched = match_profiles(config, [f], tmp_path)
        assert matched == {"backend": [f]}

    def test_no_match_returns_empty(self, tmp_path: Path) -> None:
        config = QualityChecksConfig(
            profiles=[CheckProfile(name="backend", paths=["backend/**"], commands=["ruff"])]
        )
        f = tmp_path / "frontend" / "app.ts"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n")

        matched = match_profiles(config, [f], tmp_path)
        assert matched == {}

    def test_file_matches_two_profiles(self, tmp_path: Path) -> None:
        config = QualityChecksConfig(
            profiles=[
                CheckProfile(name="root", paths=["**"], commands=["ruff"]),
                CheckProfile(name="backend", paths=["backend/**"], commands=["mypy"]),
            ]
        )
        f = tmp_path / "backend" / "app.py"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n")

        matched = match_profiles(config, [f], tmp_path)
        assert matched == {"root": [f], "backend": [f]}

    def test_double_star_semantics(self, tmp_path: Path) -> None:
        config = QualityChecksConfig(
            profiles=[CheckProfile(name="root", paths=["src/**"], commands=["ruff"])]
        )
        f = tmp_path / "src" / "a" / "b" / "c.py"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n")

        matched = match_profiles(config, [f], tmp_path)
        assert matched == {"root": [f]}

    def test_multi_profile_multi_file(self, tmp_path: Path) -> None:
        config = QualityChecksConfig(
            profiles=[
                CheckProfile(name="a", paths=["a/**"], commands=["ruff"]),
                CheckProfile(name="b", paths=["b/**"], commands=["ruff"]),
            ]
        )
        fa = tmp_path / "a" / "x.py"
        fb = tmp_path / "b" / "y.py"
        fa.parent.mkdir(parents=True)
        fb.parent.mkdir(parents=True)
        fa.write_text("1\n")
        fb.write_text("1\n")

        matched = match_profiles(config, [fa, fb], tmp_path)
        assert matched == {"a": [fa], "b": [fb]}


# ---------------------------------------------------------------------------
# run_checks
# ---------------------------------------------------------------------------


class TestRunChecks:

    def _profile(self, command: str, timeout: int = 5) -> QualityChecksConfig:
        return QualityChecksConfig(
            profiles=[CheckProfile(name="p", paths=["**"], commands=[command], timeout=timeout)]
        )

    def test_passing_command_yields_no_failures(self, tmp_path: Path) -> None:
        config = self._profile(f'{_PY} -c "import sys; sys.exit(0)"')
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")

        failures = run_checks({"p": [f]}, config, tmp_path)
        assert failures == []

    def test_failing_command_yields_failure(self, tmp_path: Path) -> None:
        config = self._profile(f'{_PY} -c "import sys; sys.exit(1)"')
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")

        failures = run_checks({"p": [f]}, config, tmp_path)
        assert len(failures) == 1
        assert failures[0].profile == "p"
        assert failures[0].returncode == 1
        assert failures[0].pre_existing is False

    def test_timeout_is_a_failure(self, tmp_path: Path) -> None:
        config = self._profile(f'{_PY} -c "import time; time.sleep(5)"', timeout=1)
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")

        failures = run_checks({"p": [f]}, config, tmp_path)
        assert len(failures) == 1
        assert "timed out" in failures[0].output

    def test_files_placeholder_expansion(self, tmp_path: Path) -> None:
        config = self._profile(
            f'{_PY} -c "import sys; print(sys.argv[1:]); sys.exit(1)" {{files}}'
        )
        f = tmp_path / "sub" / "a.py"
        f.parent.mkdir()
        f.write_text("x = 1\n")

        failures = run_checks({"p": [f]}, config, tmp_path)
        assert len(failures) == 1
        assert "sub/a.py" in failures[0].output

    def test_output_is_capped(self, tmp_path: Path) -> None:
        config = self._profile(
            f'{_PY} -c "import sys; print(\'x\' * 10000); sys.exit(1)"'
        )
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")

        failures = run_checks({"p": [f]}, config, tmp_path)
        assert len(failures) == 1
        assert len(failures[0].output) <= 4000

    def test_no_matched_profiles_yields_no_failures(self, tmp_path: Path) -> None:
        config = self._profile(f'{_PY} -c "import sys; sys.exit(1)"')
        failures = run_checks({}, config, tmp_path)
        assert failures == []


# ---------------------------------------------------------------------------
# capture_baseline
# ---------------------------------------------------------------------------


class TestCaptureBaseline:

    def test_round_trip(self, tmp_path: Path) -> None:
        config = QualityChecksConfig(
            profiles=[
                CheckProfile(
                    name="p",
                    paths=["**"],
                    commands=[f'{_PY} -c "import sys; sys.exit(1)"'],
                )
            ]
        )
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")

        baseline = capture_baseline({"p": [f]}, config, tmp_path)
        key = f'p::{_PY} -c "import sys; sys.exit(1)"'
        assert key in baseline.results
        returncode, output = baseline.results[key]
        assert returncode == 1


# ---------------------------------------------------------------------------
# new_failures — baseline diff rules
# ---------------------------------------------------------------------------


class TestNewFailures:

    def test_rule1_new_failure_not_in_baseline_blocks(self, tmp_path: Path) -> None:
        baseline = CheckBaseline(results={"p::cmd": (0, "")})
        failure = CheckFailure(profile="p", command="cmd", returncode=1, output="boom")

        blocking = new_failures([failure], baseline, [tmp_path / "a.py"])
        assert blocking == [failure]

    def test_rule1_command_absent_from_baseline_blocks(self) -> None:
        baseline = CheckBaseline(results={})
        failure = CheckFailure(profile="p", command="cmd", returncode=1, output="boom")

        # profile IS covered by baseline (another command ran), but this exact
        # command has no baseline entry -> blocks.
        baseline = CheckBaseline(results={"p::other": (0, "")})
        blocking = new_failures([failure], baseline, [Path("a.py")])
        assert blocking == [failure]

    def test_rule2_pre_existing_failure_is_advisory(self) -> None:
        baseline = CheckBaseline(results={"p::cmd": (1, "a.py:1: error\nb.py:2: error")})
        failure = CheckFailure(
            profile="p", command="cmd", returncode=1, output="a.py:1: error\nb.py:2: error"
        )

        blocking = new_failures([failure], baseline, [Path("a.py")])
        assert blocking == []
        assert failure.pre_existing is True

    def test_rule2_new_line_mentioning_modified_file_blocks(self) -> None:
        baseline = CheckBaseline(results={"p::cmd": (1, "b.py:2: error")})
        failure = CheckFailure(
            profile="p", command="cmd", returncode=1, output="b.py:2: error\na.py:9: new error"
        )

        blocking = new_failures([failure], baseline, [Path("a.py")])
        assert blocking == [failure]
        assert failure.pre_existing is False

    def test_rule3_profile_missing_from_baseline_strict_blocks(self) -> None:
        baseline = CheckBaseline(results={"other::cmd": (1, "unrelated")})
        failure = CheckFailure(profile="p", command="cmd", returncode=1, output="boom")

        blocking = new_failures([failure], baseline, [Path("a.py")])
        assert blocking == [failure]

    def test_no_failures_returns_empty(self) -> None:
        baseline = CheckBaseline(results={"p::cmd": (0, "")})
        assert new_failures([], baseline, [Path("a.py")]) == []


# ---------------------------------------------------------------------------
# Dogfood: deep-architect's own .quality-checks.toml
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent


class TestDogfoodConfig:

    def test_load_quality_checks_returns_dogfood_profile(self) -> None:
        config = load_quality_checks(_REPO_ROOT)

        assert config.auto_detected is False
        assert len(config.profiles) == 1
        profile = config.profiles[0]
        assert profile.name == "deep-architect"
        assert profile.paths == ["deep_architect/**", "tests/**"]
        assert any("ruff check" in c for c in profile.commands)
        assert any("mypy" in c for c in profile.commands)
        assert any("bandit" in c for c in profile.commands)
        assert any("pytest" in c for c in profile.commands)

    def test_dogfood_non_test_commands_pass_for_real(self) -> None:
        """Runs the real ruff/mypy/bandit commands (not pytest, to avoid this
        test recursively re-spawning the whole suite) against deep-architect
        itself through the real run_checks()."""
        config = load_quality_checks(_REPO_ROOT)
        profile = config.profiles[0]
        non_test_commands = [c for c in profile.commands if "pytest" not in c]
        filtered_config = QualityChecksConfig(
            profiles=[
                CheckProfile(
                    name=profile.name,
                    paths=profile.paths,
                    commands=non_test_commands,
                    timeout=profile.timeout,
                )
            ]
        )
        matched = {profile.name: [_REPO_ROOT / "deep_architect" / "quality_checks.py"]}

        failures = run_checks(matched, filtered_config, _REPO_ROOT)

        assert failures == []
