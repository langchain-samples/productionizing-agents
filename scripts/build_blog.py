#!/usr/bin/env python3
"""Render docs/writing-agent-evals.md to a self-contained styled HTML page.

    python scripts/build_blog.py

Requires `markdown` and `pygments` (both in requirements.txt). Code fences tagged
with a language get syntax-highlighted inline, so the output has no CDN or JS
dependency: one file you can email, host, or open from disk.
"""

from __future__ import annotations

import html as _html
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "writing-agent-evals.md"
OUT = ROOT / "docs" / "writing-agent-evals.html"

TITLE = "Your Agent Needs a Test Suite"

# --- page styles -----------------------------------------------------------
# Palette matches the deck: LangChain dark, #7FC8FF primary.
BASE_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
:root{--bg:#0b0e14;--panel:#121722;--line:#243044;--tx:#e8edf5;--dim:#93a2b8;
      --acc:#7FC8FF;--good:#E3FF8F;--bad:#FBB0A5;--toc-w:288px;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
     font:17px/1.72 Inter,-apple-system,sans-serif;
     -webkit-font-smoothing:antialiased}
.wrap{max-width:760px;margin:0 auto;padding:76px 28px 140px}
h1{font-size:44px;line-height:1.1;letter-spacing:-.025em;margin:0 0 10px;font-weight:700}
h1+h3{color:var(--acc);font-size:21px;font-weight:600;margin:0 0 52px;
      padding-bottom:32px;border-bottom:1px solid var(--line)}
h2{font-size:29px;letter-spacing:-.018em;margin:68px 0 20px;font-weight:700;
   padding-top:26px;border-top:1px solid var(--line)}
h3{font-size:21px;margin:44px 0 14px;font-weight:600;color:var(--acc)}
h4{font-size:17px;margin:30px 0 10px;color:var(--dim);font-weight:600}
p,li{color:var(--tx)}
a{color:var(--acc)}
strong{font-weight:600}
blockquote{margin:30px 0;padding:20px 26px;background:var(--panel);
           border-left:3px solid var(--acc);border-radius:8px}
blockquote p{margin:0 0 12px}blockquote p:last-child{margin:0}
code{font:.87em JetBrains Mono,monospace;background:#1b2331;color:var(--acc);
     padding:2px 6px;border-radius:4px}
pre{background:var(--panel);border:1px solid var(--line);border-radius:9px;
    padding:18px 22px;overflow-x:auto;margin:24px 0}
pre code{background:none;color:var(--tx);padding:0;font-size:14px;line-height:1.62}
table{width:100%;border-collapse:collapse;margin:26px 0;font-size:15.5px}
th{text-align:left;font:600 12px JetBrains Mono,monospace;letter-spacing:.1em;
   text-transform:uppercase;color:var(--dim);padding:0 14px 10px 0;
   border-bottom:1px solid var(--line)}
td{padding:11px 14px 11px 0;border-bottom:1px solid rgba(36,48,68,.6);
   vertical-align:top}
tr:last-child td{border-bottom:none}
hr{border:0;border-top:1px solid var(--line);margin:56px 0}
ul,ol{padding-left:24px}li{margin:7px 0}
h2,h3{scroll-margin-top:24px}
/* Author note above the opening anecdote. Deliberately quieter than body text so it
   reads as a preface rather than competing with the cold open. */
.bio{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--acc);
     border-radius:9px;padding:20px 24px;margin:0 0 44px}
.bio p{margin:0 0 12px;color:var(--dim);font-size:15.5px;line-height:1.68}
.bio p:last-child{margin:0}
.bio strong{color:var(--tx);font-weight:600}
/* --- chapter dividers --- */
h1.chapter{display:block;font-size:34px;line-height:1.15;letter-spacing:-.02em;
           font-weight:700;margin:76px 0 10px;padding-top:34px;
           border-top:2px solid var(--acc);scroll-margin-top:24px}
h1.chapter .cn{display:block;font:600 11px JetBrains Mono,monospace;letter-spacing:.18em;
               text-transform:uppercase;color:var(--acc);margin-bottom:12px}
