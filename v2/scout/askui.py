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

STAGES = [
    ("facts", "Reading the card's verified facts"),
    ("search", "Searching and reading sources"),
    ("ground", "Checking every quote against its page"),
    ("floor", "Checking every number against the evidence"),
    ("verify", "Verifying each sentence"),
    ("done", "Done"),
]
EXAMPLES = {
    "default": ["What changed in their pricing this quarter?",
                "What is their latest reported quarterly revenue?",
                "What are they hiring for right now?"],
}
_TIER_TITLE = {"primary": "Primary source: the document itself, or the company's own statement",
               "reputable_secondary": "Reputable secondary source",
               "sentiment_only": "Sentiment: reviews or discussion, not a fact source"}


def _chip(cls, tier=None) -> str:
    label = classify.CLASS_LABEL.get(cls or "unknown", "Web")
    title = _TIER_TITLE.get(tier or classify.CLASS_TIER.get(cls or "") or "", "")
    return (f'<span class="srcclass srcclass-{_html.escape(str(cls or "unknown"))}"'
            + (f' title="{_html.escape(title)}"' if title else "") + f'>{_html.escape(label)}</span>')


def _cites(text: str, cites: list) -> str:
    body = _html.escape(text)
    marks = "".join(f'<a class="ask-cite" href="#ask-src-{n}" data-n="{n}">{n}</a>' for n in cites)
    return f"{body} {marks}" if marks else body


