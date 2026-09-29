"""Ask Scout in the page (WS2, 2026-09-28): a floating button on every card opens a slide-over
panel where the reader asks a question in the context they are already in (the card's competitor,
the audience they picked). Progress streams in by stage, then the answer: paragraphs with [n]
citations, each source with the same class chip as the card, the cited excerpt one tap away, what
could not be verified, the Cut Log, and the closing line. Nothing is a page; `/answers/<id>` is the
permalink and the durable artifact.

Two modes behind one endpoint (`POST /api/ask`):
  canned   RC review at $0 (config.ASK_CANNED_ID): the stored answer replays with realistic stage
           timing so the interaction can be judged before the engine exists.
  engine   (WS2 step 4b) the viewer relays the engine's SSE.
The button renders only when config.ASK_ENABLED; production stays byte-identical until the flip.
"""
from __future__ import annotations

import html as _html
import json
import re

from scout import config
from scout.sources import classify

# (key, modes, reader-facing label, time hint while live: {mode: hint}). Reader-facing on purpose
# (Uroš, 2026-09-28): what is happening and why it is worth the wait, never the pipeline's names.
# Measured: quick draft ~15 s + verify ~20 s; deep research 110-250 s, verify 30-60 s, the
# optional repair + second check 30-100 s.
STAGES = [
    ("facts", "qd", "Starting from what Scout already verified", {}),
    ("draft", "q", "Writing an answer from what Scout knows", {"q": "about 15 s"}),
    ("search", "d", "Finding and reading sources", {"d": "Scout reads the pages itself, usually 1 to 3 min"}),
    ("ground", "d", "Confirming each quote is really on its page", {"d": "about 15 s"}),
    ("floor", "qd", "Matching every number to its source", {}),
    ("verify", "qd", "Fact-checking each sentence before you see it",
     {"q": "a second model checks every claim; anything it can't confirm is cut, about 20 s",
      "d": "a second model checks every claim; anything it can't confirm is cut, about a minute"}),
    ("rewrite", "d", "Fixing sentences that didn't hold up", {"d": "rewritten and checked again rather than kept"}),
    ("done", "qd", "Ready", {}),
]
WHY_WAIT = "Scout answers, then fact-checks every sentence before showing it. What can't be verified is cut, not shown."
POLL_MS = 8000          # recovery poll interval after a dropped stream
POLL_MAX_MS = 12 * 60 * 1000
# The three example chips (Uroš, 2026-09-28): what a rep actually asks, filled in per card.
EXAMPLES = {
    "default": ["The buyer says {competitor} is cheaper. Is it?",
                "What has {competitor} shipped or changed in the last 30 days?",
                "What will a {competitor} rep say against us, and is it true?"],
}
_TIER_TITLE = {"primary": "Primary source: the document itself, or the company's own statement",
               "reputable_secondary": "Reputable secondary source",
               "sentiment_only": "Sentiment: reviews or discussion, not a fact source"}


def _chip(cls, tier=None) -> str:
    if cls == "scout_take":
        return '<span class="srcclass srcclass-scout_take" title="Scout\'s own battlecard judgment, not a source">Scout\'s take</span>'
    label = classify.CLASS_LABEL.get(cls or "unknown", "Web")
    title = _TIER_TITLE.get(tier or classify.CLASS_TIER.get(cls or "") or "", "")
    return (f'<span class="srcclass srcclass-{_html.escape(str(cls or "unknown"))}"'
            + (f' title="{_html.escape(title)}"' if title else "") + f'>{_html.escape(label)}</span>')


def _cites(text: str, cites: list, key: str | None = None) -> str:
    body = _html.escape(text)
    if key and key in text:                          # the clause the model marked, verbatim: bold it
        body = body.replace(_html.escape(key), f"<b>{_html.escape(key)}</b>", 1)
    marks = "".join(f'<a class="ask-cite" href="#ask-src-{n}" data-n="{n}">{n}</a>' for n in cites)
    return f"{body} {marks}" if marks else body


