"""The sub-chat view shown inside an Orca chat: your brief branching into one card per point.

Shown at the top of Claude's reply on a brief and on every "3: …" message. Points
settled since the last view dissolve into ✓ chips as it opens.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .model import id_num

THREAD = 4

_STYLE = """
:root{--ck-1:#2563eb;--ck-2:#16a34a;--ck-3:#d97706;--ck-4:#9333ea;--ck-5:#db2777;--ck-ok:#16a34a;--ck-wait:#d97706}
@media (prefers-color-scheme: dark){:root{--ck-1:#60a5fa;--ck-2:#4ade80;--ck-3:#fbbf24;--ck-4:#c084fc;--ck-5:#f472b6;--ck-ok:#4ade80;--ck-wait:#fbbf24}}
html.dark{--ck-1:#60a5fa;--ck-2:#4ade80;--ck-3:#fbbf24;--ck-4:#c084fc;--ck-5:#f472b6;--ck-ok:#4ade80;--ck-wait:#fbbf24}
*{box-sizing:border-box}
body{margin:0;background:var(--background,#fff);color:var(--foreground,#171717);font:14px/1.45 var(--font-sans,system-ui,-apple-system,sans-serif)}
.wrap{max-width:820px;padding:4px 2px 6px}
.bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12.5px;color:var(--muted-foreground,#737373);margin:0 0 10px}
.bar b{color:var(--foreground,#171717);font-weight:600}
.bar .k{font-family:var(--font-mono,ui-monospace,Menlo,monospace);font-size:11.5px;padding:0 6px;border-radius:5px;background:var(--secondary,#f2f2f2);color:var(--secondary-foreground,#171717)}
.stage{display:grid;grid-template-columns:200px 56px minmax(0,1fr);align-items:start}
.brief{border:1px solid var(--border,#e5e5e5);background:var(--secondary,#f4f4f4);color:var(--secondary-foreground,#171717);border-radius:12px 12px 12px 4px;padding:9px 11px;font-size:12.5px;white-space:pre-wrap;overflow-wrap:anywhere}
.brief .who{font-size:10.5px;color:var(--muted-foreground,#737373);margin-bottom:2px}
svg.lines{width:56px;overflow:visible}
.subs{display:grid;gap:9px}
.sub{border:1px solid var(--border,#e5e5e5);border-left:3px solid var(--c);border-radius:var(--radius,10px);background:var(--card,#fafafa);color:var(--card-foreground,#171717);padding:8px 10px;max-height:600px;overflow:hidden;transition:opacity .7s ease,transform .7s ease,max-height .8s ease .3s,padding .8s ease .3s,margin .8s ease .3s,border-width .8s ease .3s}
.sub.gone{opacity:0;transform:translateX(-24px);max-height:0;padding-top:0;padding-bottom:0;border-width:0;margin-top:-9px}
.sub.focus{box-shadow:0 0 0 2px color-mix(in srgb,var(--c) 45%,transparent)}
.sub h3{margin:0 0 5px;font-size:13px;font-weight:600;display:flex;gap:7px;align-items:flex-start}
.n{flex:none;display:inline-grid;place-items:center;width:19px;height:19px;border-radius:50%;background:var(--c);color:#fff;font-size:10.5px;font-weight:700;margin-top:1px}
.st{margin-left:auto;font-size:11px;font-weight:500;color:var(--muted-foreground,#737373);white-space:nowrap}
.st.wait{color:var(--ck-wait)}.st.ok{color:var(--ck-ok)}.st.now{color:var(--c)}
.m{font-size:12.5px;margin:3px 0;padding:5px 8px;border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:150px;overflow:hidden}
.m.c{background:var(--background,#fff);border:1px solid var(--border,#e5e5e5)}
.m.u{background:color-mix(in srgb,var(--c) 15%,transparent);margin-left:24px}
.latest{font-size:12px;color:var(--muted-foreground,#737373)}
.ask{margin-top:4px;padding:5px 8px;border-radius:8px;font-size:12.5px;background:color-mix(in srgb,var(--ck-wait) 13%,transparent)}
.ask b{color:var(--ck-wait);font-weight:600}
.box{margin-top:6px;border:1px dashed var(--border,#d4d4d4);border-radius:8px;padding:4px 8px;font-size:12px;color:var(--muted-foreground,#8a8a8a);font-family:var(--font-mono,ui-monospace,Menlo,monospace)}
.done{margin-top:10px;display:flex;flex-wrap:wrap;gap:5px;min-height:4px}
.chip{font-size:11.5px;padding:1px 9px;border-radius:999px;background:color-mix(in srgb,var(--ck-ok) 14%,transparent);color:var(--ck-ok);max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip.new{animation:pop .5s ease-out}
@keyframes pop{from{opacity:0;transform:scale(.85)}to{opacity:1;transform:none}}
@media (max-width:620px){.stage{grid-template-columns:minmax(0,1fr)}svg.lines{display:none}.brief{margin-bottom:8px}}
"""

_SCRIPT = """
(function(){
  var D = JSON.parse(document.getElementById('d').textContent);
  function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
  function col(n){return 'var(--ck-'+(((n-1)%5)+1)+')'}
  document.getElementById('brief').innerHTML='<div class="who">You</div>'+esc(D.brief||'Your brief');
  var bar=document.getElementById('bar');
  if(D.talking){bar.innerHTML='Talking to <b>'+D.talking.num+' · '+esc(D.talking.title)+'</b>. Plain messages go here. <span class="k">0:</span> everyone · <span class="k">N:</span> switch';}
  else if(D.cards.some(function(c){return !c.settling})){bar.innerHTML='Type <span class="k">2: …</span> to talk to one point. It stays with that point until you switch or it settles.';}
  else{bar.innerHTML='<b>All points settled.</b>';}
  var subs=document.getElementById('subs'), done=document.getElementById('done');
  D.cards.forEach(function(c){
    var el=document.createElement('div'); el.className='sub'+(c.focus?' focus':''); el.id='s'+c.node; el.style.setProperty('--c',col(c.num));
    var st=c.state, cls=c.focus?'now':c.waiting?'wait':c.settling?'ok':'';
    var h='<h3><span class="n">'+c.num+'</span><span>'+esc(c.title)+'</span><span class="st '+cls+'">'+esc(st)+'</span></h3>';
    if(!c.thread.length && c.latest) h+='<div class="latest">Latest: '+esc(c.latest)+'</div>';
    c.thread.forEach(function(m){h+='<div class="m '+(m.who==='you'?'u':'c')+'">'+(m.who==='you'?'You: ':'Claude: ')+esc(m.text)+'</div>'});
    if(c.waiting) h+='<div class="ask"><b>Claude asks:</b> '+esc(c.waiting)+'</div>';
    el.innerHTML=h; subs.appendChild(el);
  });
  D.done.forEach(function(c){var s=document.createElement('span');s.className='chip';s.textContent='✓ '+c.num+' '+c.title;done.appendChild(s)});
  function lines(){
    var svg=document.getElementById('lines'); if(getComputedStyle(svg).display==='none')return;
    var stage=svg.parentNode, top=stage.getBoundingClientRect().top, b=document.getElementById('brief').getBoundingClientRect();
    var y0=b.top-top+Math.min(b.height/2,40), html='', h=stage.getBoundingClientRect().height;
    svg.setAttribute('viewBox','0 0 56 '+h); svg.style.height=h+'px';
    Array.prototype.forEach.call(subs.children,function(el){
      if(el.classList.contains('gone'))return;
      var r=el.getBoundingClientRect(); var y=r.top-top+Math.min(r.height/2,18);
      html+='<path d="M0 '+y0+' C28 '+y0+' 28 '+y+' 56 '+y+'" fill="none" stroke="'+getComputedStyle(el).getPropertyValue('--c')+'" stroke-width="2.5"/>';
    });
    svg.innerHTML=html;
  }
  lines(); window.addEventListener('resize',lines);
  // Points settled since the last view dissolve into chips.
  setTimeout(function(){
    D.cards.forEach(function(c){
      if(!c.settling)return;
      var el=document.getElementById('s'+c.node); el.classList.add('gone');
      var n=0;(function f(){lines(); if(n++<22) setTimeout(f,50)})();
      setTimeout(function(){var s=document.createElement('span');s.className='chip new';s.textContent='✓ '+c.num+' '+c.title;done.appendChild(s)},900);
      setTimeout(lines,1300);
    });
  },1400);
})();
"""


def build(state: dict, session: str, brief: str | None, nodes: list[str], focus: str | None = None, fresh: bool = False) -> tuple[dict, list[str]]:
    """The view's data, and the points it leaves open (to dissolve them next time)."""
    st = state
    last_open = (st.get("view_open") or {}).get(session)
    cards, done, still_open = [], [], []
    for nid in nodes:
        n = st["nodes"][nid]
        sc = (st.get("subchats") or {}).get(nid, {"thread": []})
        live = [r for r in n["records"] if st["records"][r]["active"]]
        q = next((st["questions"][k]["text"] for k, qq in st["questions"].items() if qq["status"] == "open" and qq.get("node") == nid), None)
        is_open = n["status"] in ("open", "tabled") or nid == focus
        settling = not is_open and last_open is not None and nid in last_open
        item = {
            "node": nid,
            "num": id_num(nid),
            "title": n["title"],
            "thread": sc.get("thread", [])[-THREAD:],
            "latest": st["records"][live[-1]]["decision"] if live else None,
            "waiting": q if nid != focus else None,
            "focus": nid == focus,
            "settling": settling,
        }
        if nid == focus:
            item["state"] = "replying below ↓"
        elif fresh and is_open:
            item["state"] = "Claude is on it ↓"
        elif q:
            item["state"] = "needs you"
        elif settling:
            item["state"] = "settled ✓"
        else:
            item["state"] = "open" if is_open else ""
        if is_open:
            cards.append(item)
            still_open.append(nid)
        elif settling:
            cards.append(item)
        else:
            done.append(item)
    talking = None
    if focus and focus in st["nodes"]:
        talking = {"num": id_num(focus), "title": st["nodes"][focus]["title"]}
    return {"brief": brief, "cards": cards, "done": done, "talking": talking}, still_open


def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Creek sub-chats</title>'
        f"<style>{_STYLE}</style></head><body><div class=\"wrap\"><div class=\"bar\" id=\"bar\"></div><div class=\"stage\">"
        '<div class="brief" id="brief"></div><svg class="lines" id="lines"></svg>'
        '<div><div class="subs" id="subs"></div><div class="done" id="done"></div></div></div></div>'
        f'<script type="application/json" id="d">{payload}</script><script>{_SCRIPT}</script></body></html>'
    )


def write(data: dict) -> str | None:
    """Write the view into this Orca chat's folder. Returns the line that shows it, or None outside Orca."""
    d = os.environ.get("ORCA_CHAT_SCRATCH_DIR")
    if not d or not os.path.isdir(d) or not os.access(d, os.W_OK):
        return None
    name = f"creek-subchats-{os.urandom(3).hex()}.html"
    (Path(d) / name).write_text(render(data), encoding="utf-8")
    return f'::orca-visual{{file="{name}" title="Sub-chats"}}'
