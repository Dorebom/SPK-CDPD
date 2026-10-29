/* Offline DOM fixture: executes only the supplied versioned collector script.
 * No browser, network, CDP, or arbitrary production execution entry point.
 */
'use strict';
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const observations = {rectReads:0, forbiddenReads:[], matchedSelectors:[], nodesCreated:0};
const selectors = [
  '#prompt-textarea, textarea[data-testid*="prompt" i], [contenteditable="true"][role="textbox"]',
  'button[data-testid="send-button"], button[data-testid*="send" i]',
  'button[data-testid*="stop" i]',
  'input[type="file"]',
  'article[data-testid^="conversation-turn-"], [data-message-author-role="assistant"]',
];
function forbidden(name) {
  observations.forbiddenReads.push(name);
  throw new Error('FORBIDDEN_FIXTURE_READ');
}
class Element {
  constructor(tag, options={}) {
    observations.nodesCreated++;
    this.localName = tag;
    this.options = options;
    this.firstElementChild = null;
    this.nextElementSibling = null;
    this.shadowRoot = null;
  }
  append(...children) {
    this.firstElementChild = children[0] || null;
    children.forEach((child,index) => {child.nextElementSibling = children[index+1] || null;});
    return this;
  }
  getAttribute(name) {
    if (!['role','aria-disabled','aria-hidden'].includes(name)) return forbidden('attribute:'+name);
    return this.options[name] || null;
  }
  hasAttribute(name) {
    if (!['disabled','hidden'].includes(name)) return forbidden('attribute-presence:'+name);
    return this.options[name] === true;
  }
  getBoundingClientRect() {
    observations.rectReads++;
    return {x:1.123,y:2.789,width:100.234,height:20.345};
  }
  matches(selector) {
    const index = selectors.indexOf(selector);
    if (index < 0) return forbidden('arbitrary-selector');
    observations.matchedSelectors.push(index);
    return (this.options.features || []).includes(index);
  }
  get children() {return forbidden('unbounded-children-array');}
  get innerText() {return forbidden('innerText');}
  get textContent() {return forbidden('textContent');}
  get innerHTML() {return forbidden('innerHTML');}
  get outerHTML() {return forbidden('outerHTML');}
  get value() {return forbidden('value');}
  get id() {return forbidden('id');}
  get className() {return forbidden('className');}
  get contentDocument() {return forbidden('iframe-contentDocument');}
  get contentWindow() {return forbidden('iframe-contentWindow');}
}
const html = new Element('html');
const body = new Element('body');
html.append(body);
if (input.mode === 'heavy') {
  const children = [];
  for (let i=0;i<5000;i++) children.push(new Element(i % 2 ? 'button' : 'div', {features: i % 2 ? [1] : []}));
  body.append(...children);
} else if (input.mode === 'empty_shadow_budget') {
  const hosts = [];
  for (let i=0;i<8;i++) {
    const host = new Element('section', {features:[1]});
    host.shadowRoot = new Element('shadow-root-fixture');
    hosts.push(host);
  }
  body.append(...hosts);
} else {
  const host = new Element('div');
  const shadowInput = new Element('textarea', {role:'textbox',features:[0]});
  const shadowSend = new Element('button', {role:'button',features:[1],disabled:true});
  const shadow = new Element('shadow-root-fixture');
  shadow.append(shadowInput,shadowSend);
  host.shadowRoot = shadow;
  const iframe = new Element('iframe');
  const closedHost = new Element('private-custom-tag-SYNTHETIC_SECRET');
  closedHost.shadowRoot = null;
  // Closed shadow content is deliberately unreachable; it must not appear.
  closedHost.closedSecret = new Element('input', {features:[3]});
  const attachment = new Element('input', {features:[3]});
  const stop = new Element('button', {features:[2],hidden:true,display:'none'});
  const response = new Element('article', {features:[4]});
  const script = new Element('script');
  const extraHosts = [];
  if (input.mode === 'empty_shadow') {
    const emptyHost = new Element('section');
    emptyHost.shadowRoot = new Element('shadow-root-fixture');
    extraHosts.push(emptyHost);
  }
  body.append(host,...extraHosts,iframe,closedHost,attachment,stop,response,script);
}
const sandbox = {
  document:{documentElement:html},
  getComputedStyle:(el)=>({display:el.options.display || 'block',visibility:'visible',opacity:'1'}),
};
try {
  const structure = vm.runInNewContext(input.script,sandbox,{timeout:1000});
  process.stdout.write(JSON.stringify({structure,observations})+'\n');
} catch (_) {
  process.stdout.write(JSON.stringify({failure_code:'FIXED_SCRIPT_FAILED',observations})+'\n');
  process.exitCode=1;
}