def answer_html(a: dict, permalink: bool = True, show_question: bool = True, chat: bool = False) -> str:
    """Server-rendered answer body (one renderer for the panel and the permalink page). In the
    thread the reader's own bubble carries the question, so the echo is off there; `chat` collapses
    sources / could-not-verify / cut log behind a summary line and offers Research deeper."""
    if a.get("failed"):
        return f'<div class="ask-answer ask-failed"><p class="ask-p ask-none">{_html.escape(str(a.get("error") or "Scout could not answer that."))}</p></div>'
    paras = "".join(f'<p class="ask-p">{_cites(p.get("text", ""), p.get("cites", []), p.get("key"))}</p>' for p in a.get("paragraphs") or [])
    if not paras:
        paras = '<p class="ask-p ask-none">Scout could not verify an answer to this question. What it tried is in the Cut Log.</p>'
    srcs = ""
    for s in a.get("sources") or []:
        url = s.get("url") or "#"
        if s.get("take") or s.get("class") == "scout_take":
            # a battlecard judgment: linked to its card, never dressed as a page
            label, target = f'{s.get("card") or "battlecard"}', ' target="_blank" rel="noopener"' if url.startswith("http") else ""
        else:
            label, target = re.sub(r"^www\.", "", (re.sub(r"^https?://", "", url).split("/")[0])), ' target="_blank" rel="noopener"'
        quotes = [x for x in (s.get("excerpts") or [s.get("excerpt")]) if x]
        exc = "".join(f"<blockquote>{_html.escape(str(x))}</blockquote>" for x in quotes)
        srcs += (f'<li id="ask-src-{s["n"]}" class="ask-src"><span class="ask-n">{s["n"]}</span>'
                 f'<a href="{_html.escape(url)}"{target}>{_html.escape(label)}</a>'
                 f'{_chip(s.get("class"), s.get("tier"))}'
                 + (f'<span class="ask-asof">{_html.escape(str(s.get("as_of")))}</span>' if s.get("as_of") else "")
                 + (f'<details class="ask-exc"><summary>quote{"s" if len(quotes) > 1 else ""}</summary>{exc}</details>' if exc else "")
                 + "</li>")
    unans = "".join(f"<li>{_html.escape(u)}</li>" for u in a.get("unanswered") or [])
    cuts = "".join(f'<li><b>{_html.escape(str(c.get("label") or ""))}</b> {_html.escape(str(c.get("reason") or ""))}</li>' for c in a.get("cut_log") or [])
    n_src, n_un, n_cut = len(a.get("sources") or []), len(a.get("unanswered") or []), len(a.get("cut_log") or [])
    quick = a.get("kind") == "quick"
    aid = _html.escape(str(a.get("id") or ""))
    # the chat shows the answer and one quiet line of links; sources, what could not be verified and the
    # cut log open in place (Uroš, 2026-09-28: "this is a chat bot, not the battlecard")
    hid = " hidden" if chat else ""               # the permalink page has no toggles: everything open
    more = "" if not chat else "".join(x for x in [
        (f'<button type="button" class="ask-tgl" data-for="src-{aid}">{n_src} source{"s" if n_src != 1 else ""}</button>' if n_src else ""),
        (f'<button type="button" class="ask-tgl" data-for="un-{aid}">{n_un} could not verify</button>' if n_un else ""),
        (f'<button type="button" class="ask-tgl" data-for="cut-{aid}">{n_cut} cut</button>' if n_cut else "")])
    secs = ((f'<div class="ask-sec" id="src-{aid}"{hid}><span class="ey">Sources</span><ol class="ask-srcs">{srcs}</ol></div>' if srcs else "")
            + (f'<div class="ask-sec ask-unans" id="un-{aid}"{hid}><span class="ey">Could not verify</span><ul>{unans}</ul></div>' if unans else "")
            + (f'<div class="ask-sec ask-cut" id="cut-{aid}"{hid}><span class="ey">Cut log</span><ul>{cuts}</ul></div>' if cuts else ""))
    how = ("answered from what Scout already knew" if quick else "researched and fact-checked")
    foot = f'<span class="ask-vmark">Verified</span> · {how} · {float(a.get("seconds") or 0):.0f}&nbsp;s'
    deeper = (f'<button type="button" class="ask-deeper" data-q="{_html.escape(a.get("question") or "")}">Research deeper · 2 to 5 min</button>'
              if quick and chat else "")
    link = f'<a class="ask-link" href="/answers/{aid}">Copy link</a>' if permalink and a.get("id") else ""
    q_echo = f'<div class="ask-q">{_html.escape(a.get("question") or "")}</div>' if show_question else ""
    scope = a.get("card") or a.get("competitor")
    about = f'<div class="ask-about">About {_html.escape(str(scope))}</div>' if scope else ""
    return (f'<div class="ask-answer{" ask-quick" if quick else ""}" data-id="{aid}">'
            f'{q_echo}{about}{paras}'
            + (f'<div class="ask-more">{more}</div>' if more else "")
            + secs
            + f'<div class="ask-foot"><span>{foot}</span>{deeper}{link}</div></div>')


