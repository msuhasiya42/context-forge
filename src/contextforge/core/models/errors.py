"""Provider-neutral failures; only explicitly transient failures may be retried."""


class ModelError(Exception):
    pass


class ModelTimeout(ModelError):
    pass


class ModelUnavailable(ModelError):
    pass


class ModelRejected(ModelError):
    pass