h1.chapter .cb{display:block;font:400 16px/1.6 Inter,sans-serif;color:var(--dim);
               margin-top:10px;letter-spacing:0}
/* The first h2 after a chapter divider shouldn't add a second rule. */
h1.chapter + h2{border-top:none;padding-top:0;margin-top:44px}
"""

# Sidebar table of contents + per-block copy buttons. Both are progressive: the page is
# fully readable with JS off, the sidebar just won't track scroll position.
CHROME_CSS = """
/* --- table of contents --- */
#toc{position:fixed;top:0;left:0;bottom:0;width:var(--toc-w);overflow-y:auto;
     padding:40px 14px 60px 22px;border-right:1px solid var(--line);
     background:var(--bg);z-index:30;display:none;
     scrollbar-width:thin;scrollbar-color:var(--line) transparent}
#toc::-webkit-scrollbar{width:8px}
#toc::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px}
#toc .lbl{font:600 11px JetBrains Mono,monospace;letter-spacing:.14em;
          text-transform:uppercase;color:var(--dim);margin:0 0 14px 10px}
#toc ol{list-style:none;margin:0;padding:0}
#toc .chap{margin:22px 0 8px;padding:0 9px}
#toc .chap:first-child{margin-top:0}
#toc .chap .n{margin:0;font:600 9.5px JetBrains Mono,monospace;letter-spacing:.18em;
              text-transform:uppercase;color:var(--acc);opacity:.85}
#toc .chap .t{margin:2px 0 0;font-size:13px;font-weight:600;color:var(--tx);
              padding-bottom:7px;border-bottom:1px solid var(--line)}
#toc a{display:block;color:var(--dim);text-decoration:none;font-size:13.5px;
       line-height:1.4;padding:6px 9px;border-radius:6px;
       border-left:2px solid transparent;transition:color .12s,background .12s}
#toc a:hover{color:var(--tx);background:var(--panel)}
#toc a.on{color:var(--acc);background:var(--panel);border-left-color:var(--acc)}
#toc .row{display:flex;align-items:flex-start}
#toc .row a{flex:1;min-width:0}
#toc .car{flex:0 0 20px;height:30px;padding:0;margin:0;border:0;cursor:pointer;
          background:none;color:var(--dim);font-size:9px;line-height:30px;
          text-align:center;transition:transform .15s,color .12s}
#toc .car:hover{color:var(--tx)}
#toc .grp.open>.row>.car{transform:rotate(90deg)}
#toc .car.none{visibility:hidden;cursor:default}
#toc .sub{display:none;margin:1px 0 5px}
#toc .grp.open>.sub{display:block}
#toc .sub a{padding-left:29px;font-size:12.5px;color:#7d8ba1}
/* --- narrow-screen drawer --- */
.tocbtn{position:fixed;top:16px;left:16px;z-index:40;width:40px;height:40px;
        border:1px solid var(--line);border-radius:9px;background:var(--panel);
        color:var(--tx);font-size:15px;cursor:pointer;line-height:1}
.tocbtn:hover{border-color:var(--acc);color:var(--acc)}
body.toc-open #toc{display:block;box-shadow:0 0 60px rgba(0,0,0,.6)}
@media (min-width:1200px){
  body{padding-left:var(--toc-w)}
  #toc{display:block}
  .tocbtn{display:none}
}
/* --- copy buttons --- */
pre{position:relative}
/* macOS hides overlay scrollbars until you scroll, which makes a clipped trailing
   comment look like it isn't there. Keep the bar visible whenever a block overflows. */