def button_and_panel_html(slug: str | None, meta: dict | None, persona: str | None) -> str:
    """The floating button + the panel shell + its JS. Empty string when Ask is off.

    The panel is a CONVERSATION (Uroš, 2026-09-28: "it should be a conversation, like a chat bot,
    not one question at a time"): every question and answer stays in the thread, the composer is
    always at the bottom, and each follow-up sends the thread so far (questions + answer ids) so
    the engine answers in context and reuses the facts it already verified."""
    if not config.ASK_ENABLED:
        return ""
    meta = meta or {}
    comp = (meta.get("competitor") or "").strip()
    # One thread across every card (Uroš, 2026-09-28): the scope comes from the question; the card
    # the reader is on is only a hint (never shown in the header).
    plabel = {"eng_led": "an eng-led champion", "technical_evaluator": "a technical evaluator", "economic_buyer": "an economic buyer",
              "security_regulated": "a security & regulated buyer", "exec_top_down": "an exec / top-down buyer"}.get(persona or "", "")
    ctx = "Ask Scout about any competitor" + (f" · for {plabel}" if plabel else "")
    examples = [e.replace("{competitor}", comp or "the competitor") for e in EXAMPLES["default"]]
    ex_html = "".join(f'<button type="button" class="ask-ex">{_html.escape(e)}</button>' for e in examples)
    stages_js = json.dumps([{"k": k, "m": m, "t": t, "h": h} for k, m, t, h in STAGES])
    canned = bool(config.ASK_CANNED_ID)
    cfg = json.dumps({"slug": slug, "competitor": comp, "my_company": meta.get("my_company") or "", "persona": persona or "",
                      "canned": canned, "poll_ms": POLL_MS, "poll_max_ms": POLL_MAX_MS, "why": WHY_WAIT})
    review = ('<div class="ask-review">Review build: every question replays one stored answer.</div>' if canned else "")
    # The script is a RAW template, not part of the f-string: an f-string turned the JS `'\n\n'`
    # into two real newlines inside a string literal (a syntax error that silently killed every
    # handler on RC, 2026-09-28), and its brace doubling made the JS unreadable. `onsubmit="return
    # false"` is the belt to that: even with no script the form can never navigate the page.
    script = _PANEL_JS.replace("__CFG__", cfg).replace("__STAGES__", stages_js)
    return f'''
<button type="button" class="ask-fab" id="ask-fab" aria-haspopup="dialog" aria-controls="ask-panel">
  <span class="ask-fab-dot"></span>Ask Scout<span class="ask-fab-n" id="ask-fab-n" hidden></span></button>
<aside class="ask-panel" id="ask-panel" role="dialog" aria-labelledby="ask-title" hidden>
  <div class="ask-head"><div><h3 id="ask-title" class="ask-name">Ask Scout <span class="ask-beta">Beta</span></h3><div class="ask-sub">{_html.escape(ctx)}</div></div>
    <div class="ask-headbtns"><button type="button" class="ask-clear" id="ask-clear" title="Start a new thread" hidden>New thread</button>
    <button type="button" class="ask-close" id="ask-close" aria-label="Minimize" title="Minimize">–</button></div></div>
  <div class="ask-thread" id="ask-thread" aria-live="polite">
    {review}
    <div class="ask-intro" id="ask-intro">
      <p>Every sentence in an answer was fact-checked against a verified source, or it is not in the answer. A quick answer from what Scout already knows takes about half a minute; "Research deeper" searches the web and takes a few minutes. The thread stays with you across cards and visits.</p>
      <div class="ask-exs">{ex_html}</div>
    </div>
  </div>
  <form class="ask-composer" id="ask-form" onsubmit="return false">
    <textarea id="ask-q" rows="1" maxlength="400" placeholder="Ask a question" aria-label="Your question" enterkeyhint="send"></textarea>
    <button type="submit" class="ask-go" id="ask-go" aria-label="Ask">Ask</button>
  </form>
</aside>
<script>{script}</script>'''


