from typing import Any, Dict

import pytest
from loguru import logger
from yarl import URL

from blrec.bili.api import BaseApi


class FakeResponse:
    request_info = 'Cookie: SESSDATA=request-secret'
    url = URL('https://api.bilibili.com/x/test?access_key=query-secret')
    status = 200

    async def __aenter__(self) -> 'FakeResponse':
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def text(self) -> str:
        return '{"code":0,"token":"response-secret"}'

    async def json(self) -> Dict[str, Any]:
        return {'code': 0, 'token': 'response-secret'}


class FakeSession:
    def get(self, *args: object, **kwargs: object) -> FakeResponse:
        return FakeResponse()


@pytest.mark.asyncio
async def test_api_trace_logs_metadata_without_credentials_or_response_body() -> None:
    messages = []
    sink = logger.add(messages.append, level='TRACE', format='{message}')
    try:
        api = BaseApi(  # type: ignore[arg-type]
            FakeSession(), headers={'Cookie': 'SESSDATA=header-secret'}
        )

        await api._get_json_res('https://api.bilibili.com/x/test')
    finally:
        logger.remove(sink)

    output = ''.join(str(message) for message in messages)
    assert 'api.bilibili.com' in output
    assert '/x/test' in output
    assert 'status=200' in output
    for secret in (
        'request-secret',
        'query-secret',
        'response-secret',
        'header-secret',
        'SESSDATA',
        'access_key',
    ):
        assert secret not in output
