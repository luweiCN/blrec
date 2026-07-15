from typing import Protocol, runtime_checkable

__all__ = (
    'ReadableStream',
    'AsyncReadableStream',
    'WritableStream',
    'AsyncWritableStream',
    'RandomIO',
    'AsyncRandomIO',
)


@runtime_checkable
class ReadableStream(Protocol):
    def read(self, size: int = -1) -> bytes:
        pass


@runtime_checkable
class AsyncReadableStream(Protocol):
    async def read(self, size: int = -1) -> bytes:
        pass


@runtime_checkable
class WritableStream(Protocol):
    def write(self, data: bytes) -> int:
        pass


@runtime_checkable
class AsyncWritableStream(Protocol):
    async def write(self, data: bytes) -> int:
        pass


@runtime_checkable
class RandomIO(ReadableStream, WritableStream, Protocol):
    def tell(self) -> int:
        pass

    def seek(self, offset: int, whence: int = 0) -> int:
        pass

    def truncate(self, size: int = None) -> int:
        pass


@runtime_checkable
class AsyncRandomIO(AsyncReadableStream, AsyncWritableStream, Protocol):
    async def tell(self) -> int:
        pass

    async def seek(self, offset: int, whence: int = 0) -> int:
        pass

    async def truncate(self, size: int = None) -> int:
        pass