# The widget's script. Plain JS (single braces, real backslash escapes); `__CFG__` and `__STAGES__`
# are replaced with JSON at render time. tests/test_viewer_routes.py syntax-checks the rendered
# script with node and drives it in a headless browser.
_PANEL_JS = r"""(function(){
var CFG=__CFG__, STAGES=__STAGES__;
var fab=document.getElementById('ask-fab'), panel=document.getElementById('ask-panel');
var form=document.getElementById('ask-form'), q=document.getElementById('ask-q'), go=document.getElementById('ask-go');
var thread=document.getElementById('ask-thread'), intro=document.getElementById('ask-intro'), clearBtn=document.getElementById('ask-clear');
var fabN=document.getElementById('ask-fab-n');
var KEY='scout_ask_thread_v1', OPEN_KEY='scout_ask_open_v1', PEND_KEY='scout_ask_pending_v1', MAX=20;
var history=[], pending=null, busy=false, lastFocus=null, timer=null, t0=0, acts=[], cur=null, mode='q';
function lsGet(k){try{return localStorage.getItem(k);}catch(e){return null;}}
function lsSet(k,v){try{if(v===null)localStorage.removeItem(k);else localStorage.setItem(k,v);}catch(e){}}
function load(){try{var arr=JSON.parse(lsGet(KEY)||'[]');return Array.isArray(arr)?arr.slice(-MAX):[];}catch(e){return [];}}
function save(){lsSet(KEY,JSON.stringify(history.slice(-MAX)));}
function loadPending(){try{var p=JSON.parse(lsGet(PEND_KEY)||'null');return (p&&p.answer_id&&p.question&&(Date.now()-(p.started||0))<CFG.poll_max_ms)?p:null;}catch(e){return null;}}
function savePending(p){pending=p;lsSet(PEND_KEY,p?JSON.stringify(p):null);}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function token(){var a=new Uint8Array(16);try{crypto.getRandomValues(a);}catch(e){for(var i=0;i<16;i++)a[i]=Math.floor(Math.random()*256);}return Array.prototype.map.call(a,function(b){return ('0'+b.toString(16)).slice(-2);}).join('');}
function badge(){var n=history.length;fabN.hidden=!n;fabN.textContent=n?String(n):'';}
var FINE=window.matchMedia&&window.matchMedia('(pointer: fine)').matches;
// on touch, a programmatic focus pops the keyboard and moves the layout: the reader taps when ready
function focusQ(){if(!FINE)return;try{q.focus({preventScroll:true});}catch(e){q.focus();}}
function scrollEnd(){thread.scrollTop=thread.scrollHeight;}
// open / minimize: the widget is NOT modal (the page stays usable behind it); Esc minimizes.
function open(){lastFocus=document.activeElement;panel.hidden=false;fab.hidden=true;lsSet(OPEN_KEY,'1');fit();scrollEnd();setTimeout(focusQ,30);}
function minimize(){panel.hidden=true;fab.hidden=false;badge();lsSet(OPEN_KEY,'0');fit();if(lastFocus&&lastFocus.focus&&lastFocus!==document.body){try{lastFocus.focus({preventScroll:true});}catch(e){}}}
// The on-screen keyboard (iPad, phone) shrinks the VISUAL viewport and Safari scrolls the page to
// reveal the field, dragging a bottom-fixed panel with it. Keep the panel inside the visual
// viewport instead, and hold the page's scroll when the composer takes focus.
var vv=window.visualViewport, fitTimer=null, yKeep=null;
function fitNow(){
  if(!vv||panel.hidden){panel.style.bottom='';panel.style.height='';return;}
  var phone=window.matchMedia('(max-width:640px)').matches;
  var hidden=Math.max(0,Math.round(window.innerHeight-vv.height-vv.offsetTop));   // layout px covered below (keyboard)
  var b=(hidden<40&&!phone)?'':(hidden+(phone?0:18))+'px';
  var h=(hidden<40&&!phone)?'':Math.max(240,Math.round(phone?vv.height:Math.min(640,vv.height-36)))+'px';
  if(panel.style.bottom!==b)panel.style.bottom=b;                                   // write only on change: no reflow churn
  if(panel.style.height!==h)panel.style.height=h;
  // the page moved under the keyboard: put it back ONCE, after the keyboard has settled
  if(yKeep!==null&&Math.abs(window.scrollY-yKeep)>2)window.scrollTo(0,yKeep);}
// the keyboard animates for ~300 ms and fires resize on every frame; fit once it has settled
function fit(){clearTimeout(fitTimer);fitTimer=setTimeout(fitNow,FINE?0:180);}
if(vv){vv.addEventListener('resize',fit);vv.addEventListener('scroll',fit);}
q.addEventListener('focus',function(){yKeep=window.scrollY;if(FINE){var y=yKeep;requestAnimationFrame(function(){if(Math.abs(window.scrollY-y)>1)window.scrollTo(0,y);});}});
q.addEventListener('blur',function(){yKeep=null;});
fab.addEventListener('click',open);
document.getElementById('ask-close').addEventListener('click',minimize);
document.addEventListener('keydown',function(e){if(e.key==='Escape'&&!panel.hidden)minimize();});
Array.prototype.forEach.call(document.querySelectorAll('.ask-ex'),function(b){b.addEventListener('click',function(){q.value=b.textContent;submit();});});
// Enter sends, Shift+Enter is a newline (the chat convention)
q.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();submit();}});
var lastLen=0;
q.addEventListener('input',function(){var n=q.value.length;if(n<lastLen)q.style.height='auto';lastLen=n;if(q.scrollHeight>q.clientHeight+2)q.style.height=Math.min(q.scrollHeight,140)+'px';});
form.addEventListener('submit',function(e){e.preventDefault();submit();});
function add(html,cls){var d=document.createElement('div');d.className='ask-msg '+(cls||'');d.innerHTML=html;thread.appendChild(d);scrollEnd();return d;}
function stageHtml(k,extra,note){
  var list=STAGES.filter(function(s){return s.m.indexOf(mode)>=0;});
  var i=list.findIndex(function(s){return s.k===k;});
  var h=list.map(function(s,j){
    var st=j<i?'done':(j===i?'on':'');
    if(s.k==='rewrite'&&st!=='on'&&st!=='done')return '';
    var hint=(j===i&&s.h&&s.h[mode])?('<span class="ask-hint">'+esc(s.h[mode])+'</span>'):'';
    var line='<div class="ask-st '+st+'"><span class="ask-st-dot"></span><span>'+s.t+(j===i&&extra?(' · '+esc(extra)):'')+hint+'</span></div>';
    if(j===i&&acts.length)line+='<div class="ask-acts">'+acts.map(function(a){return '<div class="ask-act">'+esc(a)+'</div>';}).join('')+'</div>';
    return line;}).join('');
  return '<div class="ask-why">'+CFG.why+'</div>'+h+'<div class="ask-elapsed"><span id="ask-elapsed">0 s</span>'+(note?' · <span class="ask-note">'+esc(note)+'</span>':'')+'</div>';}
function tick(){var s=Math.round((Date.now()-t0)/1000);var el=document.getElementById('ask-elapsed');if(el)el.textContent=(s>=60?Math.floor(s/60)+' min '+(s%60)+' s':s+' s');}
function show(prog,k,extra,note){if(k){cur=k;var s=STAGES.filter(function(x){return x.k===k;})[0];if(s&&s.m.indexOf(mode)<0)mode=s.m[0];}prog.innerHTML=stageHtml(cur||'facts',extra||'',note||'');tick();scrollEnd();}
function wire(box){
  // the sections behind the summary line open in place
  Array.prototype.forEach.call(box.querySelectorAll('.ask-tgl'),function(b){b.addEventListener('click',function(){var t=box.querySelector('#'+b.getAttribute('data-for'));if(!t)return;t.hidden=!t.hidden;b.classList.toggle('open',!t.hidden);if(!t.hidden)thread.scrollTop=Math.max(0,t.offsetTop-thread.offsetTop-12);});});
  // [n] opens the sources and scrolls the THREAD to the line (never the page: scrollIntoView would move the document too)
  Array.prototype.forEach.call(box.querySelectorAll('.ask-cite'),function(a){a.addEventListener('click',function(e){e.preventDefault();var t=box.querySelector('#ask-src-'+a.getAttribute('data-n'));if(!t)return;var sec=t.closest('.ask-sec');if(sec&&sec.hidden){sec.hidden=false;var b=box.querySelector('.ask-tgl[data-for="'+sec.id+'"]');if(b)b.classList.add('open');}t.classList.add('lit');thread.scrollTop=Math.max(0,t.offsetTop-thread.offsetTop-thread.clientHeight/2+t.offsetHeight/2);setTimeout(function(){t.classList.remove('lit');},1600);});});
  var d=box.querySelector('.ask-deeper');if(d)d.addEventListener('click',function(){if(busy)return;d.disabled=true;ask(d.getAttribute('data-q')||'','d');});
  var l=box.querySelector('.ask-link');if(l)l.addEventListener('click',function(e){e.preventDefault();var u=location.origin+l.getAttribute('href');try{navigator.clipboard.writeText(u);l.textContent='Link copied';}catch(err){location.href=u;}});
}
function setBusy(b){busy=b;go.disabled=b;q.disabled=b;q.placeholder=b?'Scout is working…':(history.length?'Ask a follow-up':'Ask a question');}
function fail(prog,question,text){done(prog,'<p class="ask-p ask-none">'+esc(text)+'</p>',null,question);}
function begin(question,m){
  if(intro)intro.hidden=true;q.value='';q.style.height='auto';setBusy(true);acts=[];cur='facts';mode=m||'q';
  add('<div class="ask-bubble">'+(mode==='d'?'<span class="ask-deep-tag">Research deeper</span>':'')+esc(question)+'</div>','ask-user');
  var prog=add('','ask-scout ask-working');t0=Date.now();clearInterval(timer);timer=setInterval(tick,1000);show(prog,'facts');
  return prog;}
function submit(){
  if(busy)return;var question=q.value.trim();if(!question)return;
  ask(question,'q');}
// m: 'q' = quick (from what Scout already verified, ~30 s), 'd' = Research deeper (searches, minutes)
function ask(question,m){
  var prog=begin(question,m), rid=token();
  var payload={question:question,rid:rid,mode:(m==='d'?'deep':'quick'),slug:CFG.slug,competitor:CFG.competitor,my_company:CFG.my_company,persona:CFG.persona,
               history:history.slice(-6).map(function(h){return {question:h.question,answer_id:h.answer_id};})};
  fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
  .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
  .then(function(x){
    if(!x.ok||!x.j){fail(prog,question,(x.j&&x.j.message)||'Ask Scout is not available right now.');return;}
    if(x.j.mode==='engine'){
      if(x.j.id)savePending({question:question,answer_id:x.j.id,rid:rid,started:t0,mode:mode});
      stream(x.j.engine,x.j.token,payload,prog,question);return;}
    if(!x.j.id){fail(prog,question,x.j.message||'Ask Scout is not available right now.');return;}
    var plan=x.j.stages||[],i=0;
    function next(){
      if(i>=plan.length){fetch('/api/answers/'+x.j.id).then(function(r){return r.json();}).then(function(a){done(prog,a.html||'',x.j.id,question);}).catch(function(){fail(prog,question,'The answer could not be loaded.');});return;}
      show(prog,plan[i].k,plan[i].extra||'');var d=plan[i].ms||1200;i++;setTimeout(next,d);
    }
    next();
  })
  .catch(function(){fail(prog,question,'Ask Scout is not reachable right now.');});
}
function stream(engine,tok,payload,prog,question){
  var finished=false;
  function lost(){if(finished)return;finished=true;if(pending&&pending.question===question)recover(prog,question,'Connection dropped. Scout is still working on it; reconnecting');else fail(prog,question,'The connection dropped before the answer arrived.');}
  fetch(engine+'/ask',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},body:JSON.stringify(payload)})
  .then(function(r){
    if(!r.ok){finished=true;return r.json().then(function(j){fail(prog,question,(j&&j.message)||('Ask Scout returned '+r.status));}).catch(function(){fail(prog,question,'Ask Scout returned '+r.status+'.');});}
    var reader=r.body.getReader(),dec=new TextDecoder(),buf='';
    function handle(ev){
      if(ev.kind)mode=(ev.kind==='deep'?'d':'q');
      if(ev.activity){acts.push(ev.activity);if(acts.length>5)acts.shift();show(prog,null,'');}
      else if(ev.stage){acts=[];show(prog,ev.stage,ev.extra||'');}
      else if(ev.done){finished=true;done(prog,ev.html||'',ev.id,question);}
      else if(ev.error){finished=true;fail(prog,question,ev.error);}
    }
    function pump(){return reader.read().then(function(c){
      if(c.done){lost();return;}
      buf+=dec.decode(c.value,{stream:true});var parts=buf.split('\n\n');buf=parts.pop();
      parts.forEach(function(p){var line=p.split('\n').filter(function(l){return l.indexOf('data: ')===0;})[0];if(!line)return;var ev;try{ev=JSON.parse(line.slice(6));}catch(e){return;}handle(ev);});
      return pump();
    });}
    return pump();
  })
  .catch(function(){if(!finished)lost();});
}
// the stream is gone but the engine is not: the answer lands in the store under the id the
// viewer gave us, so poll for it (also how a reload mid-question picks the answer back up)
function recover(prog,question,note){
  var p=pending;if(!p){fail(prog,question,'The connection dropped before the answer arrived.');return;}
  show(prog,null,'',note);
  var deadline=(p.started||Date.now())+CFG.poll_max_ms;
  function poll(){
    if(Date.now()>deadline){savePending(null);fail(prog,question,'Scout did not finish that one. Ask it again.');return;}
    fetch('/api/answers/'+p.answer_id,{cache:'no-store'}).then(function(r){
      if(r.status===404)return null;
      if(!r.ok)return null;
      return r.json();
    }).then(function(a){
      if(!a){setTimeout(poll,CFG.poll_ms);return;}
      if(a.failed){fail(prog,question,a.error||'Scout could not answer that.');}
      else done(prog,a.html||'',a.id,question);
    }).catch(function(){setTimeout(poll,CFG.poll_ms);});
  }
  setTimeout(poll,CFG.poll_ms);
}
function done(box,html,id,question){
  clearInterval(timer);box.className='ask-msg ask-scout';box.innerHTML=html;wire(box);
  if(id){history.push({question:question,answer_id:id,html:html,mode:mode});save();clearBtn.hidden=false;}
  if(pending&&pending.question===question)savePending(null);
  badge();setBusy(false);scrollEnd();if(!panel.hidden)setTimeout(focusQ,20);
}
clearBtn.addEventListener('click',function(){history=[];save();savePending(null);Array.prototype.forEach.call(thread.querySelectorAll('.ask-msg'),function(m){m.remove();});if(intro)intro.hidden=false;clearBtn.hidden=true;badge();setBusy(false);focusQ();});
// restore the thread this browser already has (across cards and visits), whether it was open, and
// a question still in flight
history=load();
if(history.length){if(intro)intro.hidden=true;clearBtn.hidden=false;history.forEach(function(h){add('<div class="ask-bubble">'+(h.mode==='d'?'<span class="ask-deep-tag">Research deeper</span>':'')+esc(h.question)+'</div>','ask-user');var b=add(h.html||'','ask-scout');wire(b);});}
badge();setBusy(false);
if(lsGet(OPEN_KEY)==='1'){panel.hidden=false;fab.hidden=true;scrollEnd();}
pending=loadPending();
if(pending){var pr=begin(pending.question,pending.mode||'q');t0=pending.started||Date.now();cur=(mode==='d'?'search':'draft');recover(pr,pending.question,'Still working on this from before; reconnecting');}
})();"""


