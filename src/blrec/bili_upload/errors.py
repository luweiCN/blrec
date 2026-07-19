from typing import Any, Mapping, Optional

__all__ = (
    'BiliApiError',
    'DefinitelyNotSent',
    'ProtocolContractError',
    'RemoteOutcomeUnknown',
)


class ProtocolContractError(RuntimeError):
    pass


class DefinitelyNotSent(RuntimeError):
    def __init__(self, operation: Optional[str] = None) -> None:
        self.operation = operation
        super().__init__('request was not sent')

    def __repr__(self) -> str:
        return '<DefinitelyNotSent>'


class RemoteOutcomeUnknown(RuntimeError):
    def __init__(self, operation: Optional[str] = None) -> None:
        self.operation = operation
        super().__init__('remote outcome is unknown')

    def __repr__(self) -> str:
        return '<RemoteOutcomeUnknown>'


class BiliApiError(RuntimeError):
    def __init__(
        self,
        code: int,
        public_message: Optional[str] = None,
        *,
        operation: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.code = code
        self.public_message = public_message
        self.operation = operation
        self.details = {} if details is None else dict(details)
        super().__init__('Bilibili API error {}'.format(code))

    def __repr__(self) -> str:
        return '<BiliApiError code={}>'.format(self.code)
