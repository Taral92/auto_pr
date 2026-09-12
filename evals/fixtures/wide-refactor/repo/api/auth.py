from fastapi import Header, HTTPException


def require_token(authorization: str = Header("")) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    return authorization[7:]
