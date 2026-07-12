class FlvDataError(ValueError):
    pass


class FlvHeaderError(FlvDataError):
    pass


class FlvTagError(FlvDataError):
    pass


class FlvStreamCorruptedError(Exception):
    pass


class FlvFileCorruptedError(Exception):
    pass
