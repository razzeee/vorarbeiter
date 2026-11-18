import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize("field", ["ADMIN_TOKEN", "GITHUB_WEBHOOK_SECRET"])
@pytest.mark.parametrize("value", [None, "", " \t "])
def test_auth_setting_required(monkeypatch, tmp_path, field, value):
    monkeypatch.chdir(tmp_path)  # Ignore any developer .env file.
    if value is None:
        monkeypatch.delenv(field)
    else:
        monkeypatch.setenv(field, value)

    with pytest.raises(ValidationError) as exc:
        Settings()
    assert [error["loc"] for error in exc.value.errors()] == [(field.lower(),)]
