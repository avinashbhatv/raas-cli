from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from salt_config_cli.cli.main import cli
from salt_config_cli.core.repositories import RepositorySource
from salt_config_cli.services.git_repository import ContentWorkspaceService, GitRepositoryService


def _git_repo(path: Path, files: dict[str, str]) -> Path:
    if not shutil.which("git"):
        pytest.skip("git executable is required")
    for relative, text in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "SCC Tests"], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True)
    return path


@pytest.fixture()
def vcf_repositories(tmp_path: Path) -> tuple[Path, Path]:
    states = _git_repo(
        tmp_path / "saltext-vcf",
        {
            "vcf-infra/cluster-drs/cluster-drs.sls": "drs-ready:\n  test.nop:\n    - name: ready\n",
            "vcf-infra/cluster-drs/map.jinja": "{% set config = salt['pillar.get']('cluster_drs', {}) %}\n",
            "vcf-infra/cluster-drs/defaults.yaml": "cluster_drs:\n  enabled: false\n",
        },
    )
    values = _git_repo(
        tmp_path / "customer-values",
        {
            "prod/9.1.1/cluster-drs/values.yaml": (
                "cluster_drs:\n"
                "  enabled: true\n"
                "  automation_level: fullyAutomated\n"
            )
        },
    )
    return states, values


def test_folder_values_layout_and_raas_state_path(vcf_repositories: tuple[Path, Path], tmp_path: Path) -> None:
    states_path, values_path = vcf_repositories
    service = GitRepositoryService(tmp_path / "cache")
    states = service.sync(
        "vcf-salt",
        RepositorySource(
            kind="states",
            url=str(states_path),
            ref="main",
            root="vcf-infra",
            layout="{resource}",
        ),
    )
    values = service.sync(
        "customer-values",
        RepositorySource(
            kind="data",
            url=str(values_path),
            ref="main",
            layout="{environment}/{version}/{resource}/values.yaml",
        ),
    )

    package = ContentWorkspaceService(tmp_path / "work").build(
        "cluster-drs",
        states,
        data_repository=values,
        environment="prod",
        version="9.1.1",
    )

    assert package.states_repository_path == "vcf-infra/cluster-drs"
    assert package.data_repository_path == "prod/9.1.1/cluster-drs/values.yaml"
    assert package.data_file and package.data_file.name == "values.yaml"
    assert package.state_entrypoint == "cluster-drs/cluster-drs.sls"