def answer_html(a: dict, permalink: bool = True, show_question: bool = True) -> str:
    """Server-rendered answer body (one renderer for the panel and the permalink page). In the
    thread the reader's own bubble carries the question, so the echo is off there."""
    paras = "".join(f'<p class="ask-p">{_cites(p.get("text", ""), p.get("cites", []))}</p>' for p in a.get("paragraphs") or [])
    if not paras:
        paras = '<p class="ask-p ask-none">Scout could not verify an answer to this question. What it tried is in the Cut Log.</p>'
    srcs = ""
    for s in a.get("sources") or []:
        dom = re.sub(r"^www\\.", "", (re.sub(r"^https?://", "", s.get("url") or "").split("/")[0]))
        exc = _html.escape(str(s.get("excerpt") or ""))
        srcs += (f'<li id="ask-src-{s["n"]}" class="ask-src"><span class="ask-n">{s["n"]}</span>'
                 f'<a href="{_html.escape(s.get("url") or "#")}" target="_blank" rel="noopener">{_html.escape(dom)}</a>'
                 f'{_chip(s.get("class"), s.get("tier"))}'
                 + (f'<span class="ask-asof">{_html.escape(str(s.get("as_of")))}</span>' if s.get("as_of") else "")
                 + (f'<details class="ask-exc"><summary>quote</summary><blockquote>{exc}</blockquote></details>' if exc else "")
                 + "</li>")
    unans = "".join(f"<li>{_html.escape(u)}</li>" for u in a.get("unanswered") or [])
    cuts = "".join(f'<li><b>{_html.escape(str(c.get("label") or ""))}</b> {_html.escape(str(c.get("reason") or ""))}</li>' for c in a.get("cut_log") or [])
    t = a.get("trajectory") or {}
    n_cut = len(a.get("cut_log") or [])
    foot = (f'verified in {a.get("seconds", 0):g} s · {len(a.get("sources") or [])} source{"s" if len(a.get("sources") or []) != 1 else ""}'
            f' · {n_cut} cut' + (f' · {t.get("rewritten", 0)} rewritten' if t.get("rewritten") else ""))
    link = f'<a class="ask-link" href="/answers/{_html.escape(a["id"])}">Copy link</a>' if permalink and a.get("id") else ""
    q_echo = f'<div class="ask-q">{_html.escape(a.get("question") or "")}</div>' if show_question else ""
    about = f'<div class="ask-about">About {_html.escape(str(a.get("competitor")))}</div>' if a.get("competitor") else ""
    return (f'<div class="ask-answer" data-id="{_html.escape(str(a.get("id") or ""))}">'
            f'{q_echo}{about}{paras}'
            + (f'<div class="ask-sec"><span class="ey">Sources</span><ol class="ask-srcs">{srcs}</ol></div>' if srcs else "")
            + (f'<div class="ask-sec ask-unans"><span class="ey">Could not verify</span><ul>{unans}</ul></div>' if unans else "")
            + (f'<details class="ask-sec ask-cut"><summary><span class="ey">Cut log</span> {n_cut}</summary><ul>{cuts}</ul></details>' if cuts else "")
            + f'<div class="ask-foot"><span>{foot}</span>{link}</div></div>')


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
    # the reader is on is only a hint, shown as "currently on".
    about = "Any tracked competitor" + (f" · currently on {comp}" if comp else "")
    plabel = {"eng_led": "an eng-led champion", "technical_evaluator": "a technical evaluator", "economic_buyer": "an economic buyer",
              "security_regulated": "a security & regulated buyer", "exec_top_down": "an exec / top-down buyer"}.get(persona or "", "")
    ctx = about + (f" · for {plabel}" if plabel else "")
    examples = [e.replace("their", f"{comp}'s") if comp else e for e in EXAMPLES["default"]]
    ex_html = "".join(f'<button type="button" class="ask-ex">{_html.escape(e)}</button>' for e in examples)
    stages_js = json.dumps([{"k": k, "t": t} for k, t in STAGES])
    canned = bool(config.ASK_CANNED_ID)
    cfg = json.dumps({"slug": slug, "competitor": comp, "my_company": meta.get("my_company") or "", "persona": persona or "",
                      "canned": canned})
    review = ('<div class="ask-review">Review build: every question replays one stored answer, so the reply will not match '
              'what you asked. The thread, the stages and the answer layout are what is under review.</div>' if canned else "")
    return f'''
<button type="button" class="ask-fab" id="ask-fab" aria-haspopup="dialog" aria-controls="ask-panel">
  <span class="ask-fab-dot"></span>Ask Scout</button>
<div class="ask-backdrop" id="ask-backdrop" hidden></div>
<aside class="ask-panel" id="ask-panel" role="dialog" aria-modal="true" aria-labelledby="ask-title" hidden>
  <div class="ask-head"><div><div class="ey">Ask Scout</div><h3 id="ask-title">{_html.escape(ctx)}</h3></div>
    <div class="ask-headbtns"><button type="button" class="ask-clear" id="ask-clear" title="Start a new thread" hidden>New thread</button>
    <button type="button" class="ask-close" id="ask-close" aria-label="Close">×</button></div></div>
  <div class="ask-thread" id="ask-thread" aria-live="polite">
    {review}
    <div class="ask-intro" id="ask-intro">
      <p>Ask about any competitor Scout tracks{(" (you are on " + _html.escape(comp) + ")") if comp else ""}. Every sentence in the answer cites a verified source, or it is not in the answer. The thread stays with you across cards and visits.</p>
      <div class="ask-exs">{ex_html}</div>
    </div>
  </div>
  <form class="ask-composer" id="ask-form">
    <textarea id="ask-q" rows="1" maxlength="400" placeholder="Ask a question" aria-label="Your question"></textarea>
    <button type="submit" class="ask-go" id="ask-go" aria-label="Ask">Ask</button>
  </form>
</aside>
<script>(function(){{
var CFG={cfg}, STAGES={stages_js};
var fab=document.getElementById('ask-fab'), panel=document.getElementById('ask-panel'), bd=document.getElementById('ask-backdrop');
var form=document.getElementById('ask-form'), q=document.getElementById('ask-q'), go=document.getElementById('ask-go');
var thread=document.getElementById('ask-thread'), intro=document.getElementById('ask-intro'), clearBtn=document.getElementById('ask-clear');
var history=[], busy=false, lastFocus=null, timer=null, t0=0, KEY='scout_ask_thread_v1', MAX=20;
function load(){{try{{var raw=localStorage.getItem(KEY);var arr=raw?JSON.parse(raw):[];if(!Array.isArray(arr))return [];return arr.slice(-MAX);}}catch(e){{return [];}}}}
function save(){{try{{localStorage.setItem(KEY,JSON.stringify(history.slice(-MAX)));}}catch(e){{}}}}
function esc(s){{return String(s).replace(/[&<>"]/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c];}});}}
function open(){{lastFocus=document.activeElement;panel.hidden=false;bd.hidden=false;document.body.classList.add('ask-open');setTimeout(function(){{q.focus();}},30);}}
function close(){{panel.hidden=true;bd.hidden=true;document.body.classList.remove('ask-open');if(lastFocus&&lastFocus.focus)lastFocus.focus();}}
fab.addEventListener('click',open);document.getElementById('ask-close').addEventListener('click',close);bd.addEventListener('click',close);
document.addEventListener('keydown',function(e){{if(e.key==='Escape'&&!panel.hidden)close();}});
panel.addEventListener('keydown',function(e){{if(e.key!=='Tab')return;var f=panel.querySelectorAll('button:not([disabled]),textarea,a[href],summary,[tabindex]:not([tabindex="-1"])');if(!f.length)return;var a=f[0],z=f[f.length-1];if(e.shiftKey&&document.activeElement===a){{z.focus();e.preventDefault();}}else if(!e.shiftKey&&document.activeElement===z){{a.focus();e.preventDefault();}}}});
Array.prototype.forEach.call(document.querySelectorAll('.ask-ex'),function(b){{b.addEventListener('click',function(){{q.value=b.textContent;submit();}});}});
q.addEventListener('keydown',function(e){{if(e.key==='Enter'&&!e.shiftKey){{e.preventDefault();submit();}}}});
q.addEventListener('input',function(){{q.style.height='auto';q.style.height=Math.min(q.scrollHeight,140)+'px';}});
function scrollEnd(){{thread.scrollTop=thread.scrollHeight;}}
function add(html,cls){{var d=document.createElement('div');d.className='ask-msg '+(cls||'');d.innerHTML=html;thread.appendChild(d);scrollEnd();return d;}}
function stageHtml(k,extra){{var i=STAGES.findIndex(function(s){{return s.k===k;}});return STAGES.map(function(s,j){{var st=j<i?'done':(j===i?'on':'');return '<div class="ask-st '+st+'"><span class="ask-st-dot"></span><span>'+s.t+(j===i&&extra?(' · '+extra):'')+'</span></div>';}}).join('')+'<div class="ask-elapsed" id="ask-elapsed">0 s</div>';}}
function tick(){{var s=Math.round((Date.now()-t0)/1000);var el=document.getElementById('ask-elapsed');if(el)el.textContent=s+' s';}}
function wire(box){{Array.prototype.forEach.call(box.querySelectorAll('.ask-cite'),function(a){{a.addEventListener('click',function(e){{e.preventDefault();var t=box.querySelector('#ask-src-'+a.getAttribute('data-n'));if(t){{t.classList.add('lit');t.scrollIntoView({{block:'center',behavior:'smooth'}});setTimeout(function(){{t.classList.remove('lit');}},1600);}}}});}});var l=box.querySelector('.ask-link');if(l)l.addEventListener('click',function(e){{e.preventDefault();var u=location.origin+l.getAttribute('href');try{{navigator.clipboard.writeText(u);l.textContent='Link copied';}}catch(err){{location.href=u;}}}});}}
function setBusy(b){{busy=b;go.disabled=b;q.disabled=b;q.placeholder=b?'Scout is working…':(history.length?'Ask a follow-up':'Ask a question');}}
function submit(){{if(busy)return;var question=q.value.trim();if(!question)return;
  if(intro)intro.hidden=true;q.value='';q.style.height='auto';setBusy(true);
  add('<div class="ask-bubble">'+esc(question)+'</div>','ask-user');
  var prog=add(stageHtml('facts'),'ask-scout ask-working');t0=Date.now();timer=setInterval(tick,1000);
  var payload={{question:question,slug:CFG.slug,competitor:CFG.competitor,my_company:CFG.my_company,persona:CFG.persona,history:history.slice(-6).map(function(h){{return {{question:h.question,answer_id:h.answer_id}};}})}};
  fetch('/api/ask',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}})
  .then(function(r){{return r.json().then(function(j){{return {{ok:r.ok,j:j}};}});}})
  .then(function(x){{if(!x.ok||!x.j||!x.j.id){{done(prog,'<p class="ask-p ask-none">'+esc((x.j&&x.j.message)||'Ask Scout is not available right now.')+'</p>',null,question);return;}}
    var plan=x.j.stages||[],i=0;
    function next(){{if(i>=plan.length){{fetch('/api/answers/'+x.j.id).then(function(r){{return r.json();}}).then(function(a){{done(prog,a.html||'',x.j.id,question);}}).catch(function(){{done(prog,'<p class="ask-p ask-none">The answer could not be loaded.</p>',null,question);}});return;}}
      prog.innerHTML=stageHtml(plan[i].k,plan[i].extra||'');scrollEnd();var d=plan[i].ms||1200;i++;setTimeout(next,d);}}
    next();}})
  .catch(function(){{done(prog,'<p class="ask-p ask-none">Ask Scout is not reachable right now.</p>',null,question);}});
}}
function done(box,html,id,question){{clearInterval(timer);box.className='ask-msg ask-scout';box.innerHTML=html;wire(box);if(id){{history.push({{question:question,answer_id:id,html:html}});save();clearBtn.hidden=false;}}setBusy(false);scrollEnd();setTimeout(function(){{q.focus();}},20);}}
form.addEventListener('submit',function(e){{e.preventDefault();submit();}});
clearBtn.addEventListener('click',function(){{history=[];save();Array.prototype.forEach.call(thread.querySelectorAll('.ask-msg'),function(m){{m.remove();}});if(intro)intro.hidden=false;clearBtn.hidden=true;setBusy(false);q.focus();}});
// restore the thread this browser already has (across cards and visits)
history=load();
if(history.length){{if(intro)intro.hidden=true;clearBtn.hidden=false;history.forEach(function(h){{add('<div class="ask-bubble">'+esc(h.question)+'</div>','ask-user');var b=add(h.html||'','ask-scout');wire(b);}});}}
setBusy(false);
}})();</script>'''