PANEL_CSS = """
/* the widget's own `display` values would beat the UA's [hidden]{display:none}; this keeps the
   hidden attribute (the script's only show/hide mechanism) authoritative */
.ask-fab[hidden],.ask-panel[hidden],.ask-fab-n[hidden],.ask-clear[hidden],.ask-intro[hidden]{display:none!important}
.ask-fab{position:fixed;right:18px;bottom:18px;z-index:60;display:inline-flex;align-items:center;gap:8px;padding:11px 16px;border:0;border-radius:999px;background:#2b2a26;color:#fff;font:600 14px/1 system-ui,-apple-system,sans-serif;box-shadow:0 6px 20px rgba(20,18,10,.22);cursor:pointer}
.ask-fab-dot{width:8px;height:8px;border-radius:50%;background:#7ed0a6;box-shadow:0 0 0 3px rgba(126,208,166,.28)}
.ask-fab-n{min-width:18px;height:18px;padding:0 5px;border-radius:9px;background:#7ed0a6;color:#12301f;font:700 11px/18px system-ui,sans-serif;text-align:center}
.ask-panel{position:fixed;right:18px;bottom:18px;width:min(440px,calc(100vw - 24px));height:min(640px,calc(100vh - 36px));z-index:80;background:#fbfaf6;border:1px solid #e3ded2;border-radius:14px;box-shadow:0 18px 48px rgba(20,18,10,.22);display:flex;flex-direction:column;overflow:hidden;font-family:system-ui,-apple-system,sans-serif;color:#2b2a26}
.ask-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:16px 18px 12px;border-bottom:1px solid #e3ded2}
.ask-head h3.ask-name{margin:0;font-size:19px;font-weight:700;letter-spacing:-.01em;display:flex;align-items:center;gap:8px}
.ask-beta{font-family:ui-monospace,Menlo,monospace;font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:#7a2e0e;background:#f9ece6;border:1px solid #ecc9bb;border-radius:4px;padding:2px 6px;font-weight:600}
.ask-sub{margin-top:3px;font-size:12.5px;color:#5f5e54}
.ask-why{font-size:12px;color:#5f5e54;margin:0 0 8px;padding-bottom:8px;border-bottom:1px dashed #e3ded2}
.ask-close{border:0;background:transparent;font-size:26px;line-height:1;color:#8a877c;cursor:pointer;padding:0 4px}
.ask-headbtns{display:flex;align-items:center;gap:8px}
.ask-clear{border:1px solid #dfdbcf;background:#fff;border-radius:999px;padding:4px 10px;font-size:11.5px;color:#5f5e54;cursor:pointer;white-space:nowrap}
.ask-about{display:inline-block;font-family:ui-monospace,Menlo,monospace;font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;color:#8a877c;margin-bottom:6px}
.ask-thread{flex:1;overflow:auto;padding:14px 18px 8px;display:flex;flex-direction:column;gap:12px}
.ask-review{font-size:12px;color:#7a2e0e;background:#f9ece6;border:1px solid #ecc9bb;border-radius:8px;padding:8px 10px}
.ask-intro p{margin:0 0 10px;font-size:13.5px;line-height:1.5;color:#5f5e54}
.ask-msg{max-width:100%}.ask-user{align-self:flex-end;max-width:88%}
.ask-bubble{display:inline-block;background:#2b2a26;color:#fff;border-radius:14px 14px 4px 14px;padding:9px 13px;font-size:14px;line-height:1.45;white-space:pre-wrap}
.ask-scout{background:#fff;border:1px solid #e3ded2;border-radius:14px 14px 14px 4px;padding:12px 14px}
.ask-working{color:#8a877c}
.ask-composer{display:flex;gap:8px;align-items:flex-end;padding:10px 12px 12px;border-top:1px solid #e3ded2;background:#fbfaf6}
/* 16px: below that iOS Safari zooms the whole page when the field takes focus */
.ask-composer textarea{flex:1;min-width:0;box-sizing:border-box;padding:10px 12px;font:16px/1.4 system-ui,sans-serif;border:1px solid #cfc8b8;border-radius:10px;background:#fff;resize:none;max-height:140px}
.ask-composer textarea:disabled{background:#f4f2ec;color:#8a877c}
.ask-exs{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 4px}
.ask-ex{border:1px solid #dfdbcf;background:#fff;border-radius:999px;padding:5px 10px;font-size:12px;color:#5f5e54;cursor:pointer;text-align:left}
.ask-go{border:0;border-radius:10px;background:#2b2a26;color:#fff;font:600 14px system-ui,sans-serif;padding:10px 16px;cursor:pointer;min-height:40px}.ask-go:disabled{background:#a9a69b;cursor:default}
.ask-elapsed{font-family:ui-monospace,Menlo,monospace;font-size:10.5px;color:#8a877c;padding:4px 0 0 20px}
.ask-note{color:#7a2e0e}
.ask-hint{display:block;font-weight:400;font-size:11.5px;color:#8a877c;margin-top:1px}
.ask-acts{margin:0 0 4px 20px;border-left:2px solid #e3ded2;padding-left:10px}
.ask-act{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:#5f5e54;padding:2px 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ask-act:last-child{color:#2b2a26}
.ask-st{display:flex;align-items:center;gap:10px;padding:7px 0;font-size:13.5px;color:#8a877c}
.ask-st-dot{width:10px;height:10px;border-radius:50%;border:2px solid #cfc8b8;box-sizing:border-box}
.ask-st.on{color:#2b2a26;font-weight:600}.ask-st.on .ask-st-dot{border-color:#2b2a26;animation:askpulse 1s infinite}
.ask-st.done{color:#5f5e54}.ask-st.done .ask-st-dot{background:#7ed0a6;border-color:#7ed0a6}
@keyframes askpulse{0%{box-shadow:0 0 0 0 rgba(43,42,38,.35)}100%{box-shadow:0 0 0 8px rgba(43,42,38,0)}}
.ask-answer .ask-q{font-size:12.5px;color:#8a877c;margin-bottom:10px;padding-bottom:10px;border-bottom:1px dashed #e3ded2}
.ask-p{font-size:14.5px;line-height:1.55;margin:0 0 12px}.ask-none{color:#8a877c}.ask-p b{font-weight:650;color:#151410}
.ask-cite{display:inline-block;min-width:16px;padding:0 4px;margin-left:2px;border-radius:4px;background:#eef3f6;color:#2a4658;font:600 10.5px/16px ui-monospace,Menlo,monospace;text-decoration:none;vertical-align:super}
.ask-sec{margin-top:14px}.ask-sec .ey{font-family:ui-monospace,Menlo,monospace;font-size:9px;letter-spacing:.16em;text-transform:uppercase;color:#8a877c}
.ask-srcs{margin:6px 0 0;padding-left:0;list-style:none}.ask-src{display:flex;align-items:center;flex-wrap:wrap;gap:8px;padding:7px 0;border-top:1px solid #efece4;font-size:13px;transition:background .3s}
.ask-src.lit{background:#f4f0e2}.ask-n{font:600 10.5px/18px ui-monospace,Menlo,monospace;min-width:18px;text-align:center;border-radius:4px;background:#eef3f6;color:#2a4658}
.ask-asof{font-family:ui-monospace,Menlo,monospace;font-size:10.5px;color:#8a877c}
.ask-exc{flex-basis:100%}.ask-exc summary{cursor:pointer;font-size:11.5px;color:#34566b}.ask-exc blockquote{margin:6px 0 0;padding:8px 10px;border-left:3px solid #dfdbcf;background:#fff;font-size:12.5px;color:#5f5e54}
.ask-unans ul,.ask-cut ul{margin:6px 0 0;padding-left:18px;font-size:13px;color:#5f5e54}.ask-cut summary{cursor:pointer;font-size:12px;color:#5f5e54}
.ask-foot{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:14px;padding-top:10px;border-top:1px solid #e3ded2;font-family:ui-monospace,Menlo,monospace;font-size:11px;color:#8a877c}
.ask-link{color:#2a4658}
.ask-more{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:8px}
.ask-tgl{border:0;background:transparent;padding:0;font-family:ui-monospace,Menlo,monospace;font-size:11px;color:#2a4658;cursor:pointer;text-decoration:underline;text-underline-offset:2px}
.ask-tgl.open{text-decoration:none;color:#2b2a26}
.ask-vmark{color:#1f4d2a;font-weight:600}
.ask-deeper{border:1px solid #34566b;background:#fff;color:#2a4658;border-radius:999px;padding:5px 11px;font:600 12px system-ui,sans-serif;cursor:pointer;white-space:nowrap}
.ask-deeper:disabled{opacity:.5;cursor:default}
.ask-deep-tag{display:block;font-family:ui-monospace,Menlo,monospace;font-size:9px;letter-spacing:.12em;text-transform:uppercase;opacity:.75;margin-bottom:3px}

.ask-answer .srcclass{font-family:ui-monospace,Menlo,monospace;font-size:9px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:#2a4658;background:#eef3f6;border:1px solid #cfdce5;border-radius:4px;padding:2px 6px;white-space:nowrap}
.ask-answer .srcclass-unknown{color:#8a877c;background:transparent;border-color:#dfdbcf}
.ask-answer .srcclass-filing,.ask-answer .srcclass-court,.ask-answer .srcclass-government{color:#1f4d2a;background:#e6f1e8;border-color:#bcd8c2}
.ask-answer .srcclass-review_site,.ask-answer .srcclass-forum{color:#6b5a1e;background:#f7f0dc;border-color:#e2d3a3}
.ask-answer .srcclass-scout_take{color:#4a3a7a;background:#eeeaf8;border-color:#cfc4ea}

.ask-permalink{max-width:720px;margin:8px auto 0;padding:18px 20px;background:#fbfaf6;border:1px solid #e3ded2;border-radius:10px;font-family:system-ui,-apple-system,sans-serif;color:#2b2a26}
.ask-back{max-width:720px;margin:12px auto;font-family:ui-monospace,Menlo,monospace;font-size:11px}
@media (max-width:640px){.ask-panel{right:0;bottom:0;width:100vw;height:100vh;height:100dvh;border-radius:0;border:0}.ask-fab{right:12px;bottom:12px}}
@media (prefers-reduced-motion:reduce){.ask-st.on .ask-st-dot{animation:none}}
/* hover only where a pointer can hover: on iOS a hover rule makes the first tap "hover" and the
   second tap act (Uroš, iPad, 2026-09-29) */
@media (hover: hover){.ask-fab:hover{background:#151410}.ask-close:hover{color:#2b2a26}.ask-clear:hover{border-color:#34566b;color:#2a4658}.ask-ex:hover{border-color:#34566b;color:#2a4658}.ask-go:hover{background:#151410}.ask-deeper:hover{background:#eef3f6}}
"""