def test_deploy_executes_directly_without_creating_saved_job(
    vcf_repositories: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    states_path, values_path = vcf_repositories
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("SCC_THEME", "plain")
    runner = CliRunner()

    for args in (
        [
            "repo", "add", "vcf-salt", "--kind", "states", "--url", str(states_path),
            "--root", "vcf-infra", "--layout", "{resource}", "--default",
        ],
        [
            "repo", "add", "customer-values", "--kind", "data", "--url", str(values_path),
            "--layout", "{environment}/{version}/{resource}/values.yaml", "--default",
        ],
    ):
        result = runner.invoke(cli, args)
        assert result.exit_code == 0, result.output

    main_module = importlib.import_module("salt_config_cli.cli.main")
    calls: list[tuple[str, dict]] = []

    def fake_upload(**kwargs):
        calls.append(("upload", kwargs))

    def fake_run(**kwargs):
        calls.append(("run", kwargs))

    def fake_job_create(**kwargs):
        calls.append(("job-create", kwargs))

    def fake_pillar(**kwargs):
        calls.append(("persistent-pillar", kwargs))

    monkeypatch.setattr(main_module.upload_file, "callback", fake_upload)
    monkeypatch.setattr(main_module.run_state, "callback", fake_run)
    monkeypatch.setattr(main_module.job_create, "callback", fake_job_create)
    monkeypatch.setattr(main_module.upload_pillar, "callback", fake_pillar)

    result = runner.invoke(
        cli,
        [
            "deploy", "cluster-drs",
            "--environment", "prod",
            "--version", "9.1.1",
            "--mode", "dry-run",
            "--target-group", "prod-vcenters",
            "--work-dir", str(tmp_path / "work"),
            "--yes",
            "--no-show-tree",
        ],
    )
    assert result.exit_code == 0, result.output

    names = [name for name, _ in calls]
    assert names == ["upload", "run"]
    upload = calls[0][1]
    run = calls[1][1]
    assert upload["path"] == "/vcf-infra/cluster-drs"
    assert run["state_file"] == "/vcf-infra/cluster-drs/cluster-drs.sls"
    assert run["target_group"] == "prod-vcenters"
    assert run["test"] is True
    assert run["pillar_file"].endswith("/data/values.yaml")
    assert "direct state.apply" in result.output
    assert "Saved job: not created" in result.output


def test_save_job_is_explicit_safe_and_contains_no_customer_values(
    vcf_repositories: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    states_path, values_path = vcf_repositories
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("SCC_THEME", "plain")
    runner = CliRunner()

    for args in (
        [
            "repo", "add", "vcf-salt", "--kind", "states", "--url", str(states_path),
            "--root", "vcf-infra", "--layout", "{resource}", "--default",
        ],
        [
            "repo", "add", "customer-values", "--kind", "data", "--url", str(values_path),
            "--layout", "{environment}/{version}/{resource}/values.yaml", "--default",
        ],
    ):
        result = runner.invoke(cli, args)
        assert result.exit_code == 0, result.output

    main_module = importlib.import_module("salt_config_cli.cli.main")
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(main_module.upload_file, "callback", lambda **kwargs: calls.append(("upload", kwargs)))
    monkeypatch.setattr(main_module.run_state, "callback", lambda **kwargs: calls.append(("run", kwargs)))
    monkeypatch.setattr(main_module.upload_pillar, "callback", lambda **kwargs: calls.append(("pillar", kwargs)))
    monkeypatch.setattr(main_module.job_create, "callback", lambda **kwargs: calls.append(("job-create", kwargs)))

    result = runner.invoke(
        cli,
        [
            "deploy", "cluster-drs",
            "--environment", "prod",
            "--version", "9.1.1",
            "--mode", "apply",
            "--target-group", "prod-vcenters",
            "--save-job",
            "--job-name", "cluster-drs-prod",
            "--work-dir", str(tmp_path / "work"),
            "--yes",
            "--no-show-tree",
        ],
    )
    assert result.exit_code == 0, result.output

    by_name = {name: kwargs for name, kwargs in calls}
    assert list(name for name, _ in calls) == ["upload", "job-create", "run"]
    saved = by_name["job-create"]
    assert saved["kwargs"] == ("test=False",)
    assert saved["pillars"] == ()
    assert saved["target_group"] == "prod-vcenters"
    assert by_name["run"]["test"] is False
    assert by_name["run"]["pillar_file"].endswith("/data/values.yaml")


def test_deploy_help_exposes_direct_execution_and_optional_saved_job() -> None:
    result = CliRunner().invoke(cli, ["--theme", "plain", "deploy", "--help"])
    assert result.exit_code == 0, result.output
    assert "--save-job / --no-save-job" in result.output
    assert "direct test=True" in result.output
    assert "--create-job" not in result.output


def test_run_state_uses_dotted_reference_and_runtime_pillar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    values_file = tmp_path / "values.yaml"
    values_file.write_text("cluster_drs:\n  enabled: true\n", encoding="utf-8")
    main_module = importlib.import_module("salt_config_cli.cli.main")
    route_payloads: list[dict] = []
    file_checks = 0

    class FakeClient:
        def call(self, resource: str, method: str, **kwargs):
            nonlocal file_checks
            if (resource, method) == ("fs", "file_exists"):
                file_checks += 1
                return SimpleNamespace(success=True, ret=file_checks >= 2, error=None)
            if (resource, method) == ("cmd", "route_cmd"):
                route_payloads.append(kwargs)
                return SimpleNamespace(success=True, ret="202608010001", error=None)
            raise AssertionError(f"Unexpected call: {resource}.{method} {kwargs}")

        def close(self) -> None:
            return None

    monkeypatch.setattr(main_module, "connect_client", lambda settings, label=None: FakeClient())
    monkeypatch.setattr(
        main_module,
        "_list_target_groups",
        lambda client: [
            {
                "name": "prod-vcenters",
                "tgt": {"*": {"tgt": "G@site:prod", "tgt_type": "compound"}},
            }
        ],
    )
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    result = CliRunner().invoke(
        cli,
        [
            "--theme", "plain",
            "run", "/vcf-infra/cluster-drs/cluster-drs.sls",
            "--target-group", "prod-vcenters",
            "--env", "vcf",
            "--pillar-file", str(values_file),
            "--async",
            "--server", "https://raas.example.test",
            "--username", "automation-user",
            "--password", "secret",
        ],
    )
    assert result.exit_code == 0, result.output
    assert file_checks == 2
    assert len(route_payloads) == 1
    payload = route_payloads[0]
    assert payload["fun"] == "state.apply"
    assert payload["arg"]["arg"] == ["vcf-infra.cluster-drs.cluster-drs"]
    assert payload["arg"]["kwarg"]["test"] is True
    assert payload["arg"]["kwarg"]["pillar"] == {"cluster_drs": {"enabled": True}}
