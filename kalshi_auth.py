import os
import base64
import datetime
import requests
from pathlib import Path
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

BASE_URL      = "https://external-api.kalshi.com/trade-api/v2"
BASE_URL_DEMO = "https://external-api.demo.kalshi.co/trade-api/v2"


def load_private_key(path=None):
    path = path or os.environ["KALSHI_KEY_PATH"]
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())


def auth_headers(method: str, path: str, key_id: str = None, private_key=None) -> dict:
    """Returns the three Kalshi auth headers for a given request."""
    key_id      = key_id or os.environ["KALSHI_KEY_ID"]
    private_key = private_key or load_private_key()
    ts          = str(int(datetime.datetime.now().timestamp() * 1000))
    path_clean  = path.split("?")[0]
    msg         = f"{ts}{method}{path_clean}".encode()
    sig         = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY":       key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }


def get(path: str, params: dict = None, authed: bool = False, demo: bool = False) -> dict:
    base = BASE_URL_DEMO if demo else BASE_URL
    url  = base + path
    hdrs = auth_headers("GET", f"/trade-api/v2{path}") if authed else {}
    r    = requests.get(url, params=params, headers=hdrs)
    r.raise_for_status()
    return r.json()
