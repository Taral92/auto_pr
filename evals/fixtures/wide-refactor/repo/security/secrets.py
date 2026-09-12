from pydantic import SecretStr


def redact(url: str, token: SecretStr) -> str:
    raw = token.get_secret_value()
    return url.replace(raw, "***") if raw else url
