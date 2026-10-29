from __future__ import annotations

import base64
import mimetypes
import http.client
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from .models import FailureKind, PageIdentity, SessionBindResult, WebDebugEvidence, WebOperationStatus


from .target_guard import (
    BindingGuard, GuardError, ENDPOINT, EXPECTED_ORIGIN, PAGE_FILTER,
    validate_endpoint, derive_browser_epoch, normalize_origin,
    strict_json_loads, single_page, same_target, frame_snapshot,
)


class _PageOps(Protocol):
    @property
    def url(self) -> str: ...

    def title(self) -> str: ...

    def evaluate(self, expression: str, arg: Any | None = None) -> Any: ...

    def trusted_click_marked_action(self, selector: str, *, timeout_ms: int) -> dict[str, Any]: ...

    def goto(self, url: str, *, timeout_ms: int) -> None: ...

    def capture_screenshot(self, destination_path: str) -> bool: ...

    def target_id(self) -> str | None: ...

    def download_from_action(
        self,
        expression: str,
        arg: Any,
        destination_path: str,
        *,
        expected_filename: str,
        timeout_ms: int,
    ) -> dict[str, Any]: ...

    def observe_composer_attachment_transport(
        self,
        *,
        attachment_action_script: str,
        upload_menu_action_script: str,
        timeout_ms: int,
    ) -> dict[str, Any]: ...

    def upload_files_to_composer(
        self,
        file_paths: Sequence[str],
        *,
        attachment_action_script: str,
        upload_menu_action_script: str,
        timeout_ms: int,
    ) -> dict[str, Any]: ...


class _BrowserTransport(Protocol):
    def discover(self, endpoint: str) -> str: ...
    def connect(self, ws_url: str) -> None: ...
    def set_event_handler(self, callback: Any) -> None: ...
    def set_dispatcher(self, callback: Any) -> None: ...
    def send(self, method: str, params: dict[str, Any], *, page: Any = None) -> dict[str, Any]: ...
    def pages(self) -> Sequence[_PageOps]: ...
    def initialize_page(self, page: Any) -> None: ...
    def is_connected(self) -> bool: ...
    def close(self) -> None: ...


class _PrimitiveProxy:
    """Guard direct low-level uses too; no permissive constructor default."""
    def __init__(self, value, dispatch):
        self._value, self._dispatch = value, dispatch

    def __getattr__(self, name):
        allowed = {"url", "title", "evaluate", "goto", "screenshot", "locator", "count", "click",
                   "expect_download", "expect_file_chooser", "suggested_filename", "failure", "save_as",
                   "cancel", "set_files", "is_multiple", "element", "get_attribute", "value"}
        if name not in allowed:
            raise AttributeError(name)
        if self._dispatch is None:
            raise GuardError("BINDING_REQUIRED")
        value = getattr(self._value, name)
        mutation = name not in {"url", "title", "count", "suggested_filename", "failure", "is_multiple", "element", "get_attribute", "value"}
        def invoke(*args, **kwargs):
            result = self._dispatch(name, lambda: value(*args, **kwargs), mutation=mutation,
                                    navigation=name == "goto")
            if name in {"locator", "expect_download", "expect_file_chooser"}:
                return _PrimitiveProxy(result, self._dispatch)
            return result
        if callable(value):
            return invoke
        result = self._dispatch(name, lambda: value, mutation=mutation)
        return _PrimitiveProxy(result, self._dispatch) if name in {"element", "value"} else result

    def __enter__(self):
        if self._dispatch is None:
            raise GuardError("BINDING_REQUIRED")
        self._entered = self._dispatch("event.enter", self._value.__enter__, mutation=False)
        return _PrimitiveProxy(self._entered, self._dispatch)

    def __exit__(self, exc_type, exc, tb):
        return self._value.__exit__(exc_type, exc, tb)


