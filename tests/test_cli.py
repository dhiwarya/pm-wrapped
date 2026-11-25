"""``pm-dash doctor`` (Part 1 §5-1, Part 3 §12-1)."""

from pmd.cli import doctor_checks, inside_git_work_tree
from pmd.config import Config, load


def test_doctor_passes_for_a_clean_data_dir(tmp_path):
    results = doctor_checks(Config(data_dir=tmp_path / "data"))
    assert all(ok for ok, _ in results), results


def test_doctor_refuses_data_dir_inside_a_git_work_tree(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    data = tmp_path / "repo" / "data"
    assert inside_git_work_tree(data) == (tmp_path / "repo").resolve()
    results = dict((text.split(" (")[0], ok) for ok, text in doctor_checks(Config(data_dir=data)))
    assert results[f"data directory {data} is outside any git work tree"] is False


def test_doctor_refuses_a_public_listen_address(tmp_path):
    results = doctor_checks(Config(data_dir=tmp_path / "d", listen_host="0.0.0.0"))
    assert any(not ok and "loopback" in text for ok, text in results)


def test_doctor_flags_loose_secret_files(tmp_path):
    secrets = tmp_path / "d" / "secrets"
    secrets.mkdir(parents=True, mode=0o700)
    token = secrets / "github-token"
    token.write_text("x")
    token.chmod(0o644)
    results = doctor_checks(Config(data_dir=tmp_path / "d"))
    assert any(not ok and "github-token" in text for ok, text in results)


def test_load_config(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('data_dir = "/tmp/pmd-test"\ntailnet_name = "Laptop.tail1.ts.net"\nport = 9000\n')
    cfg = load(f)
    assert cfg.port == 9000 and "laptop.tail1.ts.net" in cfg.allowed_hosts
    assert load(tmp_path / "missing.toml") == Config()