pre{scrollbar-width:thin;scrollbar-color:var(--line) transparent}
pre::-webkit-scrollbar{height:9px}
pre::-webkit-scrollbar-track{background:transparent}
pre::-webkit-scrollbar-thumb{background:#2c3a50;border-radius:5px}
pre:hover::-webkit-scrollbar-thumb{background:#3d4f6b}
pre .copy{position:absolute;top:8px;right:8px;padding:4px 10px;font:600 11px Inter,sans-serif;
          color:var(--dim);background:#1b2331;border:1px solid var(--line);border-radius:6px;
          cursor:pointer;opacity:0;transition:opacity .13s,color .13s,border-color .13s}
pre:hover .copy,pre .copy:focus-visible{opacity:1}
pre .copy:hover{color:var(--acc);border-color:var(--acc)}
pre .copy.ok{opacity:1;color:var(--good);border-color:var(--good)}
@media (hover:none){pre .copy{opacity:1}}
@media print{#toc,.tocbtn,pre .copy{display:none!important}body{padding-left:0}}
"""

# Figures ported from the deck (slides/index.html). Kept as inline SVG + CSS rather than
# exported images so the page stays dependency-free and the type scales with the reader's zoom.
FIGURE_CSS = """
figure{margin:34px 0;padding:0}
figure figcaption{margin-top:14px;color:var(--dim);font-size:14px;line-height:1.55;
                  text-align:center}
figure svg{display:block;width:100%;height:auto}
/* --- the five-levels ladder --- */
.ladder{display:flex;flex-direction:column;gap:7px}
.ladder .rung{display:grid;grid-template-columns:34px 1fr auto;align-items:center;gap:16px;
              padding:11px 16px;border-radius:9px;border:1px solid var(--line);
              background:var(--panel)}
.ladder .rung p{margin:0}
.ladder .lvl{font:600 17px JetBrains Mono,monospace;color:var(--dim);text-align:center}
.ladder .nm{font-size:17px;font-weight:600}
.ladder .desc{font-size:13.5px;color:var(--dim);text-align:right}
.ladder .free{border-color:var(--good);background:rgba(227,255,143,.07)}
.ladder .free .lvl{color:var(--good)}
.ladder .mid{border-color:#2f4b68}
.ladder .mid .lvl{color:var(--acc)}
.ladder .dear{border-color:var(--bad);background:rgba(251,176,165,.06)}
.ladder .dear .lvl{color:var(--bad)}
.ladder .prod{opacity:.7;border-style:dashed}
/* --- the loop diagram --- */
.ring text{font:12px JetBrains Mono,monospace;fill:var(--tx)}
.ring text.k{fill:var(--acc);font-weight:600}
.ring circle.node{fill:var(--panel);stroke:var(--acc);stroke-width:1.5}
.ring circle.node.hum{stroke:#C78EAD}
.ring path.arc{fill:none;stroke:#6FA8D8;stroke-width:2.6}
/* --- bake-off bars --- */
.bars text{font:12px JetBrains Mono,monospace;fill:var(--dim)}
.bars text.cap{font-weight:600;letter-spacing:.08em;fill:var(--dim);font-size:11px}
.bars text.val{fill:var(--tx);font-weight:600;font-size:13px}
.bars text.nm{fill:var(--tx);font-size:12.5px}
.bars line{stroke:var(--line);stroke-width:1}
"""

CHROME_JS = """
(function(){
  // ---- copy buttons ----
  function fallback(text){
    var ta=document.createElement('textarea');
    ta.value=text;ta.setAttribute('readonly','');
    ta.style.cssText='position:fixed;left:-9999px;top:0';
    document.body.appendChild(ta);ta.select();
    var ok=false;try{ok=document.execCommand('copy');}catch(e){}
    document.body.removeChild(ta);return ok;
  }
  Array.prototype.forEach.call(document.querySelectorAll('pre'),function(pre){
    var b=document.createElement('button');
    b.type='button';b.className='copy';b.textContent='Copy';
    b.setAttribute('aria-label','Copy code to clipboard');
    b.addEventListener('click',function(){
      // Read from <code> so the button's own label never lands in the clipboard.
      var src=pre.querySelector('code')||pre;
      var text=src.textContent;
      function done(ok){
        b.textContent=ok?'Copied':'Press \\u2318C';
        b.classList.toggle('ok',ok);
        setTimeout(function(){b.textContent='Copy';b.classList.remove('ok');},1500);
      }
      if(navigator.clipboard&&navigator.clipboard.writeText){
        navigator.clipboard.writeText(text).then(function(){done(true);},
                                                 function(){done(fallback(text));});
      }else{done(fallback(text));}
    });
    pre.appendChild(b);
  });

  // ---- table of contents ----
  var toc=document.getElementById('toc');
  if(!toc)return;
  var btn=document.querySelector('.tocbtn');
  if(btn)btn.addEventListener('click',function(){
    document.body.classList.toggle('toc-open');
  });

  var links=Array.prototype.slice.call(toc.querySelectorAll('a[href^="#"]'));
  var groups=Array.prototype.slice.call(toc.querySelectorAll('.grp'));
  var targets=links.map(function(a){
    try{return document.getElementById(decodeURIComponent(a.hash.slice(1)));}
    catch(e){return null;}
  });

  // A caret click is an explicit override; don't let the next scroll tick undo it.
  var pinned=null;
  Array.prototype.forEach.call(toc.querySelectorAll('.car:not(.none)'),function(c){
    c.addEventListener('click',function(ev){
      ev.preventDefault();ev.stopPropagation();
      var g=c.closest('.grp');
      g.classList.toggle('open');
      pinned=g.classList.contains('open')?null:g;
    });
  });
  // Following a link means you want that section; clear any override.
  links.forEach(function(a){
    a.addEventListener('click',function(){
      pinned=null;
      if(window.innerWidth<1200)document.body.classList.remove('toc-open');
    });
  });

  function top(el){return el.getBoundingClientRect().top+window.pageYOffset;}

  var active=-1,queued=false;
  function sync(){
    queued=false;
    var y=window.pageYOffset+120,best=-1;
    for(var i=0;i<targets.length;i++){
      if(targets[i]&&top(targets[i])<=y)best=i;else if(targets[i])break;
    }
    // At the very bottom, the last heading may still sit above the line: force it.
    if(window.pageYOffset+window.innerHeight>=
       document.documentElement.scrollHeight-2)best=targets.length-1;
    if(best===active)return;
    active=best;
    links.forEach(function(a,i){
      a.classList.toggle('on',i===best);
      a.setAttribute('aria-current',i===best?'true':'false');
    });
    if(best<0){  // above the first heading: collapse everything rather than go stale
      groups.forEach(function(x){x.classList.remove('on-path');x.classList.remove('open');});
      return;
    }
    var g=links[best].closest('.grp');
    groups.forEach(function(x){
      x.classList.toggle('on-path',x===g);
      if(x!==pinned)x.classList.toggle('open',x===g);
    });
    // Keep the highlight in view without yanking the page.
    var a=links[best],r=a.getBoundingClientRect();
    if(r.top<70||r.bottom>window.innerHeight-40){
      toc.scrollTop+=r.top-window.innerHeight*0.35;
    }
  }
  window.addEventListener('scroll',function(){
    if(!queued){queued=true;requestAnimationFrame(sync);}
  },{passive:true});
  window.addEventListener('resize',sync,{passive:true});
  sync();
})();
"""

# Pygments token classes, hand-mapped to the palette rather than using a
# stock theme (every stock dark theme fights the background).
SYNTAX_CSS = """
pre .k,pre .kn,pre .kd,pre .kc,pre .ow{color:#C78EAD;font-weight:600}
pre .kt{color:#7FC8FF}
pre .nf,pre .fm{color:#E3FF8F}
pre .nc,pre .ne{color:#7FC8FF;font-weight:600}
pre .nd{color:#E3FF8F}
pre .nb,pre .bp{color:#7FC8FF}
pre .s,pre .s1,pre .s2,pre .sa,pre .sd,pre .se,pre .sb,pre .sh,pre .si,pre .sx,pre .sr,pre .ss{color:#A6E3B0}
pre .si,pre .se{color:#E3FF8F}
pre .m,pre .mi,pre .mf,pre .mh,pre .mo,pre .il{color:#FBB0A5}
pre .c,pre .c1,pre .cm,pre .ch,pre .cs,pre .cpf{color:#5f7186;font-style:italic}
pre .o,pre .p{color:#93a2b8}
pre .nn{color:#7FC8FF}
pre .nt{color:#7FC8FF}
pre .na{color:#E3FF8F}
pre .nv,pre .vi,pre .vc,pre .vg{color:#e8edf5}
pre .err,pre .gr{color:#FBB0A5}
"""


def highlight_blocks(markup: str) -> str:
    """Replace <pre><code class="language-X"> bodies with highlighted spans."""
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_by_name
    from pygments.util import ClassNotFound

    fmt = HtmlFormatter(nowrap=True)
    pattern = re.compile(
        r'<pre><code class="language-([\w+-]+)">(.*?)</code></pre>', re.DOTALL
    )

    def sub(m: re.Match) -> str:
        lang, body = m.group(1), m.group(2)
        try:
            lexer = get_lexer_by_name(lang)
        except ClassNotFound:
            return m.group(0)
        out = highlight(_html.unescape(body), lexer, fmt).rstrip("\n")
        return f'<pre><code class="language-{lang}">{out}</code></pre>'

    return pattern.sub(sub, markup)


def build_toc(markup: str) -> str:
    """Nested sidebar TOC from the rendered `<h2>`/`<h3>` ids.

    `h2` is a top-level entry, `h3`s fold under the preceding one. Any `h3` before the
    first `h2` is the subtitle, not a section, so it's skipped.
    """
    heads = re.findall(
        r"<h1 class=\"chapter\" id=\"([^\"]+)\">.*?<span class=\"cn\">([^<]*)</span>"
        r"([^<]*)<span|<h([23]) id=\"([^\"]+)\">(.*?)</h\4>",
        markup,
        re.DOTALL,
    )

    groups: list[dict] = []
    for ch_anchor, ch_num, ch_title, level, anchor, raw in heads:
        if ch_anchor:  # a chapter divider: a label in the sidebar, not a link
            groups.append({"chapter": True, "num": ch_num.strip(),
                           "label": ch_title.strip()})
            continue
        label = _html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
        if level == "2":
            groups.append({"anchor": anchor, "label": label, "kids": []})
        elif groups and not groups[-1].get("chapter"):
            groups[-1]["kids"].append({"anchor": anchor, "label": label})

    out = ['<nav id="toc" aria-label="Table of contents"><p class="lbl">Contents</p><ol>']
    for g in groups:
        if g.get("chapter"):
            out.append(
                f'<li class="chap"><p class="n">{_html.escape(g["num"])}</p>'
                f'<p class="t">{_html.escape(g["label"])}</p></li>'
            )
            continue
        car = '<button class="car" type="button" aria-label="Toggle section">&#9654;</button>'
        if not g["kids"]:
            car = '<span class="car none">&#9654;</span>'
        out.append(
            f'<li class="grp"><div class="row">{car}'
            f'<a href="#{g["anchor"]}">{_html.escape(g["label"])}</a></div>'
        )
        if g["kids"]:
            out.append('<ol class="sub">')
            out += [
                f'<li><a href="#{k["anchor"]}">{_html.escape(k["label"])}</a></li>'
                for k in g["kids"]
            ]
            out.append("</ol>")
        out.append("</li>")
    out.append("</ol></nav>")
    return "".join(out)


def main() -> int:
    try:
        import markdown
    except ImportError:
        print("need: pip install markdown pygments", file=sys.stderr)
        return 1

    body = markdown.markdown(
        SRC.read_text(),
        extensions=["extra", "toc", "sane_lists"],
    )
    body = highlight_blocks(body)
    # Section `---` markers exist for the markdown view on GitHub. In HTML both `h2` and the
    # chapter divider draw their own rule, so an `<hr>` just above one is a second, doubled line.
    body = re.sub(r"<hr\s*/?>\s*(?=<h1 class=\"chapter\"|<h2)", "", body)
    toc = build_toc(body)

    OUT.write_text(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{TITLE}</title>"
        f"<style>{BASE_CSS}{SYNTAX_CSS}{FIGURE_CSS}{CHROME_CSS}</style></head><body>"
        '<button class="tocbtn" type="button" aria-label="Toggle contents">&#9776;</button>'
        f'{toc}<div class="wrap">{body}</div>'
        f"<script>{CHROME_JS}</script></body></html>"
    )
    print(f"wrote {OUT.relative_to(ROOT)}  ({len(OUT.read_text()):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
