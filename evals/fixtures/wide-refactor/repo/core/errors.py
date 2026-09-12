"""Error taxonomy shared by the worker and the API."""


class TransientError(Exception):
    """Retry me."""


class PermanentError(Exception):
    """Do not retry me."""


class Cancelled(Exception):
    """A newer revision superseded this work."""