PANEL_CSS = """
.ask-fab{position:fixed;right:18px;bottom:18px;z-index:60;display:inline-flex;align-items:center;gap:8px;padding:11px 16px;border:0;border-radius:999px;background:#2b2a26;color:#fff;font:600 14px/1 system-ui,-apple-system,sans-serif;box-shadow:0 6px 20px rgba(20,18,10,.22);cursor:pointer}
.ask-fab:hover{background:#151410}.ask-fab-dot{width:8px;height:8px;border-radius:50%;background:#7ed0a6;box-shadow:0 0 0 3px rgba(126,208,166,.28)}
.ask-backdrop{position:fixed;inset:0;background:rgba(20,18,10,.28);z-index:70}
.ask-panel{position:fixed;top:0;right:0;bottom:0;width:min(520px,100vw);z-index:80;background:#fbfaf6;border-left:1px solid #e3ded2;box-shadow:-12px 0 32px rgba(20,18,10,.16);display:flex;flex-direction:column;font-family:system-ui,-apple-system,sans-serif;color:#2b2a26}
.ask-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:16px 18px 12px;border-bottom:1px solid #e3ded2}
.ask-head h3{margin:2px 0 0;font-size:15px;font-weight:600}.ask-head .ey{font-family:ui-monospace,Menlo,monospace;font-size:9px;letter-spacing:.16em;text-transform:uppercase;color:#8a877c}
.ask-close{border:0;background:transparent;font-size:26px;line-height:1;color:#8a877c;cursor:pointer;padding:0 4px}.ask-close:hover{color:#2b2a26}
.ask-headbtns{display:flex;align-items:center;gap:8px}
.ask-clear{border:1px solid #dfdbcf;background:#fff;border-radius:999px;padding:4px 10px;font-size:11.5px;color:#5f5e54;cursor:pointer;white-space:nowrap}.ask-clear:hover{border-color:#34566b;color:#2a4658}
.ask-about{display:inline-block;font-family:ui-monospace,Menlo,monospace;font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;color:#8a877c;margin-bottom:6px}
.ask-thread{flex:1;overflow:auto;padding:14px 18px 8px;display:flex;flex-direction:column;gap:12px}
.ask-review{font-size:12px;color:#7a2e0e;background:#f9ece6;border:1px solid #ecc9bb;border-radius:8px;padding:8px 10px}
.ask-intro p{margin:0 0 10px;font-size:13.5px;line-height:1.5;color:#5f5e54}
.ask-msg{max-width:100%}.ask-user{align-self:flex-end;max-width:88%}
.ask-bubble{display:inline-block;background:#2b2a26;color:#fff;border-radius:14px 14px 4px 14px;padding:9px 13px;font-size:14px;line-height:1.45;white-space:pre-wrap}
.ask-scout{background:#fff;border:1px solid #e3ded2;border-radius:14px 14px 14px 4px;padding:12px 14px}
.ask-working{color:#8a877c}
.ask-composer{display:flex;gap:8px;align-items:flex-end;padding:10px 12px 12px;border-top:1px solid #e3ded2;background:#fbfaf6}
.ask-composer textarea{flex:1;box-sizing:border-box;padding:10px 12px;font:14px/1.4 system-ui,sans-serif;border:1px solid #cfc8b8;border-radius:10px;background:#fff;resize:none;max-height:140px}
.ask-composer textarea:disabled{background:#f4f2ec;color:#8a877c}
.ask-exs{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 4px}
.ask-ex{border:1px solid #dfdbcf;background:#fff;border-radius:999px;padding:5px 10px;font-size:12px;color:#5f5e54;cursor:pointer;text-align:left}.ask-ex:hover{border-color:#34566b;color:#2a4658}
.ask-go{border:0;border-radius:10px;background:#2b2a26;color:#fff;font:600 14px system-ui,sans-serif;padding:10px 16px;cursor:pointer;min-height:40px}.ask-go:hover{background:#151410}.ask-go:disabled{background:#a9a69b;cursor:default}
.ask-elapsed{font-family:ui-monospace,Menlo,monospace;font-size:10.5px;color:#8a877c;padding:4px 0 0 20px}
.ask-st{display:flex;align-items:center;gap:10px;padding:7px 0;font-size:13.5px;color:#8a877c}
.ask-st-dot{width:10px;height:10px;border-radius:50%;border:2px solid #cfc8b8;box-sizing:border-box}
.ask-st.on{color:#2b2a26;font-weight:600}.ask-st.on .ask-st-dot{border-color:#2b2a26;animation:askpulse 1s infinite}
.ask-st.done{color:#5f5e54}.ask-st.done .ask-st-dot{background:#7ed0a6;border-color:#7ed0a6}
@keyframes askpulse{0%{box-shadow:0 0 0 0 rgba(43,42,38,.35)}100%{box-shadow:0 0 0 8px rgba(43,42,38,0)}}
.ask-answer .ask-q{font-size:12.5px;color:#8a877c;margin-bottom:10px;padding-bottom:10px;border-bottom:1px dashed #e3ded2}
.ask-p{font-size:14.5px;line-height:1.55;margin:0 0 12px}.ask-none{color:#8a877c}
.ask-cite{display:inline-block;min-width:16px;padding:0 4px;margin-left:2px;border-radius:4px;background:#eef3f6;color:#2a4658;font:600 10.5px/16px ui-monospace,Menlo,monospace;text-decoration:none;vertical-align:super}
.ask-sec{margin-top:14px}.ask-sec .ey{font-family:ui-monospace,Menlo,monospace;font-size:9px;letter-spacing:.16em;text-transform:uppercase;color:#8a877c}
.ask-srcs{margin:6px 0 0;padding-left:0;list-style:none}.ask-src{display:flex;align-items:center;flex-wrap:wrap;gap:8px;padding:7px 0;border-top:1px solid #efece4;font-size:13px;transition:background .3s}
.ask-src.lit{background:#f4f0e2}.ask-n{font:600 10.5px/18px ui-monospace,Menlo,monospace;min-width:18px;text-align:center;border-radius:4px;background:#eef3f6;color:#2a4658}
.ask-asof{font-family:ui-monospace,Menlo,monospace;font-size:10.5px;color:#8a877c}
.ask-exc{flex-basis:100%}.ask-exc summary{cursor:pointer;font-size:11.5px;color:#34566b}.ask-exc blockquote{margin:6px 0 0;padding:8px 10px;border-left:3px solid #dfdbcf;background:#fff;font-size:12.5px;color:#5f5e54}
.ask-unans ul,.ask-cut ul{margin:6px 0 0;padding-left:18px;font-size:13px;color:#5f5e54}.ask-cut summary{cursor:pointer;font-size:12px;color:#5f5e54}
.ask-foot{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:14px;padding-top:10px;border-top:1px solid #e3ded2;font-family:ui-monospace,Menlo,monospace;font-size:11px;color:#8a877c}
.ask-link{color:#2a4658}
.ask-answer .srcclass{font-family:ui-monospace,Menlo,monospace;font-size:9px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:#2a4658;background:#eef3f6;border:1px solid #cfdce5;border-radius:4px;padding:2px 6px;white-space:nowrap}
.ask-answer .srcclass-unknown{color:#8a877c;background:transparent;border-color:#dfdbcf}
.ask-answer .srcclass-filing,.ask-answer .srcclass-court,.ask-answer .srcclass-government{color:#1f4d2a;background:#e6f1e8;border-color:#bcd8c2}
.ask-answer .srcclass-review_site,.ask-answer .srcclass-forum{color:#6b5a1e;background:#f7f0dc;border-color:#e2d3a3}
body.ask-open{overflow:hidden}
.ask-permalink{max-width:720px;margin:8px auto 0;padding:18px 20px;background:#fbfaf6;border:1px solid #e3ded2;border-radius:10px;font-family:system-ui,-apple-system,sans-serif;color:#2b2a26}
.ask-back{max-width:720px;margin:12px auto;font-family:ui-monospace,Menlo,monospace;font-size:11px}
@media (max-width:640px){.ask-panel{width:100vw}.ask-fab{right:12px;bottom:12px}}
@media (prefers-reduced-motion:reduce){.ask-st.on .ask-st-dot{animation:none}}
"""
