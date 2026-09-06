from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def clean_resource_url(url: str) -> str | None:
    """Keep stable web actions; reject signed downloads and strip session fields."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        return None
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    signed = {"signature", "sig", "policy", "key-pair-id", "awsaccesskeyid"}
    if any(key.lower() in signed or key.lower().startswith("x-amz-") for key, _ in pairs):
        return None
    private = {"token", "access_token", "id_token", "auth", "authorization", "session",
               "sessionid", "key", "password", "passwd", "user", "effectiveuser"}
    query = urlencode([(key, value) for key, value in pairs if key.lower() not in private])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