class _PlaywrightPageOps:
    def __init__(self, context: Any, page: Any, *, dispatch=None) -> None:
        from .cdp_page import CdpPage
        self._context = context
        # Native CdpPage guards each protocol dispatch on its persistent session.
        # Direct constructor use also requires an explicitly supplied guard.
        self._page = page if isinstance(page, CdpPage) else _PrimitiveProxy(page, dispatch)
        self._reported_target_id = page.target_id if isinstance(page, CdpPage) else None

    @property
    def url(self) -> str:
        return str(self._page.url)

    def title(self) -> str:
        try:
            return str(self._page.title())
        except Exception:
            return ""

    def evaluate(self, expression: str, arg: Any | None = None) -> Any:
        if arg is None:
            return self._page.evaluate(expression)
        return self._page.evaluate(expression, arg)

    def trusted_click_marked_action(self, selector: str, *, timeout_ms: int) -> dict[str, Any]:
        """Deliver one guarded trusted click to an already semantically-bound action.

        The caller owns semantic selection and places a unique temporary marker.
        This method owns only browser activation and trusted-event diagnostics; it
        never infers which action should be clicked.
        """
        try:
            locator = self._page.locator(selector)
            count = int(locator.count())
        except Exception as exc:
            return {
                "ok": False, "activationAttempted": False, "trustedClick": False,
                "reason": "trusted-click-locator-failed", "locatorCount": 0,
                "errorType": type(exc).__name__,
            }
        if count != 1:
            return {
                "ok": False, "activationAttempted": False, "trustedClick": False,
                "reason": "trusted-click-target-not-unique", "locatorCount": count,
            }

        token = f"shirabe-trusted-click-{time.monotonic_ns():x}"
        arm_script = r"""
        payload => {
          /* SHIRABE_TRUSTED_CLICK_DIAGNOSTIC_ARM_V1 */
          const selector=String(payload.selector||''); const token=String(payload.token||'');
          const target=document.querySelector(selector);
          if(!target) return {ok:false,reason:'target-not-found'};
          const state={trusted:0,total:0,events:[],target,listener:null};
          const listener=e=>{
            if(e.composedPath && !e.composedPath().includes(target)) return;
            state.total+=1; if(e.isTrusted) state.trusted+=1;
            if(state.events.length<16) state.events.push({type:e.type,isTrusted:Boolean(e.isTrusted),detail:Number(e.detail||0)});
          };
          state.listener=listener; document.addEventListener('click',listener,true);
          window.__shirabeTrustedClickDiagnostics=window.__shirabeTrustedClickDiagnostics||{};
          window.__shirabeTrustedClickDiagnostics[token]=state;
          return {ok:true};
        }
        """
        collect_script = r"""
        payload => {
          /* SHIRABE_TRUSTED_CLICK_DIAGNOSTIC_COLLECT_V1 */
          const bag=window.__shirabeTrustedClickDiagnostics||{}; const token=String(payload.token||'');
          const state=bag[token]; if(!state) return {ok:false,reason:'diagnostic-state-missing'};
          try{document.removeEventListener('click',state.listener,true);}catch(_){}
          const result={ok:true,trustedClickEventCount:Number(state.trusted||0),clickEventCount:Number(state.total||0),eventTrace:Array.from(state.events||[])};
          try{delete bag[token];}catch(_){} return result;
        }
        """
        try:
            arm = self._page.evaluate(arm_script, {"selector": selector, "token": token}) or {}
        except Exception as exc:
            return {
                "ok": False, "activationAttempted": False, "trustedClick": False,
                "reason": "trusted-click-diagnostic-arm-failed", "locatorCount": 1,
                "errorType": type(exc).__name__,
            }
        if not isinstance(arm, dict) or not arm.get("ok"):
            return {
                "ok": False, "activationAttempted": False, "trustedClick": False,
                "reason": "trusted-click-diagnostic-arm-not-established", "locatorCount": 1,
            }
        click_error = None
        try:
            locator.click(timeout=max(1, int(timeout_ms)))
        except Exception as exc:
            click_error = exc
        try:
            collected = self._page.evaluate(collect_script, {"token": token}) or {}
        except Exception as exc:
            collected = {"ok": False, "reason": "trusted-click-diagnostic-collect-failed", "errorType": type(exc).__name__}
        trusted_count = int(collected.get("trustedClickEventCount") or 0) if isinstance(collected, dict) else 0
        common = {
            "activationAttempted": True,
            "trustedClick": trusted_count > 0 and click_error is None,
            "locatorCount": 1,
            "trustedClickEventCount": trusted_count,
            "clickEventCount": int(collected.get("clickEventCount") or 0) if isinstance(collected, dict) else 0,
            "eventTrace": list(collected.get("eventTrace") or []) if isinstance(collected, dict) else [],
        }
        if click_error is not None:
            return {**common, "ok": False, "reason": "trusted-click-failed", "errorType": type(click_error).__name__}
        if trusted_count < 1:
            return {**common, "ok": False, "reason": "trusted-click-event-not-observed"}
        return {**common, "ok": True, "reason": "trusted-click-delivered"}

    def goto(self, url: str, *, timeout_ms: int) -> None:
        self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    def capture_screenshot(self, destination_path: str) -> bool:
        try:
            self._page.screenshot(path=str(destination_path), full_page=False)
            return True
        except Exception:
            return False

    def _install_native_open_file_picker_bridge(self, file_paths: Sequence[str]) -> tuple[str | None, dict[str, Any]]:
        """Temporarily bridge ``showOpenFilePicker`` to verified local bytes.

        Current ChatGPT Web invokes the File System Access API for the
        ``Add photos & files / Upload from computer`` action. Playwright's
        ``expect_file_chooser`` does not observe that API. The bridge preserves
        the real trusted UI action while replacing only the browser-native
        picker result with FileSystemFileHandle-compatible objects backed by
        the caller's already-verified local files.
        """

        payload_files: list[dict[str, Any]] = []
        for raw_path in file_paths:
            path = Path(raw_path).expanduser().resolve()
            data = path.read_bytes()
            media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            payload_files.append(
                {
                    "name": path.name,
                    "mediaType": media_type,
                    "base64": base64.b64encode(data).decode("ascii"),
                    "size": len(data),
                    "lastModified": int(path.stat().st_mtime * 1000),
                }
            )
        token = f"shirabe-native-picker-{id(self):x}-{time.monotonic_ns():x}"
        script = r"""
        payload => {
          /* SHIRABE_NATIVE_OPEN_FILE_PICKER_BRIDGE_V1 */
          const token=String(payload.token||''); const descriptors=Array.isArray(payload.files)?payload.files:[];
          const original=window.showOpenFilePicker;
          if(typeof original!=='function') return {ok:false,reason:'show-open-file-picker-unavailable'};
          const bag=window.__shirabeNativeOpenFilePickerBridge=window.__shirabeNativeOpenFilePickerBridge||{};
          const state={token,original,calls:0,getFileCalls:0,returnedCount:0,returnedNames:[],requestedOptions:[],installedAt:performance.now(),wrapped:null};
          const makeFile=desc=>{
            const binary=atob(String(desc.base64||'')); const bytes=new Uint8Array(binary.length);
            for(let i=0;i<binary.length;i++) bytes[i]=binary.charCodeAt(i);
            return new File([bytes],String(desc.name||''),{type:String(desc.mediaType||'application/octet-stream'),lastModified:Number(desc.lastModified||Date.now())});
          };
          const makeHandle=desc=>{
            const file=makeFile(desc); const proto=(typeof window.FileSystemFileHandle==='function'&&window.FileSystemFileHandle.prototype)||Object.prototype;
            const handle=Object.create(proto);
            Object.defineProperties(handle,{
              kind:{value:'file',enumerable:true}, name:{value:String(desc.name||''),enumerable:true},
              getFile:{value:async()=>{state.getFileCalls+=1; return file;}},
              isSameEntry:{value:async other=>other===handle},
              queryPermission:{value:async()=> 'granted'}, requestPermission:{value:async()=> 'granted'},
              [Symbol.toStringTag]:{value:'FileSystemFileHandle'}
            });
            return handle;
          };
          const wrapped=async function(options){
            state.calls+=1;
            try{state.requestedOptions.push(JSON.parse(JSON.stringify(options||{})));}catch(_){state.requestedOptions.push({});}
            const handles=descriptors.map(makeHandle); state.returnedCount=handles.length; state.returnedNames=handles.map(h=>h.name); return handles;
          };
          state.wrapped=wrapped; bag[token]=state; window.showOpenFilePicker=wrapped;
          return {ok:true,reason:'native-picker-bridge-installed',token,fileCount:descriptors.length,filenames:descriptors.map(d=>String(d.name||'')),byteCount:descriptors.reduce((n,d)=>n+Number(d.size||0),0)};
        }
        """
        try:
            raw = self._page.evaluate(script, {"token": token, "files": payload_files}) or {}
            result = dict(raw) if isinstance(raw, dict) else {"ok": False, "reason": "native-picker-bridge-invalid-result"}
            return (token if result.get("ok") else None), result
        except Exception as exc:
            return None, {"ok": False, "reason": "native-picker-bridge-install-failed", "errorType": type(exc).__name__}

    def _collect_native_open_file_picker_bridge(self, token: str) -> dict[str, Any]:
        script = r"""
        token => {
          /* SHIRABE_NATIVE_OPEN_FILE_PICKER_BRIDGE_COLLECT_V1 */
          const bag=window.__shirabeNativeOpenFilePickerBridge||{}; const state=bag[String(token||'')];
          if(!state) return {ok:false,reason:'native-picker-bridge-state-missing'};
          return {ok:true,reason:'native-picker-bridge-state-collected',calls:Number(state.calls||0),getFileCalls:Number(state.getFileCalls||0),returnedCount:Number(state.returnedCount||0),returnedNames:Array.from(state.returnedNames||[]),requestedOptions:Array.from(state.requestedOptions||[]),durationMs:Math.round(performance.now()-state.installedAt)};
        }
        """
        try:
            raw = self._page.evaluate(script, token) or {}
            return dict(raw) if isinstance(raw, dict) else {"ok": False, "reason": "native-picker-bridge-collect-invalid-result"}
        except Exception as exc:
            return {"ok": False, "reason": "native-picker-bridge-collect-failed", "errorType": type(exc).__name__}

    def _restore_native_open_file_picker_bridge(self, token: str) -> dict[str, Any]:
        script = r"""
        token => {
          /* SHIRABE_NATIVE_OPEN_FILE_PICKER_BRIDGE_RESTORE_V1 */
          const bag=window.__shirabeNativeOpenFilePickerBridge||{}; const key=String(token||''); const state=bag[key];
          if(!state) return {ok:false,reason:'native-picker-bridge-state-missing'};
          let restored=false; try{if(window.showOpenFilePicker===state.wrapped){window.showOpenFilePicker=state.original;restored=true;}}catch(_){}
          try{delete bag[key];}catch(_){}
          return {ok:restored,reason:restored?'native-picker-bridge-restored':'native-picker-bridge-not-current'};
        }
        """
        try:
            raw = self._page.evaluate(script, token) or {}
            return dict(raw) if isinstance(raw, dict) else {"ok": False, "reason": "native-picker-bridge-restore-invalid-result"}
        except Exception as exc:
            return {"ok": False, "reason": "native-picker-bridge-restore-failed", "errorType": type(exc).__name__}

    def target_id(self) -> str | None:
        # This value is reporting only; session admission always uses fresh CDP.
        return self._reported_target_id

    def download_from_action(
        self,
        expression: str,
        arg: Any,
        destination_path: str,
        *,
        expected_filename: str,
        timeout_ms: int,
    ) -> dict[str, Any]:
        """Wait for a browser download event before dispatching one page action.

        The transport returns only non-secret transfer diagnostics. Temporary browser
        paths and authenticated URLs never cross this boundary.
        """

        dispatch_result: Any = None
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        except ImportError:  # pragma: no cover - imported with Playwright transport
            PlaywrightTimeoutError = TimeoutError  # type: ignore[assignment,misc]

        try:
            with self._page.expect_download(timeout=timeout_ms) as download_info:
                dispatch_result = self._page.evaluate(expression, arg)
            download = download_info.value
        except PlaywrightTimeoutError:
            return {
                "ok": False,
                "status": "timeout",
                "eventObserved": False,
                "dispatchResult": dispatch_result,
                "suggestedFilename": None,
                "suggestedFilenameMatch": None,
                "transferComplete": False,
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": "event-failed",
                "eventObserved": False,
                "dispatchResult": dispatch_result,
                "suggestedFilename": None,
                "suggestedFilenameMatch": None,
                "transferComplete": False,
                "errorType": type(exc).__name__,
            }

        suggested = str(download.suggested_filename or "")
        suggested_match = suggested == expected_filename
        dispatch_ok = isinstance(dispatch_result, dict) and bool(dispatch_result.get("ok"))
        if not dispatch_ok:
            try:
                download.cancel()
            except Exception:
                pass
            return {
                "ok": False,
                "status": "dispatch-failed",
                "eventObserved": True,
                "dispatchResult": dispatch_result,
                "suggestedFilename": suggested or None,
                "suggestedFilenameMatch": suggested_match,
                "transferComplete": False,
            }

        failure = None
        try:
            failure = download.failure()
        except Exception as exc:
            return {
                "ok": False,
                "status": "transfer-failed",
                "eventObserved": True,
                "dispatchResult": dispatch_result,
                "suggestedFilename": suggested or None,
                "suggestedFilenameMatch": suggested_match,
                "transferComplete": False,
                "errorType": type(exc).__name__,
            }
        if failure:
            lowered = str(failure).lower()
            state = "cancelled" if "cancel" in lowered else "transfer-failed"
            return {
                "ok": False,
                "status": state,
                "eventObserved": True,
                "dispatchResult": dispatch_result,
                "suggestedFilename": suggested or None,
                "suggestedFilenameMatch": suggested_match,
                "transferComplete": False,
            }
        if not suggested_match:
            return {
                "ok": False,
                "status": "filename-mismatch",
                "eventObserved": True,
                "dispatchResult": dispatch_result,
                "suggestedFilename": suggested or None,
                "suggestedFilenameMatch": False,
                "transferComplete": True,
            }

        try:
            download.save_as(destination_path)
        except Exception as exc:
            return {
                "ok": False,
                "status": "save-failed",
                "eventObserved": True,
                "dispatchResult": dispatch_result,
                "suggestedFilename": suggested or None,
                "suggestedFilenameMatch": True,
                "transferComplete": False,
                "errorType": type(exc).__name__,
            }
        return {
            "ok": True,
            "status": "completed",
            "eventObserved": True,
            "dispatchResult": dispatch_result,
            "suggestedFilename": suggested or None,
            "suggestedFilenameMatch": True,
            "transferComplete": True,
        }


    def _bind_composer_filechooser(
        self,
        *,
        attachment_action_script: str,
        upload_menu_action_script: str,
        timeout_ms: int,
    ) -> tuple[Any | None, dict[str, Any]]:
        """Bind the FileChooser caused by one semantically selected Composer action."""

        timeout_ms = max(250, int(timeout_ms))
        token_base = f"shirabe-{id(self):x}"
        attachment_token = token_base + "-attachment"
        menu_token = token_base + "-menu"

        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        except ImportError:  # pragma: no cover
            PlaywrightTimeoutError = TimeoutError  # type: ignore[assignment,misc]

        def cleanup(attribute: str, token: str) -> None:
            try:
                self._page.evaluate(
                    "payload => { const el=document.querySelector('['+payload.attribute+'=\"'+payload.token+'\"]'); if(el) el.removeAttribute(payload.attribute); }",
                    {"attribute": attribute, "token": token},
                )
            except Exception:
                pass

        def cleanup_delta(token: str) -> None:
            if not token:
                return
            try:
                self._page.evaluate(
                    "payload => { const bag=window.__shirabeAttachmentDelta||{}; const state=bag[payload.token]; if(state&&state.observer){try{state.observer.disconnect();}catch(_){}} try{delete bag[payload.token];}catch(_){} }",
                    {"token": token},
                )
            except Exception:
                pass

        def select(script: str, token: str, **extra: Any) -> dict[str, Any]:
            try:
                payload = {"token": token, **extra}
                raw = self._page.evaluate(script, payload) or {}
                return dict(raw) if isinstance(raw, dict) else {"ok": False, "reason": "action-selection-invalid-result"}
            except Exception as exc:
                return {"ok": False, "reason": "action-selection-failed", "errorType": type(exc).__name__}

        def click_for_chooser(selector: str, *, event_timeout_ms: int) -> tuple[Any | None, dict[str, Any]]:
            try:
                locator = self._page.locator(selector)
                count = int(locator.count())
            except Exception as exc:
                return None, {"ok": False, "reason": "action-locator-failed", "errorType": type(exc).__name__}
            if count != 1:
                return None, {"ok": False, "reason": "action-target-not-unique", "locatorCount": count}
            try:
                with self._page.expect_file_chooser(timeout=event_timeout_ms) as chooser_info:
                    locator.click()
                return chooser_info.value, {"ok": True, "reason": "filechooser-observed", "locatorCount": 1}
            except PlaywrightTimeoutError:
                return None, {"ok": False, "reason": "filechooser-not-observed", "locatorCount": 1}
            except Exception as exc:
                return None, {"ok": False, "reason": "action-click-failed", "locatorCount": 1, "errorType": type(exc).__name__}

        def arm_activation_diagnostics(selector: str, token: str) -> dict[str, Any]:
            """Arm non-upload diagnostics around one trusted local-upload activation."""
            script = r"""
            payload => {
              /* SHIRABE_UPLOAD_ACTIVATION_DIAGNOSTIC_ARM_V1 */
              const token=String(payload.token||''); const selector=String(payload.selector||'');
              const target=document.querySelector(selector);
              const normalize=v=>String(v||'').replace(/\s+/g,' ').trim().toLowerCase();
              const rectOf=el=>{try{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,left:r.left,top:r.top,right:r.right,bottom:r.bottom};}catch(_){return null;}};
              const sig=el=>{if(!el)return null; const tag=String(el.tagName||'').toLowerCase(); const role=normalize(el.getAttribute&&el.getAttribute('role')); const id=normalize(el.getAttribute&&el.getAttribute('id')); const testId=normalize(el.getAttribute&&el.getAttribute('data-testid')); const aria=normalize(el.getAttribute&&el.getAttribute('aria-label')); const text=normalize(el.textContent).slice(0,180); return [tag,role,id,testId,aria,text].join('|');};
              const inputInfo=el=>({signature:sig(el),id:normalize(el.id),name:normalize(el.name),accept:String(el.accept||''),multiple:Boolean(el.multiple),disabled:Boolean(el.disabled),connected:Boolean(el.isConnected),hidden:Boolean(el.hidden),rect:rectOf(el),display:(()=>{try{return getComputedStyle(el).display}catch(_){return ''}})()});
              const inputs=()=>[...document.querySelectorAll('input[type=\"file\"]')].map(inputInfo);
              const state={token,target,beforeInputs:inputs(),events:[],inputClickMethodCalls:0,inputShowPickerCalls:0,showOpenFilePickerCalls:0,showDirectoryPickerCalls:0,showSaveFilePickerCalls:0,wrappers:{},listeners:[],armedAt:performance.now()};
              const pathIncludesTarget=e=>{try{return target?e.composedPath().some(n=>n===target):false}catch(_){return false}};
              const listener=e=>{if(state.events.length>=80)return; const t=e.target&&e.target.nodeType===1?e.target:null; state.events.push({type:e.type,isTrusted:Boolean(e.isTrusted),defaultPrevented:Boolean(e.defaultPrevented),button:typeof e.button==='number'?e.button:null,detail:typeof e.detail==='number'?e.detail:null,key:String(e.key||''),targetSignature:sig(t),pathIncludesActivationTarget:pathIncludesTarget(e),targetIsFileInput:Boolean(t&&String(t.tagName||'').toLowerCase()==='input'&&String(t.type||'').toLowerCase()==='file')});};
              for(const type of ['pointerdown','mousedown','mouseup','click','keydown','keyup']){document.addEventListener(type,listener,true);state.listeners.push([type,listener]);}
              try{const proto=HTMLInputElement.prototype; const original=proto.click; const wrapped=function(...args){if(String(this.type||'').toLowerCase()==='file')state.inputClickMethodCalls+=1; return original.apply(this,args);}; proto.click=wrapped; state.wrappers.inputClick={proto,original,wrapped};}catch(_){}
              try{const proto=HTMLInputElement.prototype; const original=proto.showPicker; if(typeof original==='function'){const wrapped=function(...args){if(String(this.type||'').toLowerCase()==='file')state.inputShowPickerCalls+=1; return original.apply(this,args);}; proto.showPicker=wrapped; state.wrappers.inputShowPicker={proto,original,wrapped};}}catch(_){}
              for(const name of ['showOpenFilePicker','showDirectoryPicker','showSaveFilePicker']){try{const original=window[name]; if(typeof original==='function'){const wrapped=function(...args){state[name+'Calls']=(state[name+'Calls']||0)+1; return original.apply(this,args);}; window[name]=wrapped; state.wrappers[name]={original,wrapped};}}catch(_){}}
              window.__shirabeUploadActivationDiagnostics=window.__shirabeUploadActivationDiagnostics||{}; window.__shirabeUploadActivationDiagnostics[token]=state;
              const r=rectOf(target); let hit=null; if(r){try{hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);}catch(_){}}
              return {ok:Boolean(target),targetSignature:sig(target),targetTag:target?String(target.tagName||'').toLowerCase():null,targetRole:target?normalize(target.getAttribute('role')):null,targetTabIndex:target?target.getAttribute('tabindex'):null,targetOuterHtml:target?String(target.outerHTML||'').slice(0,4000):null,targetRect:r,centerHitSignature:sig(hit),fileInputBeforeCount:state.beforeInputs.length,fileInputsBefore:state.beforeInputs,showOpenFilePickerAvailable:typeof window.showOpenFilePicker==='function',inputShowPickerAvailable:typeof HTMLInputElement.prototype.showPicker==='function'};
            }
            """
            try:
                raw = self._page.evaluate(script, {"selector": selector, "token": token}) or {}
                return dict(raw) if isinstance(raw, dict) else {"ok": False, "reason": "activation-diagnostic-arm-invalid"}
            except Exception as exc:
                return {"ok": False, "reason": "activation-diagnostic-arm-failed", "errorType": type(exc).__name__}

        def collect_activation_diagnostics(selector: str, token: str) -> dict[str, Any]:
            script = r"""
            payload => {
              /* SHIRABE_UPLOAD_ACTIVATION_DIAGNOSTIC_COLLECT_V1 */
              const token=String(payload.token||''); const selector=String(payload.selector||'');
              const bag=window.__shirabeUploadActivationDiagnostics||{}; const state=bag[token]; const target=document.querySelector(selector)||(state&&state.target)||null;
              const normalize=v=>String(v||'').replace(/\s+/g,' ').trim().toLowerCase();
              const rectOf=el=>{try{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,left:r.left,top:r.top,right:r.right,bottom:r.bottom};}catch(_){return null;}};
              const visible=el=>{if(!el)return false;try{const s=getComputedStyle(el),r=el.getBoundingClientRect();return el.isConnected&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'&&r.width>0&&r.height>0&&el.getAttribute('aria-hidden')!=='true';}catch(_){return false;}};
              const sig=el=>{if(!el)return null; const tag=String(el.tagName||'').toLowerCase(); const role=normalize(el.getAttribute&&el.getAttribute('role')); const id=normalize(el.getAttribute&&el.getAttribute('id')); const testId=normalize(el.getAttribute&&el.getAttribute('data-testid')); const aria=normalize(el.getAttribute&&el.getAttribute('aria-label')); const text=normalize(el.textContent).slice(0,180); return [tag,role,id,testId,aria,text].join('|');};
              const inputInfo=el=>({signature:sig(el),id:normalize(el.id),name:normalize(el.name),accept:String(el.accept||''),multiple:Boolean(el.multiple),disabled:Boolean(el.disabled),connected:Boolean(el.isConnected),hidden:Boolean(el.hidden),rect:rectOf(el),display:(()=>{try{return getComputedStyle(el).display}catch(_){return ''}})()});
              const afterInputs=[...document.querySelectorAll('input[type=\"file\"]')].map(inputInfo); const beforeInputs=(state&&state.beforeInputs)||[];
              const beforeSigs=new Set(beforeInputs.map(x=>x.signature)); const newInputs=afterInputs.filter(x=>!beforeSigs.has(x.signature));
              if(state){for(const [type,fn] of state.listeners||[]){try{document.removeEventListener(type,fn,true);}catch(_){}} for(const [name,info] of Object.entries(state.wrappers||{})){try{if(name==='inputClick'||name==='inputShowPicker'){if(info.proto[name==='inputClick'?'click':'showPicker']===info.wrapped)info.proto[name==='inputClick'?'click':'showPicker']=info.original;}else if(window[name]===info.wrapped)window[name]=info.original;}catch(_){}}}
              const events=(state&&state.events)||[]; const trustedClicks=events.filter(e=>e.type==='click'&&e.isTrusted&&e.pathIncludesActivationTarget); const fileInputClicks=events.filter(e=>e.type==='click'&&e.targetIsFileInput);
              const r=rectOf(target); let hit=null; if(r){try{hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);}catch(_){}}
              const data={ok:Boolean(state),durationMs:state?Math.round(performance.now()-state.armedAt):0,targetConnected:Boolean(target&&target.isConnected),targetVisibleAfter:visible(target),targetSignatureAfter:sig(target),centerHitSignatureAfter:sig(hit),activeElementSignature:sig(document.activeElement),trustedClickEventCount:trustedClicks.length,fileInputClickEventCount:fileInputClicks.length,inputClickMethodCalls:Number(state&&state.inputClickMethodCalls||0),inputShowPickerCalls:Number(state&&state.inputShowPickerCalls||0),showOpenFilePickerCalls:Number(state&&state.showOpenFilePickerCalls||0),showDirectoryPickerCalls:Number(state&&state.showDirectoryPickerCalls||0),showSaveFilePickerCalls:Number(state&&state.showSaveFilePickerCalls||0),fileInputBeforeCount:beforeInputs.length,fileInputAfterCount:afterInputs.length,newFileInputCount:newInputs.length,fileInputsAfter:afterInputs,newFileInputs:newInputs,eventTrace:events.slice(0,80)};
              try{delete bag[token];}catch(_){}
              return data;
            }
            """
            try:
                raw = self._page.evaluate(script, {"selector": selector, "token": token}) or {}
                return dict(raw) if isinstance(raw, dict) else {"ok": False, "reason": "activation-diagnostic-collect-invalid"}
            except Exception as exc:
                return {"ok": False, "reason": "activation-diagnostic-collect-failed", "errorType": type(exc).__name__}

        attachment = select(attachment_action_script, attachment_token)
        common: dict[str, Any] = {
            "transport": "composer-action-filechooser-v6",
            "actionRootReason": attachment.get("actionRootReason"),
            "actionRootCandidateCount": int(attachment.get("actionRootCandidateCount") or 0),
            "actionRootSelectedDepth": attachment.get("actionRootSelectedDepth"),
            "actionRootDetails": attachment.get("actionRootDetails") or [],
            "attachmentActionReason": attachment.get("reason"),
            "attachmentRawActionCount": int(attachment.get("rawActionCount") or 0),
            "attachmentActiveActionCount": int(attachment.get("activeActionCount") or 0),
            "attachmentLogicalActionCount": int(attachment.get("logicalActionCount") or 0),
            "attachmentSelectedKind": attachment.get("selectedKind"),
            "attachmentSelectedSignature": attachment.get("selectedSignature"),
            "attachmentActionDetails": attachment.get("activeActionDetails") or [],
            "attachmentTriggerAriaControls": attachment.get("triggerAriaControls") or "",
            "attachmentTriggerAriaOwns": attachment.get("triggerAriaOwns") or "",
            "attachmentTriggerAriaExpanded": attachment.get("triggerAriaExpanded") or "",
            "attachmentTriggerRect": attachment.get("triggerRect"),
            "popupBaselineCount": int(attachment.get("popupBaselineCount") or 0),
            "causalDeltaToken": attachment.get("causalDeltaToken") or "",
            "baselineVisibleInteractiveCount": int(attachment.get("baselineVisibleInteractiveCount") or 0),
            "fileChooserEventObserved": False,
            "fileChooserMultiple": None,
            "fileChooserAccept": "",
        }
        if not attachment.get("ok"):
            cleanup("data-shirabe-attachment-action", attachment_token)
            return None, {**common, "ok": False, "stage": "attachment-action-select", "reason": str(attachment.get("reason") or "attachment-action-select-failed")}

        chooser = None
        click_result: dict[str, Any]
        selected_kind = str(attachment.get("selectedKind") or "")
        try:
            selector = f'[data-shirabe-attachment-action="{attachment_token}"]'
            if selected_kind == "menu-opener":
                try:
                    locator = self._page.locator(selector)
                    locator_count = int(locator.count())
                    if locator_count != 1:
                        cleanup_delta(str(attachment.get("causalDeltaToken") or ""))
                        return None, {**common, "ok": False, "stage": "attachment-action-click", "reason": "action-target-not-unique", "attachmentActionLocatorCount": locator_count}
                    locator.click()
                    click_result = {"ok": True, "reason": "menu-opener-clicked", "locatorCount": 1}
                except Exception as exc:
                    cleanup_delta(str(attachment.get("causalDeltaToken") or ""))
                    return None, {**common, "ok": False, "stage": "attachment-action-click", "reason": "action-click-failed", "errorType": type(exc).__name__}
            else:
                chooser, click_result = click_for_chooser(selector, event_timeout_ms=min(timeout_ms, 1500))
                if chooser is not None:
                    cleanup_delta(str(attachment.get("causalDeltaToken") or ""))
        finally:
            cleanup("data-shirabe-attachment-action", attachment_token)

        common["attachmentClickReason"] = click_result.get("reason")
        if chooser is None:
            menu_result = select(
                upload_menu_action_script,
                menu_token,
                popupBaselineSignatures=attachment.get("popupBaselineSignatures") or [],
                triggerAriaControls=attachment.get("triggerAriaControls") or "",
                triggerAriaOwns=attachment.get("triggerAriaOwns") or "",
                triggerRect=attachment.get("triggerRect"),
                causalDeltaToken=attachment.get("causalDeltaToken") or "",
                deltaObservationMs=min(timeout_ms, 1400),
            )
            common.update({
                "menuActionReason": menu_result.get("reason"),
                "popupDiscoveryMode": menu_result.get("popupDiscoveryMode"),
                "popupBeforeRootCount": int(menu_result.get("popupBeforeRootCount") or 0),
                "popupAfterRootCount": int(menu_result.get("popupAfterRootCount") or 0),
                "popupNewRootCount": int(menu_result.get("popupNewRootCount") or 0),
                "popupRootCandidateCount": int(menu_result.get("popupRootCandidateCount") or 0),
                "popupSelectedSignature": menu_result.get("popupSelectedSignature"),
                "popupRootDetails": menu_result.get("popupRootDetails") or [],
                "causalObservationDurationMs": int(menu_result.get("causalObservationDurationMs") or 0),
                "causalPollCount": int(menu_result.get("causalPollCount") or 0),
                "mutationAddedCount": int(menu_result.get("mutationAddedCount") or 0),
                "mutationAttributeChangeCount": int(menu_result.get("mutationAttributeChangeCount") or 0),
                "mutationSamples": menu_result.get("mutationSamples") or [],
                "visibleInteractiveBeforeCount": int(menu_result.get("visibleInteractiveBeforeCount") or 0),
                "visibleInteractiveAfterCount": int(menu_result.get("visibleInteractiveAfterCount") or 0),
                "newlyVisibleInteractiveCount": int(menu_result.get("newlyVisibleInteractiveCount") or 0),
                "newlyVisibleInteractiveDetails": menu_result.get("newlyVisibleInteractiveDetails") or [],
                "popupNeighborhoodReason": menu_result.get("popupNeighborhoodReason"),
                "popupNeighborhoodSelectedDepth": menu_result.get("popupNeighborhoodSelectedDepth"),
                "popupNeighborhoodSignature": menu_result.get("popupNeighborhoodSignature"),
                "menuRootCount": int(menu_result.get("menuRootCount") or 0),
                "menuRawActionCount": int(menu_result.get("rawMenuActionCount") or 0),
                "menuActiveActionCount": int(menu_result.get("activeMenuActionCount") or 0),
                "menuLogicalActionCount": int(menu_result.get("logicalMenuActionCount") or 0),
                "menuSelectedSignature": menu_result.get("selectedSignature"),
                "menuActionDetails": menu_result.get("activeMenuActionDetails") or [],
                "uploadActivationTargetReason": menu_result.get("activationTargetReason"),
                "uploadActivationTargetCandidateCount": int(menu_result.get("activationTargetCandidateCount") or 0),
                "uploadActivationTargetSelectedRank": menu_result.get("activationTargetSelectedRank"),
                "uploadActivationTargetSelectedSource": menu_result.get("activationTargetSelectedSource"),
                "uploadActivationTargetSelectedTag": menu_result.get("activationTargetSelectedTag"),
                "uploadActivationTargetSelectedRole": menu_result.get("activationTargetSelectedRole"),
                "uploadActivationTargetSelectedSignature": menu_result.get("activationTargetSelectedSignature"),
                "uploadActivationTargetDetails": menu_result.get("activationTargetDetails") or [],
            })
            if not menu_result.get("ok"):
                cleanup("data-shirabe-upload-menu-action", menu_token)
                return None, {**common, "ok": False, "stage": "upload-menu-action-select", "reason": str(menu_result.get("reason") or "upload-menu-action-select-failed")}
            menu_selector = f'[data-shirabe-upload-menu-action="{menu_token}"]'
            diagnostic_token = menu_token + "-activation-diagnostic"
            arm_diag = arm_activation_diagnostics(menu_selector, diagnostic_token)
            common.update({
                "activationDiagnosticArmOk": bool(arm_diag.get("ok")),
                "activationTargetOuterHtml": arm_diag.get("targetOuterHtml"),
                "activationTargetCenterHitSignature": arm_diag.get("centerHitSignature"),
                "fileInputBeforeCount": int(arm_diag.get("fileInputBeforeCount") or 0),
                "fileInputsBefore": arm_diag.get("fileInputsBefore") or [],
                "showOpenFilePickerAvailable": bool(arm_diag.get("showOpenFilePickerAvailable")),
                "inputShowPickerAvailable": bool(arm_diag.get("inputShowPickerAvailable")),
            })
            try:
                chooser, menu_click = click_for_chooser(menu_selector, event_timeout_ms=timeout_ms)
                common["menuClickReason"] = menu_click.get("reason")
                click_diag = collect_activation_diagnostics(menu_selector, diagnostic_token)
                common.update({
                    "activationDiagnosticCollectOk": bool(click_diag.get("ok")),
                    "activationDiagnosticDurationMs": int(click_diag.get("durationMs") or 0),
                    "activationTargetConnectedAfter": bool(click_diag.get("targetConnected")),
                    "activationTargetVisibleAfter": bool(click_diag.get("targetVisibleAfter")),
                    "activationTargetSignatureAfter": click_diag.get("targetSignatureAfter"),
                    "activationCenterHitSignatureAfter": click_diag.get("centerHitSignatureAfter"),
                    "activationActiveElementSignature": click_diag.get("activeElementSignature"),
                    "activationTrustedClickEventCount": int(click_diag.get("trustedClickEventCount") or 0),
                    "fileInputClickEventCount": int(click_diag.get("fileInputClickEventCount") or 0),
                    "fileInputClickMethodCalls": int(click_diag.get("inputClickMethodCalls") or 0),
                    "fileInputShowPickerCalls": int(click_diag.get("inputShowPickerCalls") or 0),
                    "showOpenFilePickerCalls": int(click_diag.get("showOpenFilePickerCalls") or 0),
                    "showDirectoryPickerCalls": int(click_diag.get("showDirectoryPickerCalls") or 0),
                    "showSaveFilePickerCalls": int(click_diag.get("showSaveFilePickerCalls") or 0),
                    "fileInputAfterCount": int(click_diag.get("fileInputAfterCount") or 0),
                    "newFileInputCount": int(click_diag.get("newFileInputCount") or 0),
                    "fileInputsAfter": click_diag.get("fileInputsAfter") or [],
                    "newFileInputs": click_diag.get("newFileInputs") or [],
                    "activationEventTrace": click_diag.get("eventTrace") or [],
                })
            finally:
                cleanup("data-shirabe-upload-menu-action", menu_token)
            if chooser is None:
                trusted = int(common.get("activationTrustedClickEventCount") or 0)
                picker_calls = int(common.get("fileInputClickMethodCalls") or 0) + int(common.get("fileInputShowPickerCalls") or 0) + int(common.get("showOpenFilePickerCalls") or 0)
                if int(common.get("showOpenFilePickerCalls") or 0) > 0:
                    common["activationFailureMode"] = None
                    common["pickerTransport"] = "native-show-open-file-picker"
                    common["nativePickerApiObserved"] = True
                    common["fileChooserEventObserved"] = False
                    return None, {
                        **common,
                        "ok": True,
                        "stage": "native-picker-observed",
                        "reason": "native-show-open-file-picker-observed",
                    }
                elif int(common.get("fileInputShowPickerCalls") or 0) > 0:
                    failure_mode = "file-input-show-picker-invoked-without-playwright-filechooser"
                elif int(common.get("fileInputClickMethodCalls") or 0) > 0 or int(common.get("fileInputClickEventCount") or 0) > 0:
                    failure_mode = "file-input-click-invoked-without-playwright-filechooser"
                elif trusted > 0:
                    failure_mode = "trusted-click-delivered-no-picker-invocation"
                else:
                    failure_mode = "trusted-click-not-observed"
                if int(common.get("newFileInputCount") or 0) == 1:
                    failure_mode += "+unique-causal-file-input-observed"
                common["activationFailureMode"] = failure_mode
                return None, {**common, "ok": False, "stage": "filechooser", "reason": str(common.get("menuClickReason") or "filechooser-not-observed")}

        try:
            multiple = bool(chooser.is_multiple())
        except Exception:
            multiple = False
        accept = ""
        try:
            accept = str(chooser.element.get_attribute("accept") or "")
        except Exception:
            accept = ""
        common.update({
            "pickerTransport": "playwright-filechooser",
            "nativePickerApiObserved": False,
            "fileChooserEventObserved": True,
            "fileChooserMultiple": multiple,
            "fileChooserAccept": accept,
        })
        return chooser, {**common, "ok": True, "stage": "filechooser-observed", "reason": "filechooser-observed"}

    def observe_composer_attachment_transport(
        self,
        *,
        attachment_action_script: str,
        upload_menu_action_script: str,
        timeout_ms: int,
    ) -> dict[str, Any]:
        """Observe causal attachment semantics without assigning any file."""
        _chooser, result = self._bind_composer_filechooser(
            attachment_action_script=attachment_action_script,
            upload_menu_action_script=upload_menu_action_script,
            timeout_ms=timeout_ms,
        )
        return {**result, "fileAssigned": False, "promptDispatched": False}

    def upload_files_to_composer(
        self,
        file_paths: Sequence[str],
        *,
        attachment_action_script: str,
        upload_menu_action_script: str,
        timeout_ms: int,
    ) -> dict[str, Any]:
        """Attach local files through the same causal binding observed by Stage 1."""
        paths = [str(Path(path).expanduser().resolve()) for path in file_paths]
        bridge_token, bridge_arm = self._install_native_open_file_picker_bridge(paths)
        chooser = None
        result: dict[str, Any] = {}
        try:
            chooser, result = self._bind_composer_filechooser(
                attachment_action_script=attachment_action_script,
                upload_menu_action_script=upload_menu_action_script,
                timeout_ms=timeout_ms,
            )
            common = {
                **result,
                "fileCount": len(paths),
                "nativePickerBridgeArmOk": bool(bridge_arm.get("ok")),
                "nativePickerBridgeArmReason": bridge_arm.get("reason"),
            }
            if not result.get("ok"):
                return {**common, "ok": False}

            if chooser is not None:
                try:
                    chooser.set_files(paths)
                except Exception as exc:
                    return {**common, "ok": False, "stage": "filechooser-set-files", "reason": "filechooser-set-files-failed", "errorType": type(exc).__name__}
                return {**common, "ok": True, "stage": "filechooser-set-files", "reason": "files-assigned-via-filechooser", "fileAssigned": True, "pickerTransport": "playwright-filechooser"}

            if result.get("pickerTransport") != "native-show-open-file-picker" or bridge_token is None:
                return {**common, "ok": False, "stage": "native-picker-bridge", "reason": "native-picker-observed-without-bridge", "fileAssigned": False}

            bridge = self._collect_native_open_file_picker_bridge(bridge_token)
            common.update({
                "nativePickerBridgeCollectOk": bool(bridge.get("ok")),
                "nativePickerBridgeCalls": int(bridge.get("calls") or 0),
                "nativePickerBridgeGetFileCalls": int(bridge.get("getFileCalls") or 0),
                "nativePickerBridgeReturnedCount": int(bridge.get("returnedCount") or 0),
                "nativePickerBridgeReturnedNames": bridge.get("returnedNames") or [],
                "nativePickerBridgeRequestedOptions": bridge.get("requestedOptions") or [],
            })
            delivered = (
                bool(bridge.get("ok"))
                and int(bridge.get("calls") or 0) >= 1
                and int(bridge.get("returnedCount") or 0) == len(paths)
                and int(bridge.get("getFileCalls") or 0) >= len(paths)
                and tuple(str(name) for name in (bridge.get("returnedNames") or [])) == tuple(Path(path).name for path in paths)
            )
            if not delivered:
                return {**common, "ok": False, "stage": "native-picker-bridge", "reason": "native-picker-bridge-not-consumed", "fileAssigned": False}
            return {
                **common,
                "ok": True,
                "stage": "native-picker-bridge",
                "reason": "files-assigned-via-native-picker-bridge",
                "fileAssigned": True,
                "pickerTransport": "native-show-open-file-picker-bridge",
            }
        finally:
            if bridge_token is not None:
                self._restore_native_open_file_picker_bridge(bridge_token)


