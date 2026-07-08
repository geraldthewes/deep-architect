from __future__ import annotations

import shlex
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pathspec

from deep_architect.logger import get_logger
from deep_architect.models.checks import CheckProfile, LLMRulesConfig, QualityChecksConfig

logger = get_logger(__name__)

QUALITY_CHECKS_FILENAME = ".quality-checks.toml"

# Tail of combined stdout+stderr kept per command result.
_OUTPUT_CAP = 4000

_AUTODETECT_MAX_DEPTH = 3
_AUTODETECT_SKIP_DIRS = frozenset({".venv", "venv", "node_modules", "build", "dist", ".git"})

# Fixed order controls the emitted command order for auto-detected profiles.
_TOOL_COMMAND_MAP: dict[str, str] = {
    "ruff": "ruff check {files}",
    "mypy": "mypy {files}",
    "black": "black --check {files}",
    "bandit": "bandit {files}",
}


def load_quality_checks(
    repo_root: Path, override: Path | None = None, default_timeout: int = 120
) -> QualityChecksConfig:
    """Load .quality-checks.toml; fall back to auto-detection when absent."""
    config_path = override or (repo_root / QUALITY_CHECKS_FILENAME)
    if not config_path.exists():
        return autodetect_checks(repo_root, default_timeout=default_timeout)

    try:
        with config_path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Malformed {config_path}: {exc}") from exc

    llm_rules_raw = raw.get("llm_rules")
    try:
        config = QualityChecksConfig(
            profiles=[CheckProfile.model_validate(p) for p in raw.get("profile", [])],
            llm_rules=LLMRulesConfig.model_validate(llm_rules_raw) if llm_rules_raw else None,
        )
    except Exception as exc:
        raise ValueError(f"Invalid {config_path}: {exc}") from exc

    logger.info(
        "Loaded %d quality-check profile(s) from %s", len(config.profiles), config_path
    )
    return config


def autodetect_checks(repo_root: Path, default_timeout: int = 120) -> QualityChecksConfig:
    """Build file-scoped profiles from pyproject.toml files (nice-to-have fallback)."""
    profiles: list[CheckProfile] = []
    for pyproject_path in _find_pyprojects(repo_root):
        profile = _profile_from_pyproject(repo_root, pyproject_path, timeout=default_timeout)
        if profile is not None:
            profiles.append(profile)

    if profiles:
        logger.info(
            "auto-detected checks from %d pyproject.toml file(s); tests not "
            "auto-detected — create %s to declare them",
            len(profiles),
            QUALITY_CHECKS_FILENAME,
        )

    llm_rules = (
        LLMRulesConfig() if (repo_root / ".opencodereview" / "rule.json").exists() else None
    )

    return QualityChecksConfig(profiles=profiles, llm_rules=llm_rules, auto_detected=True)


def match_profiles(
    config: QualityChecksConfig, files: list[Path], repo_root: Path
) -> dict[str, list[Path]]:
    """Map profile name -> modified files matching its globs (pathspec gitwildmatch)."""
    resolved_root = repo_root.resolve()
    rel_files: list[tuple[Path, Path]] = []
    for f in files:
        try:
            rel = f.resolve().relative_to(resolved_root)
        except ValueError:
            continue
        rel_files.append((f, rel))

    matched: dict[str, list[Path]] = {}
    for profile in config.profiles:
        spec = pathspec.PathSpec.from_lines("gitignore", profile.paths)
        hits = [orig for orig, rel in rel_files if spec.match_file(rel.as_posix())]
        if hits:
            matched[profile.name] = hits

    return matched


