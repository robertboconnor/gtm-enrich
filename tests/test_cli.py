"""CLI: domain file parsing and the commands that must run without credentials."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from gtm_enrich.cli import app, read_domain_file

runner = CliRunner()


def test_read_csv_with_a_domain_column(tmp_path: Path) -> None:
    path = tmp_path / "d.csv"
    path.write_text("company,domain,owner\nAcme,acme.com,rob\nBeta,beta.io,sam\n")
    assert read_domain_file(path) == ["acme.com", "beta.io"]


def test_read_csv_with_a_website_column(tmp_path: Path) -> None:
    path = tmp_path / "d.csv"
    path.write_text("name,website\nAcme,https://acme.com/\n")
    assert read_domain_file(path) == ["https://acme.com/"]


def test_read_csv_without_a_recognized_header(tmp_path: Path) -> None:
    path = tmp_path / "d.csv"
    path.write_text("acme.com\nbeta.io\n")
    assert read_domain_file(path) == ["acme.com", "beta.io"]


def test_read_txt_skips_blanks_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "d.txt"
    path.write_text("# accounts\nacme.com\n\nbeta.io\n")
    assert read_domain_file(path) == ["acme.com", "beta.io"]


def test_check_runs_without_any_credentials() -> None:
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0
    assert "ICP config" in result.stdout


def test_fields_lists_salesforce_api_names() -> None:
    result = runner.invoke(app, ["fields", "--dest", "salesforce"])
    assert result.exit_code == 0
    assert "GTM_ICP_Fit_Score__c" in result.stdout


def test_run_without_domains_exits_with_usage_error() -> None:
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2
    assert "Nothing to do" in result.stdout


def test_missing_config_is_a_clean_error() -> None:
    result = runner.invoke(app, ["check", "--icp", "does/not/exist.yaml"])
    assert result.exit_code == 2
    assert "Config error" in result.stdout