class _PlaywrightTransport:
    """One verified browser WebSocket and persistent, exact Page CDP sessions.

    Playwright supplies the connection and Page association only. Content/Input
    commands use CdpPage on the very session used for identity metadata.
    """
    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._browser_cdp = None
        self._page_map = {}
        self._handler = None
        self._dispatcher = None
        self._metadata_check = None
        self._navigation_complete = None
        self._closing = False

    def set_event_handler(self, callback) -> None:
        self._handler = callback

    def set_dispatcher(self, callback) -> None:
        self._dispatcher = callback

    def set_metadata_check(self, callback) -> None:
        self._metadata_check = callback

    def set_navigation_complete(self, callback) -> None:
        self._navigation_complete = callback

    def _emit(self, method, params=None) -> None:
        if not self._closing and self._handler is not None:
            self._handler(method, params or {})

    def discover(self, endpoint: str) -> str:
        validate_endpoint(endpoint)
        # Direct numeric loopback; http.client neither uses proxy env nor follows
        # redirects. Bound the entire read with a timer, not just idle recv.
        connection = http.client.HTTPConnection("127.0.0.1", 9222, timeout=5.0)
        expired = threading.Event()
        def expire():
            expired.set()
            sock = connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
        timer = threading.Timer(5.0, expire)
        timer.daemon = True
        timer.start()
        try:
            connection.request("GET", "/json/version", headers={"Connection": "close"})
            response = connection.getresponse()
            if response.status != 200:
                raise GuardError("CDP_CONNECT_FAILED")
            raw = response.read(65537)
            if expired.is_set() or len(raw) > 65536:
                raise GuardError("CDP_CONNECT_FAILED")
            data = strict_json_loads(raw)
            if type(data) is not dict:
                raise GuardError("BROWSER_EPOCH_UNVERIFIED")
            ws = data.get("webSocketDebuggerUrl")
            derive_browser_epoch(ws)
            return ws
        except GuardError:
            raise
        except Exception:
            raise GuardError("CDP_CONNECT_FAILED") from None
        finally:
            timer.cancel()
            connection.close()

    def connect(self, ws_url: str) -> None:
        derive_browser_epoch(ws_url)
        if self._browser is not None:
            raise GuardError("BINDING_REVOKED")
        try:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.connect_over_cdp(ws_url, timeout=10000)
            self._browser.on("disconnected", lambda *args: self._emit("CDP.disconnected"))
            self._browser_cdp = self._browser.new_browser_cdp_session()
            for method in ("Target.targetCreated", "Target.targetDestroyed", "Target.targetInfoChanged",
                           "Target.targetCrashed", "Target.detachedFromTarget", "Inspector.detached"):
                self._browser_cdp.on(method, lambda params, m=method: self._emit(m, params))
            self._browser_cdp.on("close", lambda *args: self._emit("CDP.disconnected"))
        except Exception:
            self.close()
            raise GuardError("CDP_CONNECT_FAILED") from None

    def pages(self) -> Sequence[_PageOps]:
        if not self.is_connected():
            raise GuardError("CDP_CONNECT_FAILED")
        current = []
        # Read live contexts/pages on every validation. Do not reuse a connection
        # snapshot, choose by URL/focus, or attach until the complete set is one.
        raw_pages = [(c, p) for c in self._browser.contexts for p in c.pages]
        for context, page in raw_pages:
            key = id(page)
            if key not in self._page_map:
                self._page_map[key] = _PageAssociation(context, page)
            current.append(self._page_map[key])
        return tuple(current)

    def _attach(self, association):
        if association.cdp is None:
            association.cdp = association.context.new_cdp_session(association.raw_page)
            association.cdp.on("close", lambda *args: self._emit("CDP.disconnected"))
            for method in ("Inspector.detached", "Inspector.targetCrashed", "Page.frameStartedLoading",
                           "Page.frameStoppedLoading", "Page.frameNavigated", "Page.navigatedWithinDocument"):
                association.cdp.on(method, lambda params, m=method: self._emit(m, params))
        return association.cdp

    def send(self, method: str, params: dict, *, page=None) -> dict:
        if not self.is_connected():
            raise GuardError("CDP_COMMAND_FAILED")
        if page is None:
            if method not in {"Target.setDiscoverTargets", "Target.getTargets", "Target.getTargetInfo"}:
                raise GuardError("PROTOCOL_UNSUPPORTED")
            cdp = self._browser_cdp
        else:
            if page not in self._page_map.values() or method not in {"Target.getTargetInfo", "Page.getFrameTree"}:
                raise GuardError("TARGET_BINDING_INVALID")
            cdp = self._attach(page)
        try:
            return cdp.send(method, params)
        except Exception:
            raise GuardError("PROTOCOL_UNSUPPORTED" if method in {"Target.setDiscoverTargets", "Target.getTargets"}
                             else "CDP_COMMAND_FAILED") from None

    def initialize_page(self, association) -> None:
        if association.ops is not None:
            return
        from .cdp_page import CdpPage
        info = self.send("Target.getTargetInfo", {}, page=association).get("targetInfo", {})
        proxy = CdpPage(self._attach(association), self._browser_cdp,
                        dispatch=self._dispatcher, metadata_check=self._metadata_check,
                        target_id=info.get("targetId"), expected_origin=EXPECTED_ORIGIN,
                        browser_context_id=info.get("browserContextId"),
                        navigation_complete=self._navigation_complete)
        association.proxy = proxy
        association.ops = _PlaywrightPageOps(association.context, proxy, dispatch=self._dispatcher)
        proxy.initialize()

    def is_connected(self) -> bool:
        return self._browser is not None and bool(self._browser.is_connected())

    def close(self) -> None:
        self._closing = True
        # Detach only our sessions. Never call browser.close/Target.closeTarget.
        for association in self._page_map.values():
            if association.cdp is not None:
                try:
                    association.cdp.detach()
                except Exception:
                    pass
        if self._browser_cdp is not None:
            try:
                self._browser_cdp.detach()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._page_map = {}
        self._browser_cdp = self._browser = self._playwright = None


