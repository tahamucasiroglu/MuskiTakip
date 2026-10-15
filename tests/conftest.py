import pytest

from muskitakip import config


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path, monkeypatch):
    """Testler repo içindeki data/ klasörüne log/debug dosyası yazmasın."""
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
