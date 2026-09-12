import time

import jwt


def app_jwt(app_id: str, pem: str, ttl_s: int = 540) -> str:
    now = int(time.time())
    return jwt.encode({"iat": now - 60, "exp": now + ttl_s, "iss": app_id}, pem, "RS256")
