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


def test_env_file_is_loaded_and_shown(tmp_path: Path, monkeypatch) -> None:
    """The .env file has to actually reach os.environ — it silently did not, once."""
    import gtm_enrich.cli as cli
    import gtm_enrich.config as config

    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("FIRECRAWL_API_KEY=fc-from-dotenv\n")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "load_env", lambda: config.load_env(env))

    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0
    import os

    assert os.environ["FIRECRAWL_API_KEY"] == "fc-from-dotenv"


def test_real_environment_beats_the_env_file(tmp_path: Path, monkeypatch) -> None:
    """A .env is a convenience, not an override of what CI already set."""
    from gtm_enrich.config import load_env

    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-from-shell")
    env = tmp_path / ".env"
    env.write_text("FIRECRAWL_API_KEY=fc-from-dotenv\n")
    load_env(env)

    import os

    assert os.environ["FIRECRAWL_API_KEY"] == "fc-from-shell"


def test_check_lists_every_provider_and_backend() -> None:
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0
    for name in ("anthropic", "openai", "direct", "firecrawl", "crawl4ai", "apify"):
        assert name in result.stdout


def test_run_rejects_an_unknown_scraper_backend() -> None:
    result = runner.invoke(
        app, ["run", "--domain", "acme.example", "--scraper", "scrapy", "--no-llm"]
    )
    assert "Unknown scraper backend" in result.stdout