def _find_pyprojects(repo_root: Path) -> list[Path]:
    """Walk repo_root for pyproject.toml files, skipping hidden/build dirs."""
    found: list[Path] = []

    def _walk(directory: Path, depth: int) -> None:
        if depth > _AUTODETECT_MAX_DEPTH:
            return
        pyproject = directory / "pyproject.toml"
        if pyproject.is_file():
            found.append(pyproject)
        for child in sorted(directory.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith(".") or child.name in _AUTODETECT_SKIP_DIRS:
                continue
            _walk(child, depth + 1)

    _walk(repo_root, 0)
    return found


def _declared_tools(raw: dict[str, object]) -> set[str]:
    """Return tool names present via a [tool.X] section or a dependency entry."""
    tools: set[str] = set()

    tool_section = raw.get("tool", {})
    if isinstance(tool_section, dict):
        tools.update(name for name in _TOOL_COMMAND_MAP if name in tool_section)

    dep_strings: list[str] = []
    project = raw.get("project", {})
    if isinstance(project, dict):
        dep_strings.extend(project.get("dependencies", []))
        optional = project.get("optional-dependencies", {})
        if isinstance(optional, dict):
            for group in optional.values():
                dep_strings.extend(group)

    dependency_groups = raw.get("dependency-groups", {})
    if isinstance(dependency_groups, dict):
        for group in dependency_groups.values():
            dep_strings.extend(group)

    for dep in dep_strings:
        dep_lower = dep.strip().lower()
        tools.update(name for name in _TOOL_COMMAND_MAP if dep_lower.startswith(name))

    return tools


def _profile_from_pyproject(
    repo_root: Path, pyproject_path: Path, timeout: int = 120
) -> CheckProfile | None:
    """Emit one file-scoped profile per pyproject.toml with detected tool config."""
    try:
        with pyproject_path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        logger.warning("Skipping malformed %s: %s", pyproject_path, exc)
        return None

    tools = _declared_tools(raw)
    if not tools:
        return None

    commands = [_TOOL_COMMAND_MAP[name] for name in _TOOL_COMMAND_MAP if name in tools]

    directory = pyproject_path.parent
    rel_dir = directory.relative_to(repo_root)
    is_root = rel_dir == Path(".")
    paths = ["**"] if is_root else [f"{rel_dir.as_posix()}/**"]
    name = repo_root.name if is_root else rel_dir.as_posix()

    return CheckProfile(name=name, paths=paths, commands=commands, timeout=timeout)


# ---------------------------------------------------------------------------
# Programmatic check runner + baseline diff
# ---------------------------------------------------------------------------


@dataclass
class CheckFailure:
    """One failing command from a matched profile."""

    profile: str
    command: str          # as declared (may still contain {files})
    returncode: int
    output: str            # tail of combined stdout+stderr, capped
    pre_existing: bool = False  # True when present in baseline (advisory only)


@dataclass
class CheckBaseline:
    """Pre-fix snapshot: '<profile>::<command>' -> (returncode, output)."""

    results: dict[str, tuple[int, str]]


def _baseline_key(profile: str, command: str) -> str:
    return f"{profile}::{command}"


def _repo_relative_files(files: list[Path], repo_root: Path) -> list[Path]:
    resolved_root = repo_root.resolve()
    rel: list[Path] = []
    for f in files:
        try:
            rel.append(f.resolve().relative_to(resolved_root))
        except ValueError:
            rel.append(f)
    return rel


def _expand_command(command: str, files: list[Path]) -> str:
    if "{files}" not in command:
        return command
    file_str = " ".join(shlex.quote(str(f)) for f in files)
    return command.replace("{files}", file_str)


def _run_command(command: str, files: list[Path], repo_root: Path, timeout: int) -> tuple[int, str]:
    """Expand {files}, run the command, and return (returncode, capped output).

    A spawn error or timeout is treated as a failure (fail-closed) rather than
    swallowed — both are logged and surfaced as a non-zero returncode.
    """
    expanded = _expand_command(command, files)
    try:
        argv = shlex.split(expanded)
    except ValueError as exc:
        logger.error("Failed to parse quality-check command %r: %s", expanded, exc)
        return (1, f"failed to parse command: {exc}")

    try:
        # Commands come from the repo's own declared .quality-checks.toml (or
        # auto-detected pyproject.toml config) — equivalent trust level to a
        # Justfile the repo already runs. shell=False, no untrusted input.
        result = subprocess.run(  # nosec B603
            argv,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return (result.returncode, output[-_OUTPUT_CAP:])
    except subprocess.TimeoutExpired:
        logger.error("Quality-check command timed out after %ds: %s", timeout, expanded)
        return (1, f"command timed out after {timeout}s: {expanded}")
    except OSError as exc:
        logger.error("Quality-check command failed to spawn: %s (%s)", expanded, exc)
        return (1, f"failed to spawn command '{expanded}': {exc}")


def run_checks(
    matched: dict[str, list[Path]],
    config: QualityChecksConfig,
    repo_root: Path,
) -> list[CheckFailure]:
    """Run every command of every matched profile; return all failures."""
    profiles_by_name = {p.name: p for p in config.profiles}
    failures: list[CheckFailure] = []

    for profile_name, files in matched.items():
        profile = profiles_by_name[profile_name]
        rel_files = _repo_relative_files(files, repo_root)
        for command in profile.commands:
            returncode, output = _run_command(command, rel_files, repo_root, profile.timeout)
            if returncode != 0:
                failures.append(
                    CheckFailure(
                        profile=profile_name,
                        command=command,
                        returncode=returncode,
                        output=output,
                    )
                )

    return failures


def capture_baseline(
    matched: dict[str, list[Path]],
    config: QualityChecksConfig,
    repo_root: Path,
) -> CheckBaseline:
    """Run the same commands pre-fix and record outcomes."""
    profiles_by_name = {p.name: p for p in config.profiles}
    results: dict[str, tuple[int, str]] = {}

    for profile_name, files in matched.items():
        profile = profiles_by_name[profile_name]
        rel_files = _repo_relative_files(files, repo_root)
        for command in profile.commands:
            returncode, output = _run_command(command, rel_files, repo_root, profile.timeout)
            results[_baseline_key(profile_name, command)] = (returncode, output)

    return CheckBaseline(results=results)


def new_failures(
    failures: list[CheckFailure],
    baseline: CheckBaseline,
    modified_files: list[Path],
) -> list[CheckFailure]:
    """Return only failures introduced by the fix (baseline diff).

    1. Failed post-fix, passed (or absent) in baseline -> blocks.
    2. Failed in both -> advisory (pre_existing=True) unless the post-fix
       output has new lines mentioning a modified file that baseline's
       output didn't have -> blocks.
    3. A profile with no baseline coverage at all (the agent modified files
       outside the finding's original profile) -> strict fail-closed, any
       failure blocks.
    """
    baseline_profiles = {key.split("::", 1)[0] for key in baseline.results}
    modified_names = [f.name for f in modified_files]
    blocking: list[CheckFailure] = []

    for failure in failures:
        if failure.profile not in baseline_profiles:
            logger.warning(
                "Quality check failure in profile %r has no baseline coverage "
                "(fix touched files outside the finding's profile) — blocking: %s",
                failure.profile, failure.command,
            )
            blocking.append(failure)
            continue

        baseline_result = baseline.results.get(_baseline_key(failure.profile, failure.command))
        if baseline_result is None or baseline_result[0] == 0:
            blocking.append(failure)
            continue

        baseline_output = baseline_result[1]
        baseline_lines = set(baseline_output.splitlines())
        introduced_lines = [
            line
            for line in failure.output.splitlines()
            if line not in baseline_lines and any(name in line for name in modified_names)
        ]
        if introduced_lines:
            logger.warning(
                "Quality check %r fails in both baseline and post-fix, but new "
                "lines mention modified files — blocking: %s",
                failure.command, introduced_lines[:3],
            )
            blocking.append(failure)
        else:
            failure.pre_existing = True
            logger.warning(
                "Quality check %r pre-existing failure in profile %r — advisory, not blocking",
                failure.command, failure.profile,
            )

    return blocking
