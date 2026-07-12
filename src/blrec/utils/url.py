from typing import Literal
from urllib.parse import urlparse, urlunparse


def ensure_scheme(url: str, scheme: Literal['http', 'https']) -> str:
    return urlunparse(urlparse(url)._replace(scheme=scheme))
