"""CDPD: application-neutral Chrome DevTools Protocol diagnostics.

Public objects retain their defining modules and identities. Loading a Worker
contract does not also initialize unrelated service and storage modules.
"""
__version__ = '0.2.1'
__all__ = ['CaptureRequest', 'DiagnosticEvent', 'TargetBinding', 'CaptureError', 'CapturePolicy', 'CDPStructureCollector',
           'CaptureOutcome', 'DiagnosticService', 'EvidenceStore', 'verify_bundle']

_EXPORT_MODULES = {
    'CaptureRequest': 'contracts',
    'DiagnosticEvent': 'contracts',
    'TargetBinding': 'contracts',
    'CaptureError': 'collector',
    'CapturePolicy': 'collector',
    'CDPStructureCollector': 'collector',
    'CaptureOutcome': 'service',
    'DiagnosticService': 'service',
    'EvidenceStore': 'store',
    'verify_bundle': 'store',
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
