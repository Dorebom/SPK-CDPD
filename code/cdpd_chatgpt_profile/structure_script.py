"""Versioned fixed read-only recipe; adapted from accepted v0.2.1 structure.

Selectors are compiled local constants, never supplied by the page or caller.
They inspect node identity in memory but never return selector attributes,
labels, text, input values, URLs, arbitrary attributes, or raw DOM.
"""
from __future__ import annotations
import hashlib
import json

SCRIPT_VERSION = "cdpd.chatgpt-structure-script/v2"
SCRIPT_FAILURE_SCHEMA = "cdpd.capture-script-failure/v1"

PROBE_CATALOG = {
    "schema": "cdpd.chatgpt-capture-probes/v1",
    "features": [
        {"feature_id": "composer.input", "selector": '#prompt-textarea, textarea[data-testid*="prompt" i], [contenteditable="true"][role="textbox"]'},
        {"feature_id": "composer.send", "selector": 'button[data-testid="send-button"], button[data-testid*="send" i]'},
        {"feature_id": "generation.stop", "selector": 'button[data-testid*="stop" i]'},
        {"feature_id": "attachment.input", "selector": 'input[type="file"]'},
        {"feature_id": "response.container", "selector": 'article[data-testid^="conversation-turn-"], [data-message-author-role="assistant"]'},
    ],
    "scope": "MAIN_FRAME_AND_OPEN_SHADOW",
    "source": "ACCEPTED_V021_FIXED_MATCHER_SUBSET_NOT_LIVE_SITE_VALIDATION",
}
CATALOG_BYTES = json.dumps(PROBE_CATALOG, sort_keys=True, separators=(",", ":")).encode()
CATALOG_SHA256 = hashlib.sha256(CATALOG_BYTES).hexdigest()
FEATURE_IDS = tuple(item["feature_id"] for item in PROBE_CATALOG["features"])

# A bounded DFS uses sibling links instead of Array.from(el.children): an
# element with millions of children must not allocate an unbounded stack.
# S1 projections reserve <=200 of the shared S1+S2 node budget. Each projected
# match consumes one output node; unused reservation is returned to S2.
SCRIPT_TEMPLATE = r"""(() => {
  // Fixed recipe: __SCRIPT_VERSION__. Origin is checked before any DOM access.
  let originAllowed = false;
  try { originAllowed = location.origin === 'https://chatgpt.com'; } catch (_) {}
  if (!originAllowed) {
    return {schema:'cdpd.capture-script-failure/v1',failure_code:'TARGET_ORIGIN_MISMATCH'};
  }
  const maxNodes = __MAX_NODES__;
  const catalog = __CATALOG__;
  const tags = new Set('html head body div span main section article aside nav header footer form button input textarea select option label a p ul ol li table tbody thead tr th td svg path g img canvas video audio iframe slot h1 h2 h3 h4 h5 h6 br hr dialog details summary fieldset legend'.split(' '));
  const roles = new Set('button textbox checkbox radio switch link menu menuitem list listitem tab tablist tabpanel dialog alert status progressbar combobox option heading navigation main region group presentation none'.split(' '));
  const rows = []; const shapes = {}; const probes = [];
  const reserve = Math.min(200, maxNodes);
  let s1Count = 0; let visited = 0; let truncated = false;
  let iframeCount = 0;
  const openShadowHosts = [];
  for (const item of catalog) {
    shapes[item.feature_id] = [];
    probes.push({feature_id:item.feature_id,match_count:0,visible_count:0,enabled_count:0,probe_complete:true});
  }
  const stack = document.documentElement ? [{el:document.documentElement,parent:-1,shadow:false,siblings:false}] : [];
  while (stack.length) {
    if (visited >= maxNodes) { truncated = true; break; }
    const task = stack.pop(); const el = task.el;
    if (task.siblings && el.nextElementSibling) stack.push({el:el.nextElementSibling,parent:task.parent,shadow:task.shadow,siblings:true});
    visited++;
    const name = String(el.localName || '').toLowerCase();
    if (name === 'script' || name === 'style' || name === 'noscript') continue;
    const tag = tags.has(name) ? name : '#other';
    const rawRole = el.getAttribute('role'); const role = roles.has(rawRole) ? rawRole : null;
    const rect = el.getBoundingClientRect(); const style = getComputedStyle(el);
    const disabled = el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true';
    const hidden = el.hasAttribute('hidden') || el.getAttribute('aria-hidden') === 'true';
    const visible = style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
    const index = rows.length;
    rows.push({index,parent:task.parent,tag,role,shadowBoundary:task.shadow,disabled,hidden,visible,
      box:[rect.x,rect.y,rect.width,rect.height].map(x=>Number.isFinite(x)?Math.round(x*10)/10:0)});
    if (tag === 'iframe') iframeCount++;
    for (let i=0; i<catalog.length; i++) {
      if (!el.matches(catalog[i].selector)) continue;
      probes[i].match_count++; if(visible) probes[i].visible_count++; if(!disabled) probes[i].enabled_count++;
      if (s1Count < reserve) {
        shapes[catalog[i].feature_id].push({tag,role,parent:null,shadow:task.shadow}); s1Count++;
      } else { probes[i].probe_complete = false; truncated = true; }
    }
    if (el.firstElementChild) stack.push({el:el.firstElementChild,parent:index,shadow:false,siblings:true});
    const shadowRoot = el.shadowRoot;
    if (shadowRoot) {
      openShadowHosts.push(index);
      if (shadowRoot.firstElementChild) stack.push({el:shadowRoot.firstElementChild,parent:index,shadow:true,siblings:true});
    }
  }
  if (stack.length) for (const probe of probes) probe.probe_complete = false;
  const room = Math.max(0,maxNodes-s1Count);
  const nodes = rows.slice(0,room);
  if (nodes.length !== rows.length) truncated = true;
  // Counts describe retained S2 hosts. As in v0.2.1, an observed open root
  // counts even when empty; hosts trimmed by the shared budget do not count.
  iframeCount = nodes.filter(n=>n.tag==='iframe').length;
  const openShadowCount = openShadowHosts.filter(index=>index<nodes.length).length;
  return {schema:'cdpd.structure/v1',nodes,truncated,iframeCount,openShadowCount,
    coverage:{scope:'MAIN_FRAME_AND_OPEN_SHADOW',iframeDocuments:'NOT_CAPTURED',closedShadow:'NOT_OBSERVABLE',accessibilityTree:'NOT_CAPTURED'},
    observation:'STRUCTURE_ONLY_NOT_FUNCTIONAL_VERIFICATION',profile:{probes,feature_shapes:shapes}};
})()""".replace("__SCRIPT_VERSION__", SCRIPT_VERSION).replace("__CATALOG__", json.dumps(PROBE_CATALOG["features"], separators=(",", ":")))


def capture_script(max_nodes: int) -> str:
    if type(max_nodes) is not int or not 1 <= max_nodes <= 2000:
        raise ValueError("INPUT_INVALID")
    return SCRIPT_TEMPLATE.replace("__MAX_NODES__", str(max_nodes))


def script_sha256(max_nodes: int) -> str:
    return hashlib.sha256(capture_script(max_nodes).encode()).hexdigest()
