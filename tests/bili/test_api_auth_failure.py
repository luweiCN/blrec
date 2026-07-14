from unittest.mock import AsyncMock, Mock

import pytest

from blrec.bili.api import BaseApi
from blrec.bili.exceptions import ApiRequestError


@pytest.mark.asyncio
async def test_not_logged_in_response_triggers_account_validation() -> None:
    reporter = AsyncMock()
    api = BaseApi(Mock(), auth_failure_reporter=reporter)

    with pytest.raises(ApiRequestError) as exc_info:
        await api._check_response({'code': -101, 'message': 'not logged in'})

    assert exc_info.value.code == -101
    reporter.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_unrelated_api_error_does_not_mark_cookie_invalid() -> None:
    reporter = AsyncMock()
    api = BaseApi(Mock(), auth_failure_reporter=reporter)

    with pytest.raises(ApiRequestError) as exc_info:
        await api._check_response({'code': 412, 'message': 'risk control'})

    assert exc_info.value.code == 412
    reporter.assert_not_awaited()