class _PageAssociation:
    """Immutable Playwright Page mapping; content is delegated to one CDP proxy."""
    def __init__(self, context, page):
        self.context, self.raw_page = context, page
        self.cdp = self.proxy = self.ops = None

    @property
    def url(self):
        return str(self.raw_page.url)

    def __getattr__(self, name):
        if self.ops is None:
            raise GuardError("BINDING_REQUIRED")
        return getattr(self.ops, name)


@dataclass(frozen=True)
class _BoundPage:
    ops: _PageOps
    identity: PageIdentity


class _GuardedPageOps:
    """Every retained reference reaches the same live guard before dispatch."""
    def __init__(self, session, raw_ops, identity):
        self._session, self._raw_ops, self._last_identity = session, raw_ops, identity

    @property
    def url(self):
        self._session._validate_live()
        return self._last_identity.url

    def target_id(self):
        self._session._validate_live()
        return self._session._require_guard().record.target_id

    def title(self):
        return self._session._dispatch_primitive("title", self._raw_ops.title, mutation=False)

    def evaluate(self, expression, arg=None):
        # Arbitrary evaluate may mutate. Read-only profile diagnostics have a
        # separate consumer; this public primitive cannot bypass overlays.
        return self._session._dispatch_primitive("evaluate", lambda: self._raw_ops.evaluate(expression, arg), mutation=True)

    def goto(self, url, *, timeout_ms):
        normalize_origin(url)
        return self._session._dispatch_primitive("goto", lambda: self._raw_ops.goto(url, timeout_ms=timeout_ms), mutation=True,
                                                navigation=not isinstance(self._raw_ops, _PageAssociation))

    def capture_screenshot(self, destination_path):
        return self._session._dispatch_primitive("capture_screenshot", lambda: self._raw_ops.capture_screenshot(destination_path), mutation=False)

    def trusted_click_marked_action(self, selector, *, timeout_ms):
        return self._session._dispatch_primitive("trusted_click_marked_action", lambda: self._raw_ops.trusted_click_marked_action(selector, timeout_ms=timeout_ms), mutation=True)

    def download_from_action(self, expression, arg, destination_path, *, expected_filename, timeout_ms):
        return self._session._dispatch_primitive("download_from_action", lambda: self._raw_ops.download_from_action(expression, arg, destination_path, expected_filename=expected_filename, timeout_ms=timeout_ms), mutation=True)

    def observe_composer_attachment_transport(self, *, attachment_action_script, upload_menu_action_script, timeout_ms):
        return self._session._dispatch_primitive("observe_composer_attachment_transport", lambda: self._raw_ops.observe_composer_attachment_transport(attachment_action_script=attachment_action_script, upload_menu_action_script=upload_menu_action_script, timeout_ms=timeout_ms), mutation=True)

    def upload_files_to_composer(self, file_paths, *, attachment_action_script, upload_menu_action_script, timeout_ms):
        return self._session._dispatch_primitive("upload_files_to_composer", lambda: self._raw_ops.upload_files_to_composer(file_paths, attachment_action_script=attachment_action_script, upload_menu_action_script=upload_menu_action_script, timeout_ms=timeout_ms), mutation=True)


