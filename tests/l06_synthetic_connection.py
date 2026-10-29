"""Exact source-equivalent synthetic Connection and structure generator.

Derived strictly from FIX008 tests/test_capture_worker.py, not production code.
No pytest/test_capture_worker import; no browser, network, subprocess or host access.
Only local synthetic tests may import this module. Never product Worker CLI.
"""
from __future__ import annotations
from copy import deepcopy
from cdpd.collector import CaptureError

EPOCH = "254095102094427e3ef2c333da294588"

TARGET = "SYNTHETIC-TARGET-A"

ORIGIN = "https://chatgpt.com"

FEATURES = ("composer.input", "composer.send", "generation.stop", "attachment.input", "response.container")

READ_METHODS = {
    "Target.getTargetInfo", "Target.attachToTarget", "Target.detachFromTarget",
    "Page.getFrameTree", "Page.createIsolatedWorld", "Runtime.evaluate",
}

def node(index=0, parent=-1, *, tag="html", role=None):
    return {"index": index, "parent": parent, "tag": tag, "role": role,
            "shadowBoundary": False, "disabled": False, "hidden": False,
            "visible": True, "box": [0, 0, 200, 100]}

def valid_structure(*, truncated=False):
    return {
        "schema": "cdpd.structure/v1", "nodes": [node()], "truncated": truncated,
        "iframeCount": 0, "openShadowCount": 0,
        "coverage": {"scope": "MAIN_FRAME_AND_OPEN_SHADOW", "iframeDocuments": "NOT_CAPTURED",
                     "closedShadow": "NOT_OBSERVABLE", "accessibilityTree": "NOT_CAPTURED"},
        "observation": "STRUCTURE_ONLY_NOT_FUNCTIONAL_VERIFICATION",
        "profile": {"probes": [{"feature_id": feature, "match_count": 0, "visible_count": 0,
                                  "enabled_count": 0, "probe_complete": not truncated}
                                 for feature in FEATURES],
                    "feature_shapes": {feature: [] for feature in FEATURES}},
    }

class Connection:
    """Exact fixed-target fake: no browser imports, socket, or page fallback."""
    def __init__(self, *, structure=None, before=None, after=None, fail_method=None):
        self.browser_id = EPOCH
        self.target_id = TARGET
        self.url = ORIGIN + "/"
        self.frame_id = "SYNTHETIC-FRAME-A"
        self.loader_id = "SYNTHETIC-LOADER-A"
        self.target_type = "page"
        self.structure = valid_structure() if structure is None else deepcopy(structure)
        self.after = after
        self.fail_method = fail_method
        self.extra_targets = []
        self.disconnected = False
        self.audit = []
        self.calls = []
        self.identity_calls = 0
        self.closed = False
        if before:
            before(self)

    def target_info(self):
        return {"targetId": self.target_id, "type": self.target_type, "url": self.url,
                "attached": True, "canAccessOpener": False}

    def identity(self):
        self.identity_calls += 1
        if self.disconnected:
            raise CaptureError("DISCONNECTED")
        return {"browser_id": self.browser_id,
                "targets": [self.target_info()] + deepcopy(self.extra_targets)}

    def remaining(self):
        return 8.0

    def send(self, method, params, session=None):
        assert method in READ_METHODS, "Unexpected browser capability: " + method
        self.audit.append(method)
        self.calls.append({"method": method, "target_id": params.get("targetId"), "session": session})
        if self.fail_method == method:
            raise RuntimeError("SYNTHETIC_SECRET_EXCEPTION_MUST_NOT_PERSIST")
        if self.disconnected:
            raise CaptureError("DISCONNECTED")
        if method == "Target.getTargetInfo":
            return {"targetInfo": self.target_info()}
        if method == "Target.attachToTarget":
            assert params["targetId"] == TARGET
            return {"sessionId": "SYNTHETIC-SESSION-A"}
        if method == "Target.detachFromTarget":
            return {}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": self.frame_id, "loaderId": self.loader_id,
                                            "url": self.url, "securityOrigin": ORIGIN}}}
        if method == "Page.createIsolatedWorld":
            assert params["frameId"] == self.frame_id
            return {"executionContextId": 11}
        if method == "Runtime.evaluate":
            result = {"result": {"value": deepcopy(self.structure)}}
            if self.after:
                self.after(self)
            return result
        raise AssertionError(method)

    def close(self, session=None):
        self.closed = True
