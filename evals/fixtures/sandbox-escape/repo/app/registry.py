"""Plugin registry."""

from .files import read_file, search

HANDLERS = {"read": read_file, "search": search}


def dispatch(name: str, **kwargs):
    return HANDLERS[name](**kwargs)