class ChatGPTSession:
    """Strict operation boundary; offline until an explicit live guard is installed.

    No Target discovery can mint a Binding. L03 owns explicit issuance and the
    injected guard owns current-generation/lease authority. A session cannot
    replace its guard or reconnect an invalidated operational channel.
    """
    def __init__(self, cdp_url: str = ENDPOINT, *, transport: _BrowserTransport | None = None,
                 binding_guard: BindingGuard | None = None) -> None:
        self.cdp_url = cdp_url
        self._transport = transport if transport is not None else _PlaywrightTransport()
        self._guard = None
        self._connected = False
        self._closed = False
        self._bound = None
        self._raw_ops = None
        self._frame = None
        self._ws_url = None
        self._initializing_discovery = False
        self._initializing_page = False
        self._navigation_settled_event = False
        self._explicit_navigation_pending = False
        self._primitive_lock = threading.RLock()
        self._facade_lock = threading.Lock()
        self._instrumented_lock = threading.Lock()
        self._instrumented_active = None
        self._instrumented_attempts = set()
        self._instrumented_provider_phase = False
        self._instrumented_provider_started = False
        self._operation_token = object()
        self._dispatch_count = 0
        self._mutation_count = 0
        self._last_dispatch = None
        if binding_guard is not None:
            self.install_guard(binding_guard)

    def install_guard(self, guard: BindingGuard) -> None:
        if self._guard is not None or self._connected or self._closed:
            raise GuardError("BINDING_REVOKED")
        if not isinstance(guard, BindingGuard):
            raise GuardError("BINDING_RECORD_INVALID")
        guard.check()
        self._guard = guard

    def _require_guard(self):
        if self._guard is None:
            raise GuardError("BINDING_REQUIRED")
        self._guard.check(allow_navigating=True)
        return self._guard

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _deny(self, code):
        if self._guard is not None:
            self._guard.invalidate(code)
            code = self._guard.reason
        raise GuardError(code)

    def _discover_same(self):
        guard = self._require_guard()
        validate_endpoint(self.cdp_url)
        if self.cdp_url != guard.record.endpoint:
            self._deny("ENDPOINT_NOT_ALLOWED")
        try:
            ws = self._transport.discover(self.cdp_url)
            epoch = derive_browser_epoch(ws)
        except GuardError as error:
            self._deny(error.code)
        except Exception:
            self._deny("CDP_CONNECT_FAILED")
        if epoch != guard.record.expected_browser_id or (self._ws_url is not None and ws != self._ws_url):
            self._deny("BROWSER_IDENTITY_MISMATCH")
        return ws

    def connect(self) -> None:
        guard = self._require_guard()
        try:
            validate_endpoint(self.cdp_url)
            if self.cdp_url != guard.record.endpoint:
                raise GuardError("ENDPOINT_NOT_ALLOWED")
        except GuardError as error:
            self._deny(error.code)
        if self._closed:
            self._deny("BINDING_REVOKED")
        if self._connected:
            if not self._transport.is_connected():
                self._deny("CDP_CONNECT_FAILED")
            return
        guard.check()
        try:
            ws = self._discover_same()
            self._transport.set_event_handler(self._on_event)
            self._transport.set_dispatcher(self._dispatch_primitive)
            metadata_setter = getattr(self._transport, "set_metadata_check", None)
            if metadata_setter is not None:
                metadata_setter(self.metadata_snapshot)
            navigation_setter = getattr(self._transport, "set_navigation_complete", None)
            if navigation_setter is not None:
                navigation_setter(self.navigation_complete)
            # Exactly the previously validated WS; never the HTTP endpoint.
            self._transport.connect(ws)
            self._connected = True
            self._ws_url = ws
            self._discover_same()
            self._initializing_discovery = True
            self._send("Target.setDiscoverTargets", {"discover": True, "filter": PAGE_FILTER})
        except GuardError as error:
            self._deny(error.code)
        except Exception:
            self._deny("CDP_CONNECT_FAILED")
        finally:
            self._initializing_discovery = False

    def _send(self, method, params, *, page=None):
        self._require_guard()
        try:
            result = self._transport.send(method, params, page=page)
        except GuardError as error:
            self._deny(error.code)
        except Exception:
            self._deny("PROTOCOL_UNSUPPORTED" if method in {"Target.getTargets", "Target.setDiscoverTargets"}
                       else "CDP_COMMAND_FAILED")
        self._require_guard()
        if type(result) is not dict:
            self._deny("TARGET_UNVERIFIED")
        return result

    def _on_event(self, method, params):
        # Callbacks latch only. They never reenter CDP, rebind, navigate or resend.
        guard = self._guard
        if self._closed or guard is None:
            return
        try:
            if type(params) is not dict or type(method) is not str:
                raise GuardError("TARGET_UNVERIFIED")
            if method in {"CDP.disconnected", "Inspector.detached", "Target.detachedFromTarget"}:
                raise GuardError("CDP_CONNECT_FAILED")
            if method in {"Target.targetCrashed", "Inspector.targetCrashed"}:
                raise GuardError("TARGET_BINDING_INVALID")
            if method == "Target.targetDestroyed":
                raise GuardError("TARGET_MISSING" if params.get("targetId") == guard.record.target_id else "TARGET_UNVERIFIED")
            if method in {"Target.targetCreated", "Target.targetInfoChanged"}:
                info = params.get("targetInfo")
                if type(info) is not dict:
                    raise GuardError("TARGET_UNVERIFIED")
                if info.get("type") != "page":
                    if info.get("targetId") == guard.record.target_id:
                        raise GuardError("TARGET_BINDING_INVALID")
                    if info.get("type") not in {"worker", "service_worker", "shared_worker", "iframe", "browser", "other"}:
                        raise GuardError("TARGET_UNVERIFIED")
                    return
                if info.get("targetId") != guard.record.target_id:
                    raise GuardError("SINGLE_PAGE_VIOLATION")
                if info.get("subtype", "") != "":
                    raise GuardError("TARGET_UNVERIFIED")
                normalize_origin(info.get("url"))
                if method == "Target.targetCreated" and not self._initializing_discovery:
                    raise GuardError("TARGET_UNVERIFIED")
                return
            if method == "Page.frameNavigated":
                frame = params.get("frame")
                if type(frame) is not dict:
                    raise GuardError("TARGET_UNVERIFIED")
                if "parentId" not in frame:
                    normalize_origin(frame.get("url"))
                    if self._frame is not None and frame.get("id") != self._frame[0]:
                        raise GuardError("TARGET_UNVERIFIED")
                return
            if method == "Page.navigatedWithinDocument":
                if self._frame is None or params.get("frameId") == self._frame[0]:
                    normalize_origin(params.get("url"))
                return
            if method in {"Page.frameStartedLoading", "Page.frameStoppedLoading"}:
                if self._frame is not None and params.get("frameId") == self._frame[0]:
                    if method == "Page.frameStartedLoading":
                        if guard.state == "BOUND":
                            guard.begin_navigation()
                        self._navigation_settled_event = False
                    else:
                        self._navigation_settled_event = True
                return
            if method.startswith(("Target.", "Page.", "Inspector.")):
                raise GuardError("TARGET_UNVERIFIED")
        except GuardError as error:
            guard.invalidate(error.code)
        except Exception:
            guard.invalidate("TARGET_UNVERIFIED")

    def _validate_live(self, *, mutation=False, allow_navigating=False):
        guard = self._require_guard()
        can_settle = (guard.state == "NAVIGATING" and self._navigation_settled_event
                      and not self._explicit_navigation_pending)
        guard.check(mutation=mutation, allow_navigating=allow_navigating or can_settle)
        self.connect()
        try:
            self._discover_same()
            selected = single_page(self._send("Target.getTargets", {"filter": PAGE_FILTER}).get("targetInfos"))
            if selected["targetId"] != guard.record.target_id:
                raise GuardError("TARGET_BINDING_INVALID")
            same_target(self._send("Target.getTargetInfo", {"targetId": guard.record.target_id}).get("targetInfo"), selected)
            pages = tuple(self._transport.pages())
            if len(pages) != 1:
                raise GuardError("TARGET_UNVERIFIED")
            raw = pages[0]
            if self._raw_ops is not None and raw is not self._raw_ops:
                raise GuardError("TARGET_BINDING_INVALID")
            if raw.url != selected["url"]:
                raise GuardError("TARGET_BINDING_INVALID")
            same_target(self._send("Target.getTargetInfo", {}, page=raw).get("targetInfo"), selected)
            frame = frame_snapshot(self._send("Page.getFrameTree", {}, page=raw), selected)
            if isinstance(raw, _PageAssociation):
                if str(raw.raw_page.main_frame.url) != frame[2]:
                    raise GuardError("TARGET_UNVERIFIED")
            same_target(single_page(self._send("Target.getTargets", {"filter": PAGE_FILTER}).get("targetInfos")), selected)
            same_target(self._send("Target.getTargetInfo", {"targetId": guard.record.target_id}).get("targetInfo"), selected)
            same_target(self._send("Target.getTargetInfo", {}, page=raw).get("targetInfo"), selected)
            if frame_snapshot(self._send("Page.getFrameTree", {}, page=raw), selected) != frame:
                raise GuardError("TARGET_UNVERIFIED")
            self._discover_same()
            guard.check(mutation=mutation, allow_navigating=allow_navigating or can_settle)
            if guard.state == "NAVIGATING" and can_settle:
                guard.navigation_settled()
            self._raw_ops, self._frame = raw, frame
            identity = PageIdentity(url=frame[2], title=None, target_id=guard.record.target_id)
            if self._bound is not None:
                self._bound.ops._last_identity = identity
            return raw, identity
        except GuardError as error:
            if error.code not in {"WORK_NOT_SETTLED", "RECONCILIATION_REQUIRED", "POLICY_DENIED"}:
                self._deny(error.code)
            raise
        except Exception:
            self._deny("CDP_COMMAND_FAILED")

    def metadata_snapshot(self):
        self._validate_live(allow_navigating=True)
        return self._frame

    def navigation_complete(self):
        """CdpPage calls only after exact frame/loader/lifecycle/context proof."""
        self._validate_live(allow_navigating=True)
        guard = self._require_guard()
        if guard.state == "NAVIGATING":
            guard.navigation_settled()
        elif guard.state != "BOUND":
            self._deny("TARGET_UNVERIFIED")
        self._explicit_navigation_pending = False
        self._navigation_settled_event = False

    def bind_chatgpt_page(self, *, exact_url=None, target_id=None) -> SessionBindResult:
        try:
            guard = self._require_guard()
            if target_id is not None and target_id != guard.record.target_id:
                self._deny("TARGET_BINDING_INVALID")
            raw, identity = self._validate_live()
            if exact_url is not None and exact_url != identity.url:
                self._deny("TARGET_BINDING_INVALID")
            if self._bound is None:
                self._bound = _BoundPage(_GuardedPageOps(self, raw, identity), identity)
                self._initializing_page = True
                try:
                    self._transport.initialize_page(raw)
                finally:
                    self._initializing_page = False
                self._validate_live()
            return SessionBindResult(status=WebOperationStatus.BOUND, page=identity,
                evidence=WebDebugEvidence("session.bind", ("binding_mode=strict-registered",),
                    {"binding_mode": "strict-registered", "binding_id": guard.record.binding_id,
                     "record_sha256": guard.record.record_hash, "browser_epoch": guard.record.expected_browser_id,
                     "selected_target_id": guard.record.target_id}))
        except GuardError as error:
            return SessionBindResult(status=WebOperationStatus.FAILED, page=None, failure=error.failure_kind,
                reason=error.code, evidence=WebDebugEvidence("session.bind", (error.code,), {"binding_mode":"strict-registered"}))
        except Exception:
            if self._guard is not None:
                self._guard.invalidate("CDP_COMMAND_FAILED")
            return SessionBindResult(status=WebOperationStatus.FAILED, page=None, failure=FailureKind.SESSION_BINDING_FAILURE,
                reason="CDP_COMMAND_FAILED", evidence=WebDebugEvidence("session.bind", ("CDP_COMMAND_FAILED",)))

    def check_operation(self, mutation=True):
        guard = self._require_guard()
        guard.check(mutation=mutation)
        if self._bound is None:
            result = self.bind_chatgpt_page()
            if not result.ok:
                raise GuardError(result.reason)
        self._validate_live(mutation=mutation)

    def require_reconciliation(self):
        if self._guard is not None:
            self._guard.require_reconciliation()

    def begin_operation(self):
        # Diagnostic hooks run outside this one-shot provider phase. Even a hook
        # that calls the original facade directly cannot recurse into mutation.
        if self._instrumented_active is not None and not self._instrumented_provider_phase:
            raise GuardError("OWNER_BUSY")
        if not self._facade_lock.acquire(blocking=False):
            raise GuardError("OWNER_BUSY")
        try:
            self._require_guard().begin_operation(self._operation_token)
        except BaseException:
            self._facade_lock.release()
            raise

    def end_operation(self):
        try:
            self._guard.end_operation(self._operation_token)
        finally:
            self._facade_lock.release()

    def begin_instrumented_operation(self, method, operation_id):
        """One admission shared by wrappers of this session, with no ID replay."""
        if method not in {"open_project_chat", "execute_prompt_and_wait", "download_response_zip"}:
            raise GuardError("BINDING_RECORD_INVALID")
        if type(operation_id) is not str or not operation_id or len(operation_id) > 512:
            raise GuardError("BINDING_RECORD_INVALID")
        if not self._instrumented_lock.acquire(blocking=False):
            raise GuardError("OWNER_BUSY")
        reserved = False
        try:
            key = (method, operation_id)
            if (self._instrumented_active is not None or key in self._instrumented_attempts
                    or len(self._instrumented_attempts) >= 10000):
                raise GuardError("RECONCILIATION_REQUIRED")
            guard = self._require_guard()
            guard.check(mutation=True)
            guard.begin_operation(self._operation_token)
            reserved = True
            guard.claim_operation_id(method, operation_id)
            token = object()
            self._instrumented_attempts.add(key)
            self._instrumented_active = token
            self._instrumented_provider_phase = False
            self._instrumented_provider_started = False
            return token
        except BaseException:
            if reserved:
                self._guard.end_operation(self._operation_token)
            raise
        finally:
            self._instrumented_lock.release()

    def begin_instrumented_provider(self, token):
        with self._instrumented_lock:
            if (token is None or token is not self._instrumented_active
                    or self._instrumented_provider_started or self._instrumented_provider_phase):
                raise GuardError("RECONCILIATION_REQUIRED")
            self._require_guard().check(mutation=True)
            self._instrumented_provider_started = True
            self._instrumented_provider_phase = True

    def end_instrumented_provider(self, token):
        with self._instrumented_lock:
            if token is None or token is not self._instrumented_active:
                self.require_reconciliation()
                raise GuardError("RECONCILIATION_REQUIRED")
            self._instrumented_provider_phase = False

    def end_instrumented_operation(self, token):
        with self._instrumented_lock:
            if token is None or token is not self._instrumented_active:
                self.require_reconciliation()
                raise GuardError("RECONCILIATION_REQUIRED")
            try:
                self._guard.end_operation(self._operation_token)
            finally:
                self._instrumented_active = None
                self._instrumented_provider_phase = False

    @property
    def dispatch_state(self):
        return {"primitive_dispatches": self._dispatch_count, "mutation_dispatches": self._mutation_count,
                "last_primitive": self._last_dispatch}

    def _dispatch_primitive(self, name, callback, *, mutation, navigation=False):
        if not self._primitive_lock.acquire(blocking=False):
            raise GuardError("OWNER_BUSY")
        reserved = False
        guard = None
        try:
            guard = self._require_guard()
            guard.begin_operation(self._operation_token)
            reserved = True
            self._validate_live(mutation=mutation)
            guard = self._require_guard()
            if navigation:
                guard.begin_navigation()
                self._explicit_navigation_pending = name == "Page.navigate"
            self._dispatch_count += 1
            self._mutation_count += int(mutation)
            self._last_dispatch = name
            try:
                result = callback()
            except BaseException as error:
                # Preserve the original exception object while retaining the
                # fact that this callback was entered. This is deliberately not
                # proof that a prompt was accepted; inner click evidence owns
                # activation/establishment semantics.
                try:
                    if not hasattr(error, "dispatched"):
                        error.dispatched = True
                    if not hasattr(error, "dispatch_stage"):
                        error.dispatch_stage = "CALLBACK_ENTERED"
                except Exception:
                    pass
                if mutation:
                    self.require_reconciliation()
                if isinstance(error, GuardError) and error.code not in {
                    "WORK_NOT_SETTLED", "RECONCILIATION_REQUIRED", "POLICY_DENIED", "OWNER_BUSY"
                }:
                    guard.invalidate(error.code)
                if navigation or guard.state == "NAVIGATING":
                    guard.invalidate("TARGET_UNVERIFIED")
                # Preserve the exact original exception, including if a final
                # metadata check or clock sample fails. Never repeat callback.
                try:
                    self._validate_live(allow_navigating=navigation)
                except Exception:
                    pass
                raise
            try:
                self._validate_live(allow_navigating=navigation)
                if navigation and name != "Page.navigate" and guard.state == "NAVIGATING":
                    guard.navigation_settled()
            except GuardError as error:
                if mutation:
                    self.require_reconciliation()
                error.dispatched = True
                error.original_result = result
                raise
            return result
        finally:
            if reserved:
                try:
                    guard.end_operation(self._operation_token)
                except Exception:
                    self.require_reconciliation()
            self._primitive_lock.release()

    def _bound_page(self):
        if self._bound is None:
            return None
        try:
            self._require_guard().check()
        except GuardError:
            return None
        return self._bound

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._guard is not None:
            self._guard.invalidate("BINDING_REVOKED")
        self._bound = None
        try:
            self._transport.close()
        finally:
            self._connected = False

    def capture_bound_screenshot(self, destination_path):
        bound = self._bound_page()
        if bound is None:
            return False
        try:
            return bool(bound.ops.capture_screenshot(str(destination_path)))
        except Exception:
            return False

    def _download_from_bound_action(self, expression, arg, destination_path, *, expected_filename, timeout_ms):
        bound = self._bound_page()
        if bound is None:
            return {"ok": False, "status": "unbound", "eventObserved": False,
                    "suggestedFilename": None, "suggestedFilenameMatch": None, "transferComplete": False}
        return bound.ops.download_from_action(expression, arg, str(destination_path), expected_filename=expected_filename, timeout_ms=timeout_ms)

    def _observe_bound_composer_attachment_transport(self, *, attachment_action_script, upload_menu_action_script, timeout_ms):
        bound = self._bound_page()
        if bound is None:
            return {"ok": False, "reason": "unbound", "stage": "session", "fileChooserEventObserved": False}
        return bound.ops.observe_composer_attachment_transport(attachment_action_script=attachment_action_script, upload_menu_action_script=upload_menu_action_script, timeout_ms=timeout_ms)

    def _upload_files_to_bound_composer(self, file_paths, *, attachment_action_script, upload_menu_action_script, timeout_ms):
        bound = self._bound_page()
        if bound is None:
            return {"ok": False, "reason": "unbound", "fileCount": 0}
        return bound.ops.upload_files_to_composer(tuple(str(p) for p in file_paths), attachment_action_script=attachment_action_script, upload_menu_action_script=upload_menu_action_script, timeout_ms=timeout_ms)


def _identity_of(page: _PageOps) -> PageIdentity:
    # Reporting a failure must not touch an invalidated Page or mask the original
    # submission/result/exception. Identity here is explicitly last-verified data.
    cached = getattr(page, "_last_identity", None)
    if isinstance(cached, PageIdentity):
        return cached
    return PageIdentity(url="", title=None, target_id=None)
