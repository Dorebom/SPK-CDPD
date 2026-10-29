"""Standalone ChatGPT Web operation boundary. Not an official OpenAI API.

The public exports are loaded on demand so a target guard import does not
initialize the project and session operations. Exported objects are unchanged.
"""

__all__ = [
    "ChatGPTSession",
    "FailureKind",
    "PageIdentity",
    "ProjectIdentity",
    "ProjectOpenResult",
    "SessionBindResult",
    "WebDebugEvidence",
    "WebOperationStatus",
    "bind_project",
    "open_project",
]

_EXPORT_MODULES = {
    "ChatGPTSession": "session",
    "FailureKind": "models",
    "PageIdentity": "models",
    "ProjectIdentity": "models",
    "ProjectOpenResult": "models",
    "SessionBindResult": "models",
    "WebDebugEvidence": "models",
    "WebOperationStatus": "models",
    "bind_project": "projects",
    "open_project": "projects",
}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    value = getattr(import_module('.' + module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
