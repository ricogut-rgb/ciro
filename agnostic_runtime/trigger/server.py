"""Trigger service — a stdlib HTTP surface the phone hits to START/steer a run.

The service never executes a run itself; it delegates to a RunManager. Bearer-token
auth is mandatory (no token configured -> 503, never run open). Bound to 127.0.0.1 by
default: exposing this beyond localhost (0.0.0.0 or a tunnel) is the human's explicit
choice and demands a strong token.

GET / serves a plain, no-auth console (public HTML that carries the token in its own
fetch() headers). The console is presence-first: the standing surface is only the
conversation with 7; the machine-necessities (token, model) live behind a settings gear
and surface only when needed. Confirm crosses to the human — nothing fires until you allow it.
"""

from __future__ import annotations

import io
import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from agnostic_runtime.trigger import plate as plate_mod
from agnostic_runtime.trigger import invites as _invites
from agnostic_runtime.trigger.runs import RunManager
from agnostic_runtime.trigger.users import seed_user, user_for_email, user_for_token

# The request handler answers, or refuses — it never hangs a worker.
#   MAX_BODY caps how many bytes we will even attempt to read, so an oversized (honest or
#   hostile) Content-Length is rejected with 413 BEFORE any read blocks.
#   READ_TIMEOUT bounds every socket op, so a client that under-delivers its declared body (or
#   dribbles headers) trips the timeout and its connection is closed rather than pinning a
#   ThreadingHTTPServer thread forever (slowloris). Both are hardening against hostile clients.
MAX_BODY = 256 * 1024
READ_TIMEOUT = 30.0

# BETA_PAUSED sentinel — the incident-pause wall. If the operator (or an incident playbook)
# places this file, the surface refuses to route new asks to the daemon and shows a
# plain-English maintenance message. stat-at-every-ask (no caching) so pause/unpause is
# immediate. One wall an incident response can raise from a terminal, no code deploy needed.
BETA_PAUSED_PATH = os.path.expanduser("~/.agnostic_runtime/BETA_PAUSED")


def _beta_paused() -> bool:
    try:
        return os.path.exists(BETA_PAUSED_PATH)
    except OSError:
        return False


_WHY_HOLDING_HTML = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CIRO — Why</title>
<style>
:root{--ground:#f4ede1;--ink:#2b241d;--muted:#6e604d;--serif:'Iowan Old Style','Charter','Palatino','Georgia',serif}
@media(prefers-color-scheme:dark){:root{--ground:#17120d;--ink:#ebe0cd;--muted:#9d8f77}}
html,body{height:100%;margin:0}
body{background:var(--ground);color:var(--ink);font-family:var(--serif);display:flex;align-items:center;justify-content:center;padding:2rem;line-height:1.6}
main{max-width:30rem;text-align:center}
h1{font-style:italic;font-weight:400;font-size:28px;margin:0 0 1.5rem;letter-spacing:.01em}
p{font-size:17px;color:var(--muted);margin:0 0 1rem}
a{color:var(--muted);text-decoration:underline;text-underline-offset:3px}
a:hover{color:var(--ink)}
.back{margin-top:2.5rem;font-size:14px}
</style>
</head><body><main>
<h1>Why building generative technology in 2026 should cost nothing.</h1>
<p>The piece is being written. Thank you for the curiosity &mdash; it means the question is landing.</p>
<p class="back"><a href="/">&larr; back to CIRO</a></p>
</main></body></html>
"""


_CONSOLE_HTML = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CIRO</title>
<style>
/* --- tokens: light default, explicit dark/light, OS-dark fallback unless pinned light --- */
:root{color-scheme:light;
  --ground:#f4ede1;--ink:#2b241d;--deep:#4d3f33;--muted:#6e604d;--faint:#b5a892;--rule:#d8ccb8;
  --accent:#a86d2f;--panel:#ede4d3;--field:#faf5ec;
  --seat:#2f5a86;--seat-rule:#bcc6cf;--seat-bg:#eae7e0;
  --att:#715b0b;--att-note:#685408;--att-rule:#b8973a;--att-bg:#efe6c9;
  --att-fail:#9b3a2c;--att-fail-note:#87301f;--att-fail-rule:#c98a7a;--att-fail-bg:#f1e1d8;
  --err:#b3261e;--cta-bg:#2b241d;--cta-ink:#f4ede1;
  --firm:#715b0b;--firm-tag:#685408;--firm-rule:#c8ad5e;--firm-bg:#f0e7cb;--firm-ink:#4d3f1a;--formed-bg:#e6e9ea;
  --hearth:rgba(214,150,70,0.10);
  --sun-blue:#35609c;--sun-yellow:#d7a23a;--sun-orange:#c56a2c;--sun-green:#6b8f3e;--sun-terra:#a9552d;--sun-cobalt:#294e86;--sun-center:#fdf7ea;--sun-glow:#f0dcad;--sun-seam:#e4007c;--sun-zpring:#c9b690;
  --sun-blue-2:#2a5390;--sun-yellow-2:#c08f2c;--sun-cobalt-2:#1d3c6f;--sun-orange-2:#a9541f;--sun-terra-2:#8f4120;--sun-green-2:#55762e;--sun-gold:#c9a24a;--sun-black:#13100b;
}
:root[data-theme="dark"]{color-scheme:dark;
  --ground:#17120d;--ink:#ebe0cd;--deep:#d4c5b0;--muted:#9d8f77;--faint:#4d4235;--rule:#3a2f23;
  --accent:#d9975a;--panel:#241d14;--field:#1f1810;
  --hearth:rgba(216,148,74,0.11);
  --sun-blue:#4d7cba;--sun-yellow:#d9a53f;--sun-orange:#cf7636;--sun-green:#80a14c;--sun-terra:#bc6338;--sun-cobalt:#3f68a4;--sun-center:#f4e8d1;--sun-glow:#caa968;--sun-seam:#ff47a4;--sun-zpring:#5a4a34;
  --sun-blue-2:#3a66a0;--sun-yellow-2:#b78a2e;--sun-cobalt-2:#2e5285;--sun-orange-2:#a85c25;--sun-terra-2:#984a26;--sun-green-2:#5f7a37;--sun-gold:#d6b864;--sun-black:#241e16;
  --seat:#8fb3d9;--seat-rule:#2c3d50;--seat-bg:#141a21;
  --att:#d9b23a;--att-note:#d9b23a;--att-rule:#6a5420;--att-bg:#17130a;
  --att-fail:#e07a66;--att-fail-note:#e8907e;--att-fail-rule:#6b3328;--att-fail-bg:#1b0f0c;
  --err:#f07a6c;--cta-bg:#ebe0cd;--cta-ink:#14100c;
  --firm:#d9b23a;--firm-tag:#d9b23a;--firm-rule:#5a4818;--firm-bg:#17140a;--firm-ink:#e0d8a8;--formed-bg:#121a24;
}
:root[data-theme="light"]{color-scheme:light;
  --ground:#f4ede1;--ink:#2b241d;--deep:#4d3f33;--muted:#6e604d;--faint:#b5a892;--rule:#d8ccb8;
  --accent:#a86d2f;--panel:#ede4d3;--field:#faf5ec;
  --seat:#2f5a86;--seat-rule:#bcc6cf;--seat-bg:#eae7e0;
  --att:#715b0b;--att-note:#685408;--att-rule:#b8973a;--att-bg:#efe6c9;
  --att-fail:#9b3a2c;--att-fail-note:#87301f;--att-fail-rule:#c98a7a;--att-fail-bg:#f1e1d8;
  --err:#b3261e;--cta-bg:#2b241d;--cta-ink:#f4ede1;
  --firm:#715b0b;--firm-tag:#685408;--firm-rule:#c8ad5e;--firm-bg:#f0e7cb;--firm-ink:#4d3f1a;--formed-bg:#e6e9ea;
  --hearth:rgba(214,150,70,0.10);
  --sun-blue:#35609c;--sun-yellow:#d7a23a;--sun-orange:#c56a2c;--sun-green:#6b8f3e;--sun-terra:#a9552d;--sun-cobalt:#294e86;--sun-center:#fdf7ea;--sun-glow:#f0dcad;--sun-seam:#e4007c;--sun-zpring:#c9b690;
  --sun-blue-2:#2a5390;--sun-yellow-2:#c08f2c;--sun-cobalt-2:#1d3c6f;--sun-orange-2:#a9541f;--sun-terra-2:#8f4120;--sun-green-2:#55762e;--sun-gold:#c9a24a;--sun-black:#13100b;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
  --ground:#17120d;--ink:#ebe0cd;--deep:#d4c5b0;--muted:#9d8f77;--faint:#4d4235;--rule:#3a2f23;
  --accent:#d9975a;--panel:#241d14;--field:#1f1810;
  --hearth:rgba(216,148,74,0.11);
  --sun-blue:#4d7cba;--sun-yellow:#d9a53f;--sun-orange:#cf7636;--sun-green:#80a14c;--sun-terra:#bc6338;--sun-cobalt:#3f68a4;--sun-center:#f4e8d1;--sun-glow:#caa968;--sun-seam:#ff47a4;--sun-zpring:#5a4a34;
  --sun-blue-2:#3a66a0;--sun-yellow-2:#b78a2e;--sun-cobalt-2:#2e5285;--sun-orange-2:#a85c25;--sun-terra-2:#984a26;--sun-green-2:#5f7a37;--sun-gold:#d6b864;--sun-black:#241e16;
  --seat:#8fb3d9;--seat-rule:#2c3d50;--seat-bg:#141a21;
  --att:#d9b23a;--att-note:#d9b23a;--att-rule:#6a5420;--att-bg:#17130a;
  --att-fail:#e07a66;--att-fail-note:#e8907e;--att-fail-rule:#6b3328;--att-fail-bg:#1b0f0c;
  --err:#f07a6c;--cta-bg:#ebe0cd;--cta-ink:#14100c;
  --firm:#d9b23a;--firm-tag:#d9b23a;--firm-rule:#5a4818;--firm-bg:#17140a;--firm-ink:#e0d8a8;--formed-bg:#121a24;
}}
:root{--serif:'Iowan Old Style','Iowan Old Style W7','Charter','Palatino','Georgia',serif;
  --sans:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Helvetica,Arial,system-ui,sans-serif}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--ground);color:var(--ink);font:16px/1.55 var(--sans);-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;display:flex;flex-direction:row;overflow:hidden}
/* --- the dock: conversation with 7 --- */
#dock{width:35%;max-width:430px;min-width:290px;display:flex;flex-direction:column;background:var(--ground);border-right:1px solid var(--rule);padding:20px 20px 18px}
.brand{display:flex;align-items:baseline;justify-content:space-between;flex:0 0 auto}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.sig{font-family:var(--serif);font-style:italic;font-size:22px;color:var(--deep);letter-spacing:.02em}
.gear{width:auto;background:none;border:none;color:var(--muted);font-size:19px;padding:4px 6px;margin:0;cursor:pointer}
.gear:hover{color:var(--ink)}
.sub{color:var(--muted);font-size:14px;line-height:1.5;margin:.5rem 0 0;flex:0 0 auto}
#convo{margin-top:.85rem;overflow-y:auto;flex:1 1 auto;min-height:0;overflow-anchor:none}
#convo:empty{flex:0 0 auto}
#compose{flex:0 0 auto;padding-top:.7rem;position:relative}
label{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:.8rem 0 .3rem}
input,textarea,select,button{width:100%;box-sizing:border-box;font:inherit;border-radius:12px;border:1px solid var(--rule);background:var(--field);color:var(--ink);padding:12px}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--muted)}
textarea{display:block;min-height:64px;max-height:min(32vh,240px);resize:none;overflow-y:auto}
textarea::placeholder,input::placeholder{color:var(--muted);opacity:1}
button{background:var(--cta-bg);border-color:var(--cta-bg);color:var(--cta-ink);font-weight:600;margin-top:.7rem;-webkit-appearance:none;cursor:pointer}
button.sec{background:transparent;border-color:var(--rule);color:var(--ink);font-weight:500}
button:active{opacity:.65}
/* --- Send: the hairline handle (the one affordance that carries it) --- */
#compose button.send{display:block;width:auto;margin:.55rem 0 0 auto;padding:6px 2px 9px;background:none;border:none;border-radius:0;color:var(--deep);font:500 12px/1 var(--sans);letter-spacing:.1em;text-transform:lowercase;position:relative;transition:color .2s}
#compose button.send::after{content:'';position:absolute;right:0;bottom:0;width:56px;height:2px;border-radius:1px;background:var(--muted);opacity:.55;transition:opacity .25s,background .2s,width .25s}
#compose button.send::before{content:'';position:absolute;inset:-14px -14px}
#compose button.send:hover,#compose button.send:focus-visible{color:var(--ink);outline:none}
#compose button.send:hover::after,#compose button.send:focus-visible::after{background:var(--accent);opacity:1;width:72px}
#compose button.send:disabled{color:var(--muted);cursor:default}
#compose button.send:disabled::after,#compose button.send:disabled:hover::after{background:var(--muted);opacity:.3;width:56px}
/* --- reading while 7 writes: the jump back to the newest (shown only when the reader is scrolled up) --- */
#jump{position:absolute;left:50%;top:-46px;transform:translateX(-50%);width:auto;margin:0;padding:7px 14px;border-radius:999px;background:var(--field);border:1px solid var(--rule);color:var(--ink);font:500 12px/1 var(--sans);letter-spacing:.06em;text-transform:lowercase;box-shadow:0 2px 8px rgba(0,0,0,.14);z-index:2}
#jump[hidden]{display:none}
#jump:hover{border-color:var(--muted)}
#jump:focus-visible,.bubble button.copy:focus-visible{outline:2px solid var(--seat);outline-offset:2px}
.row{display:flex;gap:8px;margin-top:.7rem}.row>*{flex:1;margin-top:0}
.bubble button{font:600 15px/1.2 var(--sans)}.bubble button.sec{font-weight:500}
.cfg{border:1px solid var(--rule);border-radius:12px;padding:2px 14px 14px;margin-top:.7rem;background:var(--panel);flex:0 0 auto}
.cfg-welcome{font:italic 14.5px/1.55 var(--serif);color:var(--deep);margin:12px 2px 4px}
.cfg-advanced{margin-top:.9rem}
.cfg-advanced>summary{color:var(--muted);font:12px var(--sans);cursor:pointer;list-style:none;padding:4px 0}
.cfg-advanced>summary::-webkit-details-marker{display:none}
.cfg-advanced>summary::before{content:"▸ ";color:var(--muted)}
.cfg-advanced[open]>summary::before{content:"▾ "}
/* --- bubbles in the dock --- */
@keyframes settle{0%{opacity:0;transform:translateY(4px)}100%{opacity:1;transform:translateY(0)}}
.bubble{border:1px solid var(--seat-rule);background:var(--seat-bg);border-radius:14px;padding:14px 16px;margin-top:.9rem;font:17px/1.55 var(--serif);max-width:88%;white-space:pre-wrap;word-break:break-word}
.bubble:not(.muted){animation:settle .55s ease-out}
.bubble.you{border-color:var(--rule);background:transparent;color:var(--deep);margin-left:auto;font:16px/1.55 var(--sans)}
.who{color:var(--seat);font:600 13px/1.4 var(--sans);margin-bottom:.35rem}
.you .who{color:var(--muted);text-align:right}
.muted{color:var(--muted);font-size:13px}
.bubble.muted{font:13px/1.55 var(--sans);background:transparent;border-color:var(--rule)}
.err{color:var(--err)}
.bubble.err{font:14px/1.55 var(--sans);border-color:var(--err);background:transparent}
details{margin-top:.7rem}summary{color:var(--muted);font:12px var(--sans);cursor:pointer;list-style:none}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px;color:var(--muted);margin:.4rem 0 0}
.bubble.pending{border-color:var(--seat)}
/* --- standing attendant: the autonomous answerer when no one is live --- */
/* Deliberately NOT the present-seat slate of .who and NOT the sienna accent: gold + a dashed edge so a
   reader can tell at a glance that no live seat is on the other side — the door's keeper answered,
   authority-reduced. */
.bubble.att{border-color:var(--att-rule);border-style:dashed;background:var(--att-bg)}
.att .att-who{color:var(--att)}
.att-note{font-family:var(--sans);color:var(--att-note);font-size:.7rem;letter-spacing:.1em;text-transform:lowercase;font-weight:400;margin:-.2rem 0 .55rem}
.bubble.att.att-failed{border-color:var(--att-fail-rule);background:var(--att-fail-bg)}
.att-failed .att-who{color:var(--att-fail)}
.att-failed .att-note{color:var(--att-fail-note)}
.dhealth{margin-top:.5rem;font-family:var(--sans);font-size:.7rem;letter-spacing:.1em;color:var(--muted)}
#convo .bubble:first-child{margin-top:0}
/* --- per-reply copy + safe links --- */
.bubble .acts{margin-top:.5rem;line-height:1}
.bubble button.copy{width:auto;margin:0;padding:3px 0;background:none;border:none;border-radius:0;color:var(--muted);font:500 12px/1.2 var(--sans);letter-spacing:.08em;text-transform:lowercase;opacity:0;transition:opacity .15s}
.bubble:hover button.copy,.bubble:focus-within button.copy{opacity:1}
.bubble button.copy:hover{color:var(--ink)}
@media (hover:none){.bubble button.copy{opacity:1}}
.bubble a{color:inherit;text-decoration:underline;text-underline-offset:2px;text-decoration-thickness:1px}
.bubble a:hover{text-decoration-thickness:2px}
.think{font-style:italic}
/* --- the canvas: the creation forming --- */
/* A soft warm hearth-glow on the canvas (dark only; --hearth is transparent in light).
   This is what carries temperature — the darkness stays, the FEEL changes. */
#canvas{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;background:
  radial-gradient(ellipse 80% 55% at 50% 28%, var(--hearth), transparent 70%),
  var(--panel)}
.canvas-head{flex:0 0 auto;padding:16px 26px 8px;border-bottom:1px solid var(--rule)}
.canvas-title{font-family:var(--sans);font-size:.7rem;text-transform:lowercase;letter-spacing:.1em;color:var(--muted)}
.canvas-sub{font-family:var(--sans);font-size:.7rem;text-transform:lowercase;letter-spacing:.1em;color:var(--muted);margin-top:2px}
/* When the hero is on-screen (creation is empty / first encounter), the cold "the creation so far"
   eyebrow would compete with the reception — hide it until there is actually something to label. */
#canvas:has(.reception) .canvas-head{display:none}
#creation{flex:1 1 auto;overflow-y:auto;padding:16px 26px 44px}
/* --- hero reception: the mosaic-sun holds the room; one line earns its place --- */
/* The figure IS the welcome — a sun re-composed from ceramic mosaic fragments: irregular,
   hand-cut tesserae in Talavera colours, laid in concentric rings that spiral gently inward,
   grout-gaps of warm cream showing between them. At the centre a small CLEAR, light-filled
   glass sphere — the zero-point you see *through*, not an inked dot. A faint mended fracture
   crosses the whole: broken, re-joined. The mosaic-sun (CIRO, warm bloom) IS the compass-rosette
   (the concentric/radial armature): same tessellated radial shape, two registers, bound. All
   colour runs on the --sun-* tokens so it blooms on cream and stays warm on the dark hearth.
   Grout = the page --ground. */
.reception{max-width:30rem;margin:13vh auto 0;padding:0 4px;font-family:var(--serif);color:var(--ink);text-align:center}
.sunrose{display:block;width:min(336px,62vw);height:auto;margin:0 auto 2.3rem}
.ms-t{stroke:var(--ground);stroke-width:.5;stroke-linejoin:round}
.ms-b{fill:var(--sun-blue)}
.ms-y{fill:var(--sun-yellow)}
.ms-o{fill:var(--sun-orange)}
.ms-n{fill:var(--sun-green)}
.ms-r{fill:var(--sun-terra)}
.ms-d{fill:var(--sun-cobalt)}
.ms-seam{fill:none;stroke:var(--sun-seam);stroke-width:4.6;opacity:1;stroke-linecap:round;stroke-linejoin:round}
.ms-mend{stroke:var(--sun-seam);stroke-width:3.2;opacity:1;stroke-linecap:round}
.ms-c0{stop-color:var(--sun-center)}
.ms-c1{stop-color:var(--sun-glow)}
.ms-c2{stop-color:var(--ground)}
.ms-zpring{fill:none;stroke:var(--sun-zpring);stroke-width:1;opacity:.7}
.ms-spec{fill:#fff;opacity:.5}
.reception .r-open{font:italic 400 26px/1.4 var(--serif);color:var(--ink);margin:0}
.reception .r-build{font:italic 400 18px/1.3 var(--serif);color:var(--muted);letter-spacing:.04em;margin:.4rem 0 0}
.reception .r-offer{font:italic 400 19px/1.45 var(--serif);color:var(--deep);margin:2.4rem 0 0}
.reception .r-prompt{font:400 15px/1.5 var(--sans);color:var(--muted);letter-spacing:.05em;margin:2.7rem 0 1.05rem}
.r-chips{display:flex;flex-wrap:wrap;justify-content:center;gap:10px;max-width:25rem;margin:0 auto}
.r-chip{width:auto;margin:0;font:400 15px/1 var(--serif);color:var(--muted);background:var(--panel);border:1px solid var(--rule);border-radius:999px;padding:10px 17px;cursor:pointer;transition:color .15s,border-color .15s,background .15s}
.r-chip:hover{color:var(--ink);border-color:var(--muted);background:var(--field)}
.r-chip:focus-visible{outline:2px solid var(--seat);outline-offset:2px}
.r-chip[disabled]{pointer-events:none;opacity:.5}
.reception .r-why{font:400 13.5px/1.6 var(--sans);letter-spacing:.03em;max-width:25rem;margin:2.9rem auto 0}
.r-why a{color:var(--muted);text-decoration:underline;text-underline-offset:3px;text-decoration-thickness:1px;text-decoration-color:var(--faint)}
.r-why a:hover{color:var(--accent);text-decoration-color:var(--accent)}
@media(max-width:600px){
  /* On phone the canvas is a short strip above the conversation; the figure + its one line must
     fit WITHIN it, so the sun shrinks to a warm anchor and the margins tighten. */
  .reception{margin-top:0}
  .sunrose{width:min(120px,32vw);margin-bottom:.5rem}
  .reception .r-open{font-size:17px}
}
/* --- significance zones --- */
.zone{margin-bottom:20px}
.zone-label{font-size:10px;text-transform:uppercase;letter-spacing:.16em;margin-bottom:8px;padding-bottom:4px}
.zone-formed .zone-label{color:var(--seat);border-bottom:1px solid var(--seat-rule)}
.zone-firming .zone-label{color:var(--firm);border-bottom:1px solid var(--firm-rule)}
.zone-flowing .zone-label{color:var(--muted);border-bottom:1px solid var(--rule)}
.zone-flowing .atom{font-size:13px;padding:8px 12px;opacity:.7}
/* --- atoms --- */
.atom{border-radius:10px;margin-top:8px;line-height:1.5}
.atom:first-child{margin-top:0}
.atom-formed{background:var(--formed-bg);border:1px solid var(--seat-rule);color:var(--ink);font-size:17px;font-weight:600;padding:14px 16px;white-space:pre-wrap;word-break:break-word}
.atom-firming{background:var(--firm-bg);border:1px dashed var(--firm-rule);color:var(--firm-ink);font-size:14px;padding:10px 14px 10px 14px;position:relative;white-space:pre-wrap;word-break:break-word}
.atom-firming .reading-tag{position:absolute;top:6px;right:10px;font-size:11px;letter-spacing:.06em;color:var(--firm-tag)}
.svg-wrap{display:flex;justify-content:center;padding:8px 0}
.svg-wrap svg{max-width:100%;height:auto;background:#fff;border-radius:6px;padding:8px}
.canvas-hint{font-size:12px;color:var(--muted);margin-top:16px;padding:0 4px;line-height:1.6}
/* --- the plate: the next thing that needs you, and (zoomed out) everything on it --- */
#plate{flex:0 1 auto;max-height:64vh;overflow-y:auto;padding:16px 26px 14px;border-bottom:1px solid var(--rule)}
#plate[hidden]{display:none}
.p-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:10px}
.p-eyebrow{font:.7rem/1.4 var(--sans);text-transform:lowercase;letter-spacing:.1em;color:var(--muted)}
#plate button.p-zoom{width:auto;margin:0;padding:4px 0;background:none;border:none;border-radius:0;color:var(--deep);font:500 12px/1.3 var(--sans);letter-spacing:.06em;text-transform:lowercase;text-decoration:underline;text-underline-offset:3px;text-decoration-thickness:1px}
#plate button.p-zoom:hover{color:var(--ink);text-decoration-thickness:2px}
#plate button:focus-visible{outline:2px solid var(--seat);outline-offset:2px}
.pcard{background:var(--field);border:1px solid var(--rule);border-radius:14px;padding:16px 18px}
.p-num{font:12px/1.3 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--muted);letter-spacing:.04em;margin:0 0 4px}
.pcmd{margin-top:.5rem;font:13px/1.4 var(--sans);color:var(--muted);text-align:right;white-space:normal}
.pcmd.bad{color:var(--err)}
.pundo .pnote{margin:0 0 .35rem;color:var(--muted)}
.p-title{font:600 20px/1.3 var(--serif);color:var(--ink);margin:0 0 6px;word-break:break-word}
.p-why{font:16px/1.55 var(--serif);color:var(--deep);margin:0;white-space:pre-wrap;word-break:break-word}
.p-choices{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
#plate .p-choices button{width:auto;margin:0;padding:9px 14px;border-radius:10px;background:transparent;border:1px solid var(--muted);color:var(--ink);font:500 14px/1.25 var(--sans);text-align:left;max-width:100%;word-break:break-word}
#plate .p-choices button:hover{border-color:var(--ink)}
#plate .p-choices button:disabled{opacity:1;color:var(--muted);border-color:var(--rule);cursor:default}
.p-status{font:13px/1.45 var(--sans);margin-top:8px;color:var(--muted)}
.p-status:empty{display:none}
.p-status.ok{color:var(--seat)}
.p-status.bad{color:var(--err)}
.p-meta{margin-top:12px;font:12px/1.5 var(--sans);color:var(--muted);word-break:break-word}
.p-meta>div+div{margin-top:3px}
.p-chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.p-chip{border:1px solid var(--rule);border-radius:999px;padding:1px 8px;color:var(--deep);background:var(--ground)}
.p-label{color:var(--muted)}
.p-last{font:13px/1.45 var(--sans);color:var(--seat);margin:0 0 10px}
.p-last:empty{display:none}
.p-empty{font:italic 15px/1.6 var(--serif);color:var(--muted);margin:4px 0 0}
.p-group{margin-top:18px}
.p-group:first-of-type{margin-top:4px}
.p-group h3{font:10px/1.4 var(--sans);text-transform:uppercase;letter-spacing:.16em;color:var(--deep);margin:0 0 8px;padding-bottom:4px;border-bottom:1px solid var(--rule);font-weight:600}
.p-group .pcard{padding:12px 14px;margin-top:8px}
.p-group .p-title{font-size:16px;margin-bottom:4px}
.p-group .p-why{font-size:14px}
.p-group .p-choices{margin-top:10px}
.p-chose{font:13px/1.45 var(--sans);color:var(--deep);margin-top:6px}
/* --- undo: a plate choice taken back (dock bubble and settled card share one control) --- */
.pundo{margin-top:.55rem;font:13px/1.3 var(--sans);color:var(--muted);text-align:right;white-space:normal}
.pundo:empty{display:none}
.pcard .pundo{text-align:left}
button.undo{width:auto;margin:0;padding:6px 12px;min-height:32px;border-radius:10px;background:transparent;border:1px solid var(--muted);color:var(--ink);font:500 13px/1.2 var(--sans)}
button.undo:hover{border-color:var(--ink)}
button.undo.arm{border-color:var(--err);color:var(--err);font-weight:600}
button.undo:disabled{opacity:1;color:var(--muted);border-color:var(--rule);cursor:default}
button.undo:focus-visible{outline:2px solid var(--seat);outline-offset:2px}
.pundo .bad{color:var(--err)}
body.zoomed .canvas-head,body.zoomed #creation{display:none}
body.zoomed #plate{flex:1 1 auto;max-height:none;border-bottom:none}
/* --- awaiting-approval group: prepared-B items staged by the daemon --- */
/* A distinct register from needs_you — these are hash-bound, time-bound, and reversible
   proposals (worktree builds, config patches, drafts, holds). Rendered with the file list
   BEFORE the diff body so legit-looking filenames cannot be shadowed by a buried payload. */
.p-await{border-color:var(--att-rule);background:var(--att-bg)}
.p-await .p-title{color:var(--att-note)}
.pbshape{font:11px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--att-note);letter-spacing:.03em;text-transform:none;margin:0 0 6px}
.pbdesc{font:14px/1.55 var(--serif);color:var(--deep);margin:0 0 10px;white-space:pre-wrap;word-break:break-word}
.pbmeta{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font:12.5px/1.5 var(--sans);color:var(--deep);margin:10px 0;padding:10px 12px;background:var(--ground);border-radius:8px;border:1px solid var(--rule)}
.pbmeta .k{color:var(--muted);text-transform:lowercase;letter-spacing:.03em}
.pbmeta .v{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;word-break:break-all;color:var(--ink)}
.pbmeta .v.big{font-size:14px;font-family:var(--sans);color:var(--att-note);font-weight:600}
.pbfiles{margin:8px 0;padding:8px 12px;background:var(--ground);border-radius:8px;border:1px solid var(--rule)}
.pbfiles h4{margin:0 0 4px;font:600 11px/1.4 var(--sans);color:var(--deep);text-transform:uppercase;letter-spacing:.08em}
.pbfiles ul{margin:0;padding:0 0 0 18px;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--ink);word-break:break-all}
.pbfiles li{margin:1px 0}
.pbfiles .more{color:var(--muted);font-family:var(--sans);font-size:12px}
.pbdiff{margin:8px 0;border:1px solid var(--rule);border-radius:8px;background:var(--ground);overflow:hidden}
.pbdiff summary{padding:8px 12px;color:var(--muted);font:12px/1.4 var(--sans);cursor:pointer;list-style:none}
.pbdiff summary::-webkit-details-marker{display:none}
.pbdiff summary::before{content:"▸ ";color:var(--muted)}
.pbdiff[open] summary::before{content:"▾ "}
.pbdiff pre{margin:0;padding:10px 12px;max-height:40vh;overflow:auto;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--ink);background:var(--field);border-top:1px solid var(--rule);white-space:pre;word-break:normal;tab-size:4}
.pbdiff .cap{padding:6px 12px;color:var(--muted);font:12px/1.4 var(--sans);border-top:1px solid var(--rule);background:var(--panel)}
.pbrevert{font:12px/1.5 var(--sans);color:var(--muted);margin:6px 0 0}
.pbrevert code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px;color:var(--deep)}
.pbexp{display:inline-flex;align-items:baseline;gap:6px;font:12.5px/1.4 var(--sans);color:var(--att-note);margin:8px 0 0}
.pbexp.urgent{color:var(--err)}
.pbexp.stale{color:var(--err);font-weight:600}
.pbactions{display:flex;gap:8px;margin-top:14px}
#plate .pbactions button{width:auto;margin:0;padding:9px 16px;border-radius:10px;font:600 14px/1.25 var(--sans);text-align:center}
#plate .pbactions button.approve{background:var(--cta-bg);border-color:var(--cta-bg);color:var(--cta-ink)}
#plate .pbactions button.approve:hover{opacity:.88}
#plate .pbactions button.reject{background:transparent;border:1px solid var(--muted);color:var(--ink);font-weight:500}
#plate .pbactions button.reject:hover{border-color:var(--err);color:var(--err)}
#plate .pbactions button.approve.arm{background:var(--att-note);border-color:var(--att-note)}
#plate .pbactions button:disabled{opacity:.6;cursor:default}
.pbstdout{margin:8px 0 0;padding:8px 12px;background:var(--ground);border-radius:8px;border:1px dashed var(--rule);font:11.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--muted);white-space:pre-wrap;word-break:break-word;max-height:12em;overflow:auto}
.pbstdout .lbl{display:block;font-family:var(--sans);color:var(--deep);font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px}
.pbdecision{margin-top:6px;padding:10px 12px;background:var(--ground);border-radius:8px;font:13px/1.5 var(--sans);color:var(--deep)}
.pbdecision.approved{border:1px solid var(--seat-rule);color:var(--seat)}
.pbdecision.rejected{border:1px solid var(--rule);color:var(--muted)}
/* Maintenance banner — BETA_PAUSED sentinel is present on disk. */
#paused{display:none;padding:12px 18px;margin:0;background:var(--att-fail-bg);border:1px solid var(--att-fail-rule);color:var(--att-fail-note);font:14px/1.5 var(--serif);border-radius:0}
body.paused #paused{display:block}
body.paused #plate .pbactions button,body.paused #compose button.send{opacity:.5;pointer-events:none}
/* --- narrow: stack --- */
@media(max-width:760px){
  body{flex-direction:column-reverse}
  #dock{width:100%;max-width:none;min-width:0;border-right:none;border-top:1px solid var(--rule);padding:14px 16px;max-height:50vh}
  #convo{max-height:28vh}
  #canvas{width:100%}
  .canvas-head{padding:14px 18px 8px}
  #creation{padding:10px 18px 30px}
  .reception{margin-top:6vh}
}
/* --- phone: the conversation gets most of the screen; the canvas keeps a short strip above it --- */
@media(max-width:600px){
  #dock{flex:1 1 auto;min-height:0;max-height:none}
  #convo,#convo:empty{max-height:none;flex:1 1 auto}
  #canvas{flex:0 0 auto;max-height:26vh;max-height:26dvh}
  #creation{min-height:0}
  /* with a plate card, the strip opens enough to hold it; the whole canvas scrolls as one */
  body.has-plate #canvas{max-height:46vh;max-height:46dvh;display:block;overflow-y:auto}
  body.has-plate #plate{max-height:none;padding:12px 16px 12px}
  body.has-plate #creation{overflow:visible}
  body.zoomed #canvas{max-height:62vh;max-height:62dvh}
  .pcard{padding:14px 14px}
  .p-title{font-size:18px}
}
@media (prefers-reduced-motion:reduce){*{animation:none !important;transition:none !important}}
</style></head><body>
<h1 class="sr-only">CIRO</h1>
<div id="dock">
  <div class="brand"><span class="sig">7</span><button class="gear" onclick="cfg()" title="settings">&#9881;</button></div>
  <p class="sub">Not sure where to start? Click in the box and hit enter &mdash; signup opens when you&rsquo;re ready.</p>
  <div id="signup" class="cfg" style="display:none" role="dialog" aria-modal="true" aria-labelledby="su-heading">
    <p class="cfg-welcome" id="su-heading">The space opens by invite. If you were sent one, bring it in.</p>
    <label for="su-invite">invite</label><input id="su-invite" type="text" placeholder="paste the code you were sent" autocomplete="off" autocapitalize="off" spellcheck="false">
    <label for="su-email">email</label><input id="su-email" type="email" placeholder="you@wherever" autocomplete="email" inputmode="email">
    <label for="su-role">i&rsquo;m a&hellip; <span style="text-transform:none;letter-spacing:0;color:var(--faint)">(optional)</span></label><input id="su-role" type="text" placeholder="builder, writer, carrier of a decision" autocomplete="off">
    <button id="su-go" type="button" onclick="signup()">Sign up</button>
    <p class="muted" style="font-size:12px;line-height:1.55;margin-top:.7rem">Your email stays with CIRO &mdash; not sold, not tracked. Export will be yours.</p>
    <p id="su-err" class="err" role="alert" style="font-size:13px;margin:.4rem 0 0;display:none"></p>
    <button class="sec" type="button" onclick="dsignup()">not now</button>
    <noscript>
      <p class="muted" style="font-size:12px;margin:.7rem 0 .4rem">Your browser has JavaScript off. You can still sign up:</p>
      <form action="/signup" method="post" enctype="application/x-www-form-urlencoded">
        <label for="ns-invite">invite</label><input id="ns-invite" name="invite_code" type="text" required>
        <label for="ns-email">email</label><input id="ns-email" name="email" type="email" required>
        <label for="ns-role">i&rsquo;m a&hellip; (optional)</label><input id="ns-role" name="declared_role" type="text">
        <button type="submit">Sign up</button>
      </form>
    </noscript>
  </div>
  <div id="cfg" class="cfg" style="display:none">
    <p class="cfg-welcome">The invite you were sent carries what you need. If you arrived here first — hi. The space waits.</p>
    <label>token</label><input id="tok" type="password" placeholder="paste once">
    <details class="cfg-advanced"><summary>model</summary>
      <label>model</label><select id="model"><option value="cathedral">CIRO (runs on Anthropic's Claude, on this machine)</option><option value="demo">demo (scripted, zero compute)</option><option value="ollama">ollama (local, private)</option><option value="claude">claude (cloud)</option><option value="stub">stub (test)</option></select>
      <button class="sec" onclick="fresh();cfg()">start a fresh conversation</button>
    </details>
    <button class="sec" onclick="cfg()">close</button>
  </div>
  <div id="convo"></div>
  <div id="compose">
    <button id="jump" type="button" hidden aria-label="new messages below, jump to the newest">&#8595; new</button>
    <textarea id="task" placeholder="Let's move." aria-label="message to 7"></textarea>
    <button id="send" class="send" type="button" onclick="go()">Send</button>
  </div>
</div>
<div id="say" class="sr-only" aria-live="polite"></div>
<div id="canvas">
  <div id="paused" role="status" aria-live="polite">CIRO is paused for maintenance. Please check back shortly.</div>
  <section id="plate" aria-label="your plate" hidden>
    <div class="p-head"><h2 class="p-eyebrow" id="p-eyebrow">next on your plate</h2><button type="button" class="p-zoom" id="p-zoom"></button></div>
    <div class="p-last" id="p-last" aria-live="polite"></div>
    <div id="p-body"></div>
  </section>
  <div class="canvas-head">
    <div class="canvas-title">the creation so far</div>
    <div class="canvas-sub">significance firms, ephemera flows</div>
  </div>
  <div id="creation"><div class="reception"><svg class="sunrose" viewBox="0 0 400 400" role="img" aria-label="a sun composed of ceramic mosaic fragments in six concentric rings — 28, 23, 13, 7, 5, 3 — with gold grouting on the 23-ring and a small almond-shaped black stone at the center" xmlns="http://www.w3.org/2000/svg"><path class="ms-t" d="M351.44 205.74 L345.19 238.88 L387.32 250.16 L394.00 207.35 Z" fill="var(--sun-green)" stroke="var(--ground)" stroke-width="0.91" stroke-linejoin="round"/><path class="ms-t" d="M344.98 239.21 L333.27 271.36 L369.59 290.81 L388.27 250.92 Z" fill="var(--sun-terra)" stroke="var(--ground)" stroke-width="0.72" stroke-linejoin="round"/><path class="ms-t" d="M333.44 271.83 L313.85 298.60 L346.32 326.72 L368.84 290.89 Z" fill="var(--sun-orange)" stroke="var(--ground)" stroke-width="1.02" stroke-linejoin="round"/><circle class="ms-ac" cx="341.04" cy="296.69" r="1.22" fill="var(--sun-orange-2)" opacity="0.55"/><path class="ms-t" d="M314.02 297.50 L288.89 319.22 L314.72 353.86 L346.86 325.59 Z" fill="var(--sun-cobalt)" stroke="var(--ground)" stroke-width="0.87" stroke-linejoin="round"/><path class="ms-t" d="M287.69 320.13 L260.15 336.20 L276.71 373.70 L312.31 353.86 Z" fill="var(--sun-yellow)" stroke="var(--ground)" stroke-width="0.76" stroke-linejoin="round"/><path class="ms-t" d="M258.93 335.84 L227.06 345.54 L234.92 387.79 L275.64 374.35 Z" fill="var(--sun-blue)" stroke="var(--ground)" stroke-width="0.75" stroke-linejoin="round"/><circle class="ms-ac" cx="249.98" cy="363.53" r="1.65" fill="var(--sun-blue-2)" opacity="0.55"/><path class="ms-t" d="M226.76 346.86 L193.42 348.04 L191.53 390.59 L211.70 392.27 L234.12 387.24 Z" fill="var(--sun-green)" stroke="var(--ground)" stroke-width="1.01" stroke-linejoin="round"/><path class="ms-t" d="M192.99 347.84 L161.32 344.01 L150.44 384.49 L190.96 390.73 Z" fill="var(--sun-terra)" stroke="var(--ground)" stroke-width="0.82" stroke-linejoin="round"/><path class="ms-t" d="M161.57 342.48 L130.16 331.69 L110.25 369.22 L129.32 379.73 L150.22 384.54 Z" fill="var(--sun-orange)" stroke="var(--ground)" stroke-width="0.71" stroke-linejoin="round"/><path class="ms-t" d="M130.53 331.28 L101.76 314.57 L74.40 346.48 L110.38 369.35 Z" fill="var(--sun-cobalt)" stroke="var(--ground)" stroke-width="0.79" stroke-linejoin="round"/><circle class="ms-ac" cx="103.31" cy="341.04" r="1.34" fill="var(--sun-cobalt-2)" opacity="0.55"/><path class="ms-t" d="M101.26 312.53 L78.46 290.35 L46.61 314.02 L73.36 344.32 Z" fill="var(--sun-yellow)" stroke="var(--ground)" stroke-width="0.89" stroke-linejoin="round"/><circle class="ms-ac" cx="74.35" cy="315.98" r="1.35" fill="var(--sun-yellow-2)" opacity="0.55"/><path class="ms-t" d="M78.90 289.58 L62.14 258.71 L20.86 276.29 L44.82 314.79 Z" fill="var(--sun-blue)" stroke="var(--ground)" stroke-width="0.93" stroke-linejoin="round"/><path class="ms-t" d="M60.70 261.24 L51.32 227.30 L9.60 234.97 L23.00 277.82 Z" fill="var(--sun-green)" stroke="var(--ground)" stroke-width="1.09" stroke-linejoin="round"/><path class="ms-t" d="M51.74 228.80 L49.80 193.57 L5.86 191.69 L8.71 237.15 Z" fill="var(--sun-terra)" stroke="var(--ground)" stroke-width="0.84" stroke-linejoin="round"/><path class="ms-t" d="M47.50 194.82 L54.64 161.43 L11.91 150.08 L4.91 193.37 Z" fill="var(--sun-orange)" stroke="var(--ground)" stroke-width="1.08" stroke-linejoin="round"/><path class="ms-t" d="M55.16 160.39 L65.72 128.90 L30.11 110.05 L11.81 148.53 Z" fill="var(--sun-cobalt)" stroke="var(--ground)" stroke-width="0.71" stroke-linejoin="round"/><path class="ms-t" d="M66.60 127.83 L86.01 101.19 L55.60 74.83 L40.91 92.22 L30.83 108.48 Z" fill="var(--sun-yellow)" stroke="var(--ground)" stroke-width="1.04" stroke-linejoin="round"/><path class="ms-t" d="M85.79 101.93 L111.96 78.22 L87.45 44.32 L55.23 75.69 Z" fill="var(--sun-blue)" stroke="var(--ground)" stroke-width="0.95" stroke-linejoin="round"/><path class="ms-t" d="M111.77 78.79 L141.73 63.36 L125.41 25.08 L106.08 32.03 L87.34 45.23 Z" fill="var(--sun-green)" stroke="var(--ground)" stroke-width="0.96" stroke-linejoin="round"/><path class="ms-t" d="M141.63 63.83 L172.74 53.76 L165.34 14.04 L124.32 23.46 Z" fill="var(--sun-terra)" stroke="var(--ground)" stroke-width="0.76" stroke-linejoin="round"/><path class="ms-t" d="M172.75 54.20 L205.10 50.89 L206.49 10.16 L183.53 8.18 L164.96 12.55 Z" fill="var(--sun-orange)" stroke="var(--ground)" stroke-width="0.85" stroke-linejoin="round"/><circle class="ms-ac" cx="187.66" cy="29.45" r="1.67" fill="var(--sun-orange-2)" opacity="0.55"/><path class="ms-t" d="M206.91 52.26 L239.78 55.88 L250.38 17.46 L208.88 10.16 Z" fill="var(--sun-cobalt)" stroke="var(--ground)" stroke-width="0.80" stroke-linejoin="round"/><circle class="ms-ac" cx="225.93" cy="30.98" r="1.35" fill="var(--sun-cobalt-2)" opacity="0.55"/><path class="ms-t" d="M239.37 56.00 L270.07 68.32 L289.36 32.08 L273.44 21.95 L249.89 17.54 Z" fill="var(--sun-yellow)" stroke="var(--ground)" stroke-width="1.00" stroke-linejoin="round"/><path class="ms-t" d="M270.15 68.02 L299.08 86.90 L325.95 56.23 L289.63 31.39 Z" fill="var(--sun-blue)" stroke="var(--ground)" stroke-width="0.95" stroke-linejoin="round"/><path class="ms-t" d="M298.43 86.34 L321.60 111.56 L355.32 87.04 L325.79 54.74 Z" fill="var(--sun-green)" stroke="var(--ground)" stroke-width="1.00" stroke-linejoin="round"/><path class="ms-t" d="M320.37 110.42 L339.41 139.36 L377.49 122.80 L355.11 84.56 Z" fill="var(--sun-terra)" stroke="var(--ground)" stroke-width="0.83" stroke-linejoin="round"/><path class="ms-t" d="M338.75 139.44 L348.66 173.40 L390.19 165.97 L384.57 143.43 L376.07 123.15 Z" fill="var(--sun-orange)" stroke="var(--ground)" stroke-width="0.96" stroke-linejoin="round"/><path class="ms-t" d="M349.93 172.15 L350.14 206.70 L394.54 208.68 L392.25 186.37 L391.80 164.38 Z" fill="var(--sun-cobalt)" stroke="var(--ground)" stroke-width="1.08" stroke-linejoin="round"/><path class="ms-g" d="M314.98 207.70 L310.42 238.60 L344.78 250.60 L353.96 210.32 Z" fill="var(--sun-green)" stroke="var(--sun-gold)" stroke-width="1.43" stroke-linejoin="round"/><path class="ms-g" d="M309.45 239.11 L293.90 266.51 L323.49 287.46 L343.15 251.15 Z" fill="var(--sun-terra)" stroke="var(--sun-gold)" stroke-width="1.68" stroke-linejoin="round"/><path class="ms-g" d="M293.25 266.99 L271.95 288.55 L293.93 315.60 L322.86 288.25 Z" fill="var(--sun-orange)" stroke="var(--sun-gold)" stroke-width="1.61" stroke-linejoin="round"/><path class="ms-g" d="M271.54 289.20 L245.33 303.10 L259.34 334.97 L293.94 317.14 Z" fill="var(--sun-cobalt)" stroke="var(--sun-gold)" stroke-width="1.74" stroke-linejoin="round"/><path class="ms-g" d="M244.98 304.04 L214.62 310.60 L219.38 346.66 L258.31 334.87 Z" fill="var(--sun-yellow)" stroke="var(--sun-gold)" stroke-width="1.75" stroke-linejoin="round"/><path class="ms-g" d="M215.80 311.64 L184.43 310.58 L179.30 347.03 L198.58 350.97 L220.42 344.31 Z" fill="var(--sun-blue)" stroke="var(--sun-gold)" stroke-width="1.76" stroke-linejoin="round"/><circle class="ms-ac" cx="199.78" cy="332.00" r="1.33" fill="var(--sun-blue-2)" opacity="0.55"/><path class="ms-g" d="M183.67 311.73 L155.31 303.56 L140.82 337.14 L178.88 344.47 Z" fill="var(--sun-green)" stroke="var(--sun-gold)" stroke-width="1.79" stroke-linejoin="round"/><path class="ms-g" d="M156.00 302.78 L128.56 287.43 L105.83 315.24 L123.03 329.90 L141.75 336.06 Z" fill="var(--sun-terra)" stroke="var(--sun-gold)" stroke-width="1.49" stroke-linejoin="round"/><path class="ms-g" d="M128.79 288.32 L107.45 266.02 L77.38 287.46 L106.44 316.03 Z" fill="var(--sun-orange)" stroke="var(--sun-gold)" stroke-width="1.58" stroke-linejoin="round"/><path class="ms-g" d="M105.40 266.78 L91.45 239.16 L56.97 251.61 L66.61 270.79 L75.44 287.93 Z" fill="var(--sun-cobalt)" stroke="var(--sun-gold)" stroke-width="1.77" stroke-linejoin="round"/><path class="ms-g" d="M90.20 239.46 L84.28 207.19 L46.77 209.52 L52.93 232.60 L56.47 251.58 Z" fill="var(--sun-yellow)" stroke="var(--sun-gold)" stroke-width="1.75" stroke-linejoin="round"/><path class="ms-g" d="M83.88 207.49 L86.69 176.22 L50.72 168.67 L49.49 190.37 L47.75 209.81 Z" fill="var(--sun-blue)" stroke="var(--sun-gold)" stroke-width="1.62" stroke-linejoin="round"/><path class="ms-g" d="M86.98 176.98 L97.45 146.71 L64.28 129.47 L49.87 169.42 Z" fill="var(--sun-green)" stroke="var(--sun-gold)" stroke-width="1.48" stroke-linejoin="round"/><path class="ms-g" d="M98.88 146.75 L117.49 121.76 L91.31 96.93 L76.86 113.59 L65.30 129.07 Z" fill="var(--sun-terra)" stroke="var(--sun-gold)" stroke-width="1.43" stroke-linejoin="round"/><path class="ms-g" d="M116.25 122.66 L140.50 103.27 L122.84 74.56 L103.51 83.77 L89.30 97.77 Z" fill="var(--sun-orange)" stroke="var(--sun-gold)" stroke-width="1.57" stroke-linejoin="round"/><path class="ms-g" d="M142.07 102.97 L169.64 92.93 L160.01 58.96 L123.23 71.41 Z" fill="var(--sun-cobalt)" stroke="var(--sun-gold)" stroke-width="1.61" stroke-linejoin="round"/><path class="ms-g" d="M170.41 91.70 L200.26 87.50 L200.34 54.14 L161.08 57.52 Z" fill="var(--sun-yellow)" stroke="var(--sun-gold)" stroke-width="1.64" stroke-linejoin="round"/><path class="ms-g" d="M200.94 87.36 L230.52 91.06 L240.16 56.64 L201.21 54.41 Z" fill="var(--sun-blue)" stroke="var(--sun-gold)" stroke-width="1.76" stroke-linejoin="round"/><circle class="ms-ac" cx="218.20" cy="69.26" r="1.73" fill="var(--sun-blue-2)" opacity="0.55"/><path class="ms-g" d="M231.36 91.64 L259.19 103.98 L278.05 73.37 L240.65 59.53 Z" fill="var(--sun-green)" stroke="var(--sun-gold)" stroke-width="1.73" stroke-linejoin="round"/><path class="ms-g" d="M259.05 103.28 L283.04 123.16 L311.08 97.22 L277.72 72.71 Z" fill="var(--sun-terra)" stroke="var(--sun-gold)" stroke-width="1.66" stroke-linejoin="round"/><path class="ms-g" d="M283.70 121.59 L301.49 146.83 L333.55 130.03 L310.69 96.31 Z" fill="var(--sun-orange)" stroke="var(--sun-gold)" stroke-width="1.69" stroke-linejoin="round"/><path class="ms-g" d="M302.01 147.45 L312.50 176.29 L350.13 168.36 L334.28 130.82 Z" fill="var(--sun-cobalt)" stroke="var(--sun-gold)" stroke-width="1.51" stroke-linejoin="round"/><path class="ms-g" d="M313.81 176.60 L316.90 207.84 L353.35 210.28 L350.51 169.06 Z" fill="var(--sun-yellow)" stroke="var(--sun-gold)" stroke-width="1.62" stroke-linejoin="round"/><path class="ms-t" d="M279.88 208.53 Q275.71 227.78 266.16 245.19 L296.56 265.96 Q310.48 240.54 317.59 212.56 Z" fill="var(--sun-green)" stroke="var(--ground)" stroke-width="0.85" stroke-linejoin="round"/><path class="ms-t" d="M264.53 243.53 Q252.25 257.71 235.47 266.78 L252.14 298.17 Q276.89 284.92 294.79 263.95 Z" fill="var(--sun-terra)" stroke="var(--ground)" stroke-width="0.71" stroke-linejoin="round"/><circle class="ms-ac" cx="264.43" cy="271.16" r="1.51" fill="var(--sun-terra-2)" opacity="0.55"/><path class="ms-t" d="M235.66 267.08 Q218.93 273.37 200.96 276.08 L201.39 309.97 Q227.67 307.25 251.88 297.60 Z" fill="var(--sun-orange)" stroke="var(--ground)" stroke-width="0.81" stroke-linejoin="round"/><path class="ms-t" d="M201.30 275.62 Q182.58 274.13 165.89 267.22 L149.54 299.43 Q174.75 307.44 201.89 310.23 Z" fill="var(--sun-cobalt)" stroke="var(--ground)" stroke-width="0.96" stroke-linejoin="round"/><path class="ms-t" d="M164.95 267.93 Q148.97 258.87 136.22 244.40 L105.01 266.12 Q125.97 285.40 149.36 298.13 Z" fill="var(--sun-yellow)" stroke="var(--ground)" stroke-width="0.91" stroke-linejoin="round"/><path class="ms-t" d="M136.55 245.29 Q125.08 229.35 119.00 210.61 L83.77 215.23 Q91.53 242.49 105.38 267.53 Z" fill="var(--sun-blue)" stroke="var(--ground)" stroke-width="0.74" stroke-linejoin="round"/><path class="ms-t" d="M119.62 210.56 Q119.48 191.11 125.78 172.82 L89.54 159.55 Q82.53 187.03 84.68 215.15 Z" fill="var(--sun-green)" stroke="var(--ground)" stroke-width="0.97" stroke-linejoin="round"/><path class="ms-t" d="M124.75 172.79 Q134.31 155.70 147.76 141.78 L124.99 116.41 Q103.76 135.10 88.67 159.74 Z" fill="var(--sun-terra)" stroke="var(--ground)" stroke-width="0.89" stroke-linejoin="round"/><path class="ms-t" d="M147.25 142.10 Q163.58 132.40 181.76 127.67 L172.86 92.36 Q146.58 100.84 122.79 115.26 Z" fill="var(--sun-orange)" stroke="var(--ground)" stroke-width="1.00" stroke-linejoin="round"/><path class="ms-t" d="M180.54 126.32 Q199.19 124.86 217.69 126.78 L226.11 91.95 Q198.81 90.03 171.42 91.78 Z" fill="var(--sun-cobalt)" stroke="var(--ground)" stroke-width="1.00" stroke-linejoin="round"/><path class="ms-t" d="M217.89 126.02 Q234.85 131.82 251.29 140.28 L274.03 113.80 Q250.73 100.75 226.03 92.36 Z" fill="var(--sun-yellow)" stroke="var(--ground)" stroke-width="1.00" stroke-linejoin="round"/><path class="ms-t" d="M251.96 140.89 Q264.83 154.21 273.57 171.76 L308.75 158.26 Q294.80 133.04 275.79 113.79 Z" fill="var(--sun-blue)" stroke="var(--ground)" stroke-width="1.08" stroke-linejoin="round"/><path class="ms-t" d="M274.83 170.67 Q279.90 189.42 280.89 208.86 L316.81 212.80 Q316.95 184.51 308.24 157.57 Z" fill="var(--sun-green)" stroke="var(--ground)" stroke-width="0.96" stroke-linejoin="round"/><circle class="ms-ac" cx="295.17" cy="187.40" r="1.26" fill="var(--sun-green-2)" opacity="0.55"/><path class="ms-t" d="M254.98 199.78 Q248.34 223.28 232.50 241.08 L247.13 259.57 Q272.32 234.83 281.26 199.67 Z" fill="var(--sun-green)" stroke="var(--ground)" stroke-width="0.96" stroke-linejoin="round"/><path class="ms-t" d="M231.81 239.91 Q211.08 248.55 189.04 248.71 L183.36 273.93 Q216.62 272.83 248.71 261.11 Z" fill="var(--sun-terra)" stroke="var(--ground)" stroke-width="0.84" stroke-linejoin="round"/><path class="ms-t" d="M189.41 247.94 Q167.79 240.39 151.06 223.74 L126.85 235.48 Q151.78 260.47 184.16 271.66 Z" fill="var(--sun-orange)" stroke="var(--ground)" stroke-width="0.80" stroke-linejoin="round"/><path class="ms-t" d="M151.51 223.77 Q144.81 200.00 151.01 176.80 L126.36 165.13 Q117.93 200.00 128.13 235.22 Z" fill="var(--sun-cobalt)" stroke="var(--ground)" stroke-width="0.96" stroke-linejoin="round"/><circle class="ms-ac" cx="135.00" cy="200.00" r="1.82" fill="var(--sun-cobalt-2)" opacity="0.55"/><path class="ms-t" d="M151.49 176.38 Q167.94 159.79 188.85 150.78 L183.37 126.61 Q151.57 139.27 128.04 164.96 Z" fill="var(--sun-yellow)" stroke="var(--ground)" stroke-width="0.95" stroke-linejoin="round"/><path class="ms-t" d="M188.99 150.95 Q211.09 151.40 232.39 159.10 L247.84 139.60 Q216.54 127.51 183.60 126.93 Z" fill="var(--sun-blue)" stroke="var(--ground)" stroke-width="1.01" stroke-linejoin="round"/><path class="ms-t" d="M231.69 159.87 Q248.90 176.45 254.62 200.15 L281.62 200.22 Q273.05 164.82 248.50 138.57 Z" fill="var(--sun-green)" stroke="var(--ground)" stroke-width="0.85" stroke-linejoin="round"/><circle class="ms-ac" cx="258.56" cy="171.80" r="1.77" fill="var(--sun-green-2)" opacity="0.55"/><path class="ms-t" d="M231.59 205.37 Q220.83 221.37 203.93 228.57 L206.58 247.80 Q236.37 237.32 254.72 209.29 Z" fill="var(--sun-yellow)" stroke="var(--ground)" stroke-width="0.89" stroke-linejoin="round"/><circle class="ms-ac" cx="228.61" cy="229.36" r="1.77" fill="var(--sun-yellow-2)" opacity="0.55"/><path class="ms-t" d="M204.11 228.27 Q186.48 225.70 172.69 213.26 L150.62 223.98 Q176.93 243.87 207.04 248.39 Z" fill="var(--sun-blue)" stroke="var(--ground)" stroke-width="1.03" stroke-linejoin="round"/><path class="ms-t" d="M171.37 213.89 Q167.69 194.45 178.34 177.70 L163.47 162.37 Q144.70 190.51 151.24 223.66 Z" fill="var(--sun-green)" stroke="var(--ground)" stroke-width="0.97" stroke-linejoin="round"/><path class="ms-t" d="M178.70 178.06 Q196.02 172.56 213.31 174.97 L223.61 155.59 Q192.98 151.65 163.05 161.94 Z" fill="var(--sun-terra)" stroke="var(--ground)" stroke-width="1.01" stroke-linejoin="round"/><path class="ms-t" d="M213.29 174.98 Q228.48 185.95 231.46 205.51 L256.11 209.82 Q249.55 175.56 223.10 156.53 Z" fill="var(--sun-orange)" stroke="var(--ground)" stroke-width="0.83" stroke-linejoin="round"/><path class="ms-t" d="M215.23 204.56 Q203.03 212.72 190.05 209.50 L177.52 221.47 Q206.43 227.01 230.88 209.24 Z" fill="var(--sun-green)" stroke="var(--ground)" stroke-width="1.03" stroke-linejoin="round"/><path class="ms-t" d="M190.49 209.01 Q185.66 195.72 196.89 187.13 L193.69 173.88 Q169.14 190.79 177.84 220.99 Z" fill="var(--sun-terra)" stroke="var(--ground)" stroke-width="0.74" stroke-linejoin="round"/><path class="ms-t" d="M196.97 187.26 Q210.66 189.93 214.26 204.34 L231.33 209.54 Q222.50 178.73 193.64 173.21 Z" fill="var(--sun-orange)" stroke="var(--ground)" stroke-width="0.94" stroke-linejoin="round"/><path class="ms-pupil" d="M200.00 186.52 Q200.00 186.52 204.82 188.29 Q209.63 190.05 212.39 193.87 Q215.16 197.68 213.72 201.91 Q212.28 206.13 208.89 209.61 Q205.49 213.10 200.30 212.36 Q195.11 211.63 190.75 209.23 Q186.40 206.82 185.38 202.21 Q184.37 197.61 187.05 193.49 Q189.73 189.37 194.87 187.95 Z" fill="var(--sun-black)" stroke="var(--ground)" stroke-width="0.9" stroke-linejoin="round"/></svg>
    <p class="r-open">Your, Here.</p>
    <p class="r-build">To Build.</p>
    <p class="r-offer">Let&rsquo;s lighten your load.</p>
    <p class="r-prompt">What do you need more of right now?</p>
    <div class="r-chips">
      <button type="button" class="r-chip" onclick="chip('Time')">Time</button>
      <button type="button" class="r-chip" onclick="chip('Clarity')">Clarity</button>
      <button type="button" class="r-chip" onclick="chip('A decision')">A decision</button>
      <button type="button" class="r-chip" onclick="chip('Momentum')">Momentum</button>
      <button type="button" class="r-chip" onclick="chip('Courage')">Courage</button>
    </div>
    <p class="r-why"><a href="/why">Why building generative technology in 2026 should cost nothing</a></p>
  </div></div>
</div>
<script>
const $=id=>document.getElementById(id);
// Storage can throw (private window, blocked site data). Every read/write is guarded so the page
// still works without it — it just forgets between loads.
function sget(k){try{return localStorage.getItem(k)}catch(e){return null}}
function sset(k,v){try{localStorage.setItem(k,v)}catch(e){}}
function sdel(k){try{localStorage.removeItem(k)}catch(e){}}
const tok=$('tok'), model=$('model'), task=$('task'), convo=$('convo'), jump=$('jump'), send=$('send');
tok.value=sget('t7')||''; model.value=sget('m7')||'cathedral';
let CID=sget('c7')||null;
// AUTHED is the "cookie-token works" flag. It is set true after a successful /signup, and
// after loadPlate() returns 200 on a cold boot (returning visitor whose HttpOnly cookie
// authorized them). JS can't read the HttpOnly cookie itself, so the flag is how the UI
// knows a bearer-less fetch will reach the authed surface. Default false: the first-visit
// arriver sees signup and the reception, not the invite-paste path.
let AUTHED=false;
tok.addEventListener('change',()=>sset('t7',tok.value));
model.addEventListener('change',()=>sset('m7',model.value));
// First encounter: do NOT auto-open the token wall. The hero (reception) carries the room; cfg
// opens on Send-without-token (see go()) or on the gear. Reception first, gate second.
function cfg(){const c=$('cfg');c.style.display=c.style.display==='none'?'block':'none'}
// --- signup: the arriver's first door ---------------------------------------------------------
// Reveals the signup panel (osignup) or closes it (dsignup). A chip tap seeds the compose and
// reveals signup only when the arriver is NOT already authed; a repeat visit shouldn't ambush a
// returning person with a signup card they already crossed. signup() POSTs the email; on 200 the
// HttpOnly cookie is set by the server and we reload — the server authorizes via cookie, and the
// AUTHED probe below sets the UI accordingly on the next boot. Nothing is pasted; nothing is kept
// in JS. On 400 we show the per-field error inline; on 503 (incident pause) we say so.
function osignup(){
  $('signup').style.display='block';
  // Prefer the invite field — it's the first thing the arriver fills, and screen-readers
  // announce the dialog heading via aria-labelledby when focus lands inside it.
  const first=$('su-invite')||$('su-email');
  if(first)setTimeout(()=>first.focus(),50);
  const say=$('say');if(say)say.textContent='signup opened — invite required';
}
function dsignup(){$('signup').style.display='none';const er=$('su-err');if(er){er.style.display='none';er.textContent=''}}
function sherr(msg){const er=$('su-err');if(!er)return;er.textContent=msg;er.style.display='block'}
async function signup(){
  const inv=($('su-invite').value||'').trim();
  const em=($('su-email').value||'').trim();
  const dr=($('su-role').value||'').trim();
  if(!inv){sherr('an invite code is needed to sign up');$('su-invite').focus();return}
  if(!em||em.indexOf('@')<1||em.indexOf('@')===em.length-1){sherr('that email looks off');$('su-email').focus();return}
  const btn=$('su-go');if(btn){btn.disabled=true;btn.textContent='signing up…'}
  try{
    const r=await fetch('/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:em,declared_role:dr,invite_code:inv})});
    if(r.status===503){sherr('CIRO is paused for maintenance — try again shortly.');return}
    if(r.status===400){sherr('that email looks off');return}
    if(r.status===403){sherr('this space opens by invite — that code did not match a live one.');$('su-invite').focus();return}
    if(r.status===409){sherr('that email is already signed up. sign-in by link is being wired — for now, reach Rico.');return}
    if(!r.ok){sherr('signup did not go through — try again.');return}
    AUTHED=true;
    // The HttpOnly cookie is set; reload so the authed surface renders from a clean boot
    // (loadPlate, draft restore, convo state) rather than patching a half-authed in-page view.
    location.reload();
  }catch(e){sherr('signup did not go through — try again.')}
  finally{if(btn){btn.disabled=false;btn.textContent='Sign up'}}
}
// A chip tap seeds the compose with "<name> — " and either opens signup (anon) or focuses the
// textarea (authed). The user still writes the ask and presses enter — the chip never composes
// a reply on their behalf (II arc-altitude: human composes the sequence).
function chip(name){
  task.value=(name||'')+' — ';
  // Programmatic .value doesn't fire 'input' — persist the draft by hand so the seed survives
  // the signup → reload round-trip (otherwise the arriver signs up and the chip's seed is gone).
  sset(dkey(),task.value);
  grow();sync();task.focus();
  try{task.setSelectionRange(task.value.length,task.value.length)}catch(e){}
  if(!tok.value&&!AUTHED)osignup();
}
function H(){return {'Authorization':'Bearer '+tok.value,'Content-Type':'application/json'}}
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function isSvg(s){return /^\s*<svg[\s>]/i.test(s||'')}
function renderAtom(content){if(isSvg(content)){return '<div class="svg-wrap">'+content+'</div>'}return esc(content)}
function offerBody(p){
  p=p||{};
  if(p.action_type==='write_note'){const t=seven((p.params&&p.params.text)||'');return blank(t)?'':esc(t)}
  return blank(p.projection)?'':esc(p.projection)
}
const INVISIBLE=/[\u200B-\u200F\u2060-\u2064\u206A-\u206F\u202A-\u202E\u2066-\u2069\uFEFF\u00AD\u00A0\u061C\u180E\u0000]/g;
function blank(s){return !String(s==null?'':s).replace(INVISIBLE,'').trim()}
function det(o){return '<details><summary>details</summary><pre>'+esc(JSON.stringify(o,null,1))+'</pre></details>'}
// Reply text sits in .txt (copy reads it; links are built over it as DOM nodes). The reader's own
// bubbles carry no copy control.
const COPY='<div class="acts"><button type="button" class="copy" aria-label="copy this reply">copy</button></div>';
function bubble(who,text,cls){const you=/(^|\s)you(\s|$)/.test(cls||'');return '<div class="bubble '+(cls||'')+'"><div class="who">'+esc(who)+'</div>'+(you?esc(text):'<span class="txt">'+esc(text)+'</span>'+COPY)+'</div>'}
// The standing attendant — the autonomous door-keeper that answers while no one is live. Rendered
// distinct from the present-seat "7" so the reader always knows WHO is on the other side. The
// caller decides this is an attendant answer from the audit log (status.answered_by / a turn's
// answered_by), NEVER by reading the text. A non-'answered' outcome means the attendant came to
// the door but could not finish — shown as a could-not-complete note, not a confident reply.
function attendantBubble(text,outcome){
  const failed = !!(outcome && outcome!=='answered');
  const note = failed
    ? 'autonomous · no one was live · this turn did not complete'
    : 'autonomous · authority-reduced · no one is live';
  return '<div class="bubble att'+(failed?' att-failed':'')+'">'
    +'<div class="who att-who">standing attendant</div>'
    +'<div class="att-note">'+esc(note)+'</div>'
    +'<span class="txt">'+esc(text)+'</span>'+COPY+'</div>';
}
// Positive health line for the door (invariant I / 006): the raw heartbeat age so a bare armed
// light cannot hide a slowing beat, plus the attendant's last real answer-path outcome so a
// subtly-broken-but-beating daemon shows through. Empty when there is nothing honest to show.
function doorHealth(d){
  d=d||{};const bits=[];
  if(typeof d.heartbeat_age==='number') bits.push('door beat '+Math.round(d.heartbeat_age)+'s ago');
  const al=d.attendant_last;
  if(al&&al.outcome){
    bits.push(al.outcome==='answered'
      ? 'standing attendant · last answer OK'
      : 'standing attendant · last answer did not complete ('+al.outcome+')');
  }
  return bits.length?'<div class="muted dhealth">'+esc(bits.join('  ·  '))+'</div>':'';
}
// Three-state door read for the dock header (010 rendering contract).
// The custody-line is a ROOM CONDITION, not a chat bubble. It persists through daemon restarts
// (succession keeps it continuous) and renders as one of:
//   closed  : "the door is closed"  — the daemon is not running.
//   empty   : "standing by · not holding custody" — alive but no active grant, or envelope-mismatch.
//   working : "the door is being kept · <steward-role>" — alive + active custody + envelope matches.
// Steady, never pulsing — the custody line renders still; no pulse anywhere in the console.
function doorCondition(d){
  d=d||{};
  const cs=d.custody_status;
  if(!d.armed) return {state:'closed', line:'the door is closed', detail:null, custody:null};
  if(!cs || cs.state==='empty'){
    const reason = cs && cs.reason==='envelope-mismatch'
      ? 'envelope-mismatch · the live launch does not match the signed grant'
      : 'not holding custody';
    return {state:'empty', line:'standing by · '+reason, detail:cs, custody:null};
  }
  // state === 'working'
  const role = cs.steward_role || 'door-steward';
  return {state:'working',
          line:'the door is being kept · '+role,
          detail:cs,
          custody:cs};
}
function doorConditionHtml(d){
  const c=doorCondition(d);
  const cls='dcond dcond-'+c.state;
  // Include the raw custody_status under det() for operator inspection; register snapshots
  // (pid/boot_ts/instance_id) stay one-tap-away under attendant_last / per-turn details.
  const extra = c.detail?det({custody_status:c.detail}):'';
  return '<div class="'+cls+'">'+esc(c.line)+extra+'</div>';
}
function clean(t){return (t||'').replace(/^\s*CC0*7\s*(\(\s*7\s*\))?\s*:\s*/i,'').replace(/^\s*7\s*:\s*/,'')}
function humanize(err){return ({no_parseable_intent:"I did not quite catch that as something I can do here. Tell me plainly what you would like.",unregistered:"That is outside what I can do here.",refused:"I cannot do that one."}[err])||"I did not have a move to offer just now — try saying it a different way."}
function decode(s){s=s||'';const t=document.createElement('textarea');for(let i=0;i<3;i++){const prev=s;t.innerHTML=s;s=t.value;if(s===prev)break;}return s}
function seven(t){return clean(decode(t))}
// --- reading while 7 writes ---------------------------------------------------------------------
// Follow the newest only when the reader is already at (or within NEAR px of) the bottom. Scrolled
// up, their place is held across every re-render and new bubble, and a "new" jump appears instead.
const NEAR=80;
function atBottom(){return convo.scrollHeight-convo.scrollTop-convo.clientHeight<=NEAR}
function toBottom(){convo.scrollTop=convo.scrollHeight;jump.hidden=true}
function change(fn,force){
  const stick=force||atBottom(), top=convo.scrollTop, h=convo.scrollHeight;
  const out=fn();
  if(stick) toBottom();
  else{convo.scrollTop=top; if(convo.scrollHeight>h) jump.hidden=false;}
  return out;
}
convo.addEventListener('scroll',()=>{if(atBottom())jump.hidden=true});
jump.addEventListener('click',e=>{toBottom();if(e.detail===0)task.focus({preventScroll:true})});
// Is any of the reader's selection inside el? Then a soft re-render waits for the next poll.
function selIn(el){try{const s=getSelection();return !!(s&&s.rangeCount&&!s.isCollapsed&&s.getRangeAt(0).intersectsNode(el))}catch(e){return false}}
function enhance(root){root.querySelectorAll('.txt').forEach(linkify)}
function addConvo(html,force){return change(()=>{const d=document.createElement('div');d.innerHTML=html;convo.appendChild(d);enhance(d);return d},force)}
// Replace a live bubble's content — skipped when nothing changed, so an unchanged 2s poll touches
// nothing (no scroll, no lost selection, no re-settle). soft: also wait while the reader is selecting in it.
function setLive(el,html,soft){
  if(el._html===html) return;
  if(soft&&selIn(el)) return;
  change(()=>{el.innerHTML=html;el._html=html;enhance(el)});
}
// --- links: http(s) only, built as DOM nodes over text nodes; reply text is never parsed as HTML ---
const URLRE=/\bhttps?:\/\/[^\s<>"'`]+/gi;
function linkify(el){
  if(el._linked) return; el._linked=1;
  const w=document.createTreeWalker(el,NodeFilter.SHOW_TEXT), nodes=[];
  while(w.nextNode()) nodes.push(w.currentNode);
  nodes.forEach(n=>{
    const s=n.nodeValue; let m, last=0, frag=null; URLRE.lastIndex=0;
    while((m=URLRE.exec(s))){
      let u=m[0];
      // trailing sentence punctuation is not part of the link; a closing paren is kept only when balanced
      while(/[.,;:!?\])}]$/.test(u)){
        if(u.endsWith(')')&&(u.split('(').length-1)>=(u.split(')').length-1)) break;
        u=u.slice(0,-1);
      }
      let href=null;
      try{const p=new URL(u);if(p.protocol==='http:'||p.protocol==='https:')href=p.href}catch(e){}
      if(!href) continue;
      frag=frag||document.createDocumentFragment();
      frag.appendChild(document.createTextNode(s.slice(last,m.index)));
      const a=document.createElement('a');
      a.href=href; a.textContent=u; a.rel='noopener noreferrer'; a.target='_blank';
      frag.appendChild(a);
      last=m.index+u.length; URLRE.lastIndex=last;
    }
    if(frag){frag.appendChild(document.createTextNode(s.slice(last)));n.parentNode.replaceChild(frag,n)}
  });
}
// --- copy a reply ---------------------------------------------------------------------------------
async function copyText(s){
  try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(s);return true}}catch(e){}
  try{
    const t=document.createElement('textarea');t.value=s;t.setAttribute('readonly','');
    t.style.position='fixed';t.style.top='0';t.style.opacity='0';document.body.appendChild(t);
    t.select();const ok=document.execCommand('copy');t.remove();return ok;
  }catch(e){return false}
}
convo.addEventListener('click',async e=>{
  const b=e.target.closest&&e.target.closest('button.copy'); if(!b) return;
  const t=b.closest('.bubble').querySelector('.txt');
  const ok=await copyText(t?t.textContent:''); b.focus({preventScroll:true});
  b.textContent=ok?'copied':'could not copy'; $('say').textContent=ok?'copied':'could not copy';
  clearTimeout(b._t); b._t=setTimeout(()=>{b.textContent='copy';$('say').textContent=''},1500);
});
// --- the compose box: draft kept per conversation, grows with the text, Send only when there is text ---
function dkey(){return 'draft7:'+(CID||'new')}
function grow(){change(()=>{task.style.height='auto';const max=parseFloat(getComputedStyle(task).maxHeight)||240;task.style.height=Math.min(task.scrollHeight+2,max)+'px'})}
function sync(){send.disabled=blank(task.value)}
function restoreDraft(){task.value=sget(dkey())||'';grow();sync()}
task.addEventListener('input',()=>{if(blank(task.value))sdel(dkey());else sset(dkey(),task.value);grow();sync()});
restoreDraft();
// The hero reception that greets a first encounter and returns on "start a fresh conversation".
// Captured ONCE from the server-rendered markup above (the sun-and-compass-rosette + one line),
// so there is a single source of truth and the figure can never drift between the two paths.
const RECEPTION_HTML=$('creation').innerHTML;
function renderCanvas(atoms){
  const el=$('creation');
  if(!atoms||!atoms.length){el.innerHTML=RECEPTION_HTML;el._html='';return}
  const groups={formed:[],firming:[],flowing:[]};
  atoms.forEach(a=>{(groups[a.significance]||groups.flowing).push(a)});
  let html='';
  if(groups.formed.length){
    html+='<div class="zone zone-formed"><div class="zone-label">formed</div>';
    groups.formed.forEach(a=>{html+='<div class="atom atom-formed">'+renderAtom(a.content)+'</div>'});
    html+='</div>';
  }
  if(groups.firming.length){
    html+='<div class="zone zone-firming"><div class="zone-label">firming</div>';
    groups.firming.forEach(a=>{html+='<div class="atom atom-firming"><span class="reading-tag">draft</span>'+renderAtom(a.content)+'</div>'});
    html+='</div>';
  }
  if(groups.firming.length){
    html+='<div class="canvas-hint">Say “put that on the canvas” or “remove that” to shape what’s here.</div>';
  }
  if(groups.flowing.length){
    html+='<div class="zone zone-flowing"><div class="zone-label">flowing</div>';
    groups.flowing.forEach(a=>{html+='<div class="atom atom-flowing">'+esc(a.content)+'</div>'});
    html+='</div>';
  }
  // Polls re-send the same canvas every 2s: leave the DOM alone when nothing changed, so the
  // reader's scroll and selection on the canvas survive.
  if(el._html===html) return;
  el.innerHTML=html; el._html=html;
}
let openOffer=null;
function recede(o){fetch('/run/'+o.rid+'/decline',{method:'POST',headers:H(),body:'{}'}).catch(()=>{});setLive(o.el,'<div class="bubble muted">left undone.</div>');}
async function load(){
  if(!CID||!tok.value) return;
  try{
    const r=await fetch('/thread/'+CID,{headers:H()});
    if(!r.ok){sdel('c7');CID=null;return}
    const t=await r.json();
    (t.turns||[]).forEach(tn=>{
      if(tn.who==='human'){if(!blank(tn.text))markPlate(addConvo(bubble('you',tn.text,'you')),tn.plate,tn.text);}
      else{const w=seven(tn.text);if(!blank(w)){
        // A turn carries answered_by only when the standing attendant authored it; rehydrate it as
        // the attendant, never the present seat — the who-identity must survive a reload.
        addConvo(tn.answered_by?attendantBubble(w,tn.answer_outcome):bubble('7',w));
      }}
    });
    renderCanvas(t.canvas||[]);
    syncUndos();
  }catch(e){}
}
load();
function fresh(){CID=null;sdel('c7');convo.innerHTML='';jump.hidden=true;renderCanvas([]);restoreDraft();task.focus()}
async function go(){
  // No bearer AND no cookie-auth signal yet → open the signup door (not the invite-paste
  // panel). A returning arriver whose HttpOnly cookie carries them has AUTHED=true from the
  // boot probe below; they skip this door. The pasteable cfg panel is still reachable via gear.
  //
  // But cold-boot loadPlate() can net-fail (offline, DNS blip, proxy hiccup) and leave AUTHED
  // false even for a returning authed user. Re-probe once before ambushing them with signup —
  // a merciful floor. On real network failure we say so instead of silently routing to signup.
  if(!tok.value&&!AUTHED){
    let probe;
    try{probe=await fetch('/plate',{headers:H()})}catch(e){
      addConvo('<div class="bubble err">connection lost — try again in a moment.</div>',true);
      return;
    }
    if(probe && probe.ok){AUTHED=true;}
    else{osignup();return;}
  }
  const msg=task.value; if(blank(msg))return;
  const key=dkey();
  task.value=''; grow(); sync(); task.focus(); if(tok.value)sset('t7',tok.value);
  if(openOffer){recede(openOffer);openOffer=null;}
  // Sending is the one moment the view always goes to the newest (the reader just spoke).
  const mine=addConvo(bubble('you',msg,'you'),true);
  const live=addConvo('<div class="bubble muted think">7 is thinking…</div>',true);
  try{
    const r=await fetch('/run',{method:'POST',headers:H(),body:JSON.stringify({task:msg,model:model.value,conversation_id:CID})});
    if(r.status===401){
      // Auth lapsed mid-session. If an invite-paste token was in use, re-open cfg so the ops
      // path can re-paste; otherwise the cookie expired/cleared, so send the arriver to signup.
      AUTHED=false;
      if(tok.value){$('cfg').style.display='block';setLive(live,'<div class="bubble err">that token did not work — check settings.</div>');}
      else{osignup();setLive(live,'<div class="bubble err">that session expired — sign up again.</div>');}
      unsend(msg);return;
    }
    const j=await r.json();
    if(r.ok) sdel(key);
    if(j.conversation_id){CID=j.conversation_id;sset('c7',CID)}
    if(j.plate) typedCommand(mine,j.plate);
    if(!blank(task.value)) sset(dkey(),task.value);
    poll(j.run_id, live);
  }catch(e){setLive(live,'<div class="bubble err">'+esc(''+e)+'</div>');unsend(msg)}
}
// A send that did not go through hands the words back to the box (if the reader has not typed since).
function unsend(msg){if(blank(task.value)){task.value=msg;grow();sync()}}
async function poll(rid, live){
  let s;
  try{ s=await (await fetch('/run/'+rid,{headers:H()})).json(); }
  catch(e){ setLive(live,'<div class="bubble err">'+esc(''+e)+'</div>'); return }
  if(s.canvas) renderCanvas(s.canvas);
  if(s.state==='said'){
    const words=seven(s.say);
    const body=blank(words)?humanize('no_parseable_intent'):words;
    // WHO answered decides the sender: a standing-attendant answer (s.answered_by set, from the
    // audit log) must not render as the present seat "7". This is the whole presence-reservation
    // point — once "home" can be a machine, the surface still tells the truth about who is there.
    setLive(live,s.answered_by?attendantBubble(body,s.answer_outcome):bubble('7',body));
  }else if(s.state==='awaiting_cathedral'){
    const d=s.door||{};
    const cond=doorCondition(d);
    let msg, cls='bubble muted', health='';
    if(d.stranded){
      // Prefer stranded even when armed: a taken-but-unanswered ask must never read as thought.
      msg='That ask was taken but never answered. Say it again — the door will re-serve it on the next arm.';
    }else if(cond.state==='working'){
      // Only "working" (active custody + envelope match) renders as thought in progress.
      msg='CIRO is working…'; cls='bubble muted think'; health=doorHealth(d);
    }else if(cond.state==='empty'){
      // Alive but holding no custody — honest-empty. NOT the same as closed; NOT the same as working.
      // The ask stays outstanding; a present seat or a future custody-grant will pick it up.
      msg='The door is standing by, but not holding custody for this ask right now. '
         +'A present session (a family seat or the operator at the terminal) will pick it up — '
         +'or a human can sign a custody-grant and the door will keep it from then on.';
      health=doorHealth(d);
    }else{
      // Closed — no daemon, no present seat.
      msg='At the door. No one is listening yet — arm it with:\n\nbash agnostic_runtime/trigger/cathedral_wait.sh';
      health=doorHealth(d);
    }
    // The 2s re-render: soft, so it never moves the reader or drops a selection they are making in it.
    setLive(live,'<div class="'+cls+'"><div class="who">7</div>'+esc(msg)+health+doorConditionHtml(d)+'</div>',true);
    if(!d.stranded) setTimeout(()=>poll(rid,live),2000);
  }else if(s.state==='awaiting_confirm'){
    const mid=s.pending.move_id;
    const body=offerBody(s.pending);
    if(!body){
      fetch('/run/'+rid+'/decline',{method:'POST',headers:H(),body:'{}'}).catch(()=>{});
      setLive(live,bubble('7',humanize('no_parseable_intent'))); return;
    }
    const pre=blank(seven(s.say))?'':bubble('7',seven(s.say));
    setLive(live,pre+
      '<div class="bubble pending"><div class="who">7 wants to do this — nothing happens until you allow it</div>'+body+
      '<div class="row"><button class="ap">Allow</button><button class="de sec">Not now</button></div></div>');
    live.querySelector('.ap').onclick=()=>act(rid,mid,'confirm',live);
    live.querySelector('.de').onclick=()=>act(rid,mid,'decline',live);
    openOffer={rid:rid,mid:mid,el:live};
  }else if(s.state==='done'){
    const t=s.result&&s.result.params&&s.result.params.text;
    if(s.result&&s.result.fired==='write_note'&&!blank(seven(t))) setLive(live,bubble('7',seven(t)));
    else setLive(live,'<div class="bubble"><div class="who">7</div>done.'+det(s.result)+'</div>');
  }else if(s.state==='declined'){
    setLive(live,'<div class="bubble muted">left it undone.</div>');
  }else{
    setLive(live,'<div class="bubble"><div class="who">7</div>'+esc(humanize(s.error))+det(s)+'</div>');
  }
}
async function act(rid,mid,kind,live){
  openOffer=null;
  setLive(live,'<div class="bubble muted think">…</div>');
  await fetch('/run/'+rid+'/'+kind,{method:'POST',headers:H(),body:kind==='confirm'?JSON.stringify({move_id:mid}):'{}'});
  poll(rid,live);
}
// --- the plate --------------------------------------------------------------------------------
// One "next" card (the server's pick, with its reason), or zoomed out: everything, grouped. All
// item text is set with textContent — nothing from the plate is ever parsed as HTML. A tap is
// shown as sent only when the server says the act was recorded.
let PLATE=null, ZOOM=false, PLATE_SIG='';
const WORD={1:'low',2:'medium',3:'high'};
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function estLine(it){
  const d=el('div','p-chips');
  d.appendChild(el('span','p-label',"CIRO's estimate:"));
  const e=it.est||{};
  [['pressure','pressure'],['cost','cost'],['generativity','generativity']].forEach(([k,n])=>{
    d.appendChild(el('span','p-chip',n+' '+(WORD[e[k]]||'?')));
  });
  return d;
}
function card(it,opts){
  opts=opts||{};
  const c=el('article','pcard'); c.dataset.id=it.id;
  if(it.num) c.appendChild(el('div','p-num',numLabel(it.num)));
  c.appendChild(el(opts.compact?'h4':'h3','p-title',it.title));
  if(!blank(it.why)) c.appendChild(el('p','p-why',it.why));
  if(it.act){
    c.appendChild(el('div','p-chose','You chose: '+it.act.choice+(it.act.ts?'  ·  '+it.act.ts.replace(/\.\d+Z$/,'Z').replace('T',' ').replace('Z',' UTC'):'')));
    const slot=el('div','pundo'); slot.dataset.state='live';
    undoInto(slot,it); c.appendChild(slot);
  }else if((it.choices||[]).length){
    const row=el('div','p-choices'), st=el('div','p-status');
    st.setAttribute('role','status');
    it.choices.forEach(ch=>{const b=el('button',null,ch);b.type='button';b.addEventListener('click',()=>tapChoice(it,ch,row,st));row.appendChild(b)});
    c.appendChild(row); c.appendChild(st);
  }
  const m=el('div','p-meta');
  m.appendChild(estLine(it));
  if(opts.reason) m.appendChild(el('div',null,'Why this one first: '+opts.reason));
  if((it.source||[]).length) m.appendChild(el('div',null,'From: '+it.source.join('  ·  ')));
  c.appendChild(m);
  return c;
}
// --- visual-safety rendering (Class A/B expansion §9.1 + Addendum A §A.3) ---------------------
// The daemon stages prepared-B items that may embed hostile payload text (crafted by a beta
// user with intent to inject). The surface renders every field with these guarantees:
//   (1) textContent only — no innerHTML of payload-derived strings. The esc() helper already
//       escapes &<>; prepared-B text goes through el() which uses textContent.
//   (2) ANSI / control / bidi-override characters are stripped before display. A buried
//       ‮ can flip legit-looking "safe.txt" into a right-to-left ".txt.exe" at render.
//   (3) No markdown execution. The diff body renders inside <pre> as raw text, never parsed.
//   (4) No auto-rendered URLs or images. Links are not materialized from payload strings;
//       the user sees the URL characters as text and must consciously copy.
//   (5) Length-capped display with "show more". Attacks hide in large diffs — a per-field
//       cap prevents a 32KB payload from pushing the approve button off-screen or burying
//       a hostile hunk. The user can expand explicitly; the content_hash is bound to the
//       FULL body, so hidden-tail attacks don't change what approve admits.
//   (6) Second-order (A.record-operator-apply stdout from a prior admit) renders with the SAME
//       stripping — stdout_tail is adversarial input even if it looks like a system log.
const _ANSI_CSI=/\x1B\[[0-?]*[ -\/]*[@-~]/g;    // CSI sequences incl color codes
const _ANSI_OSC=/\x1B\][^\x07]*(?:\x07|\x1B\\)/g; // OSC title/link sequences
const _CTRL=/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g;  // C0 control chars except \t \n
// C1 controls (U+0080-U+009F) — includes the 8-bit CSI at U+009B. Browser <pre textContent>
// renders them inert, but a downstream copy-paste into a terminal is a real adversary path.
// Strip at the surface so copied content stays clean.
const _C1=/[-]/g;
const _BIDI=/[‪-‮⁦-⁩‎‏؜]/g; // bidi-override + marks
const _ZW=/[​-‍⁠-⁤﻿᠎]/g;         // zero-width
// Variation selectors (U+FE00-U+FE0F) can swap a legit-looking character's glyph without
// changing the code point for the eye reading raw bytes elsewhere — a path / shape_id
// homoglyph attack vector downstream of this surface.
const _VS=/[︀-️]/g;
// Soft-hyphen (U+00AD) is in INVISIBLE for blank() but was missing from strip() — a title
// with embedded U+00AD could pass through unchanged into a copied path.
const _SHY=/­/g;
function strip(s){
  if(typeof s!=='string') return '';
  return s.replace(_ANSI_CSI,'').replace(_ANSI_OSC,'').replace(_CTRL,'').replace(_C1,'')
          .replace(_BIDI,'').replace(_ZW,'').replace(_VS,'').replace(_SHY,'');
}
// Length-caps for display. The content_hash binds to the FULL body, so an "expand to show more"
// is a UX affordance only — approve still commits what the server holds.
const DIFF_HEAD=4000;      // first-N chars shown by default on the staged diff
const STDOUT_HEAD=1200;    // first-N chars shown on prior-admit stdout
function strText(s,cap){
  s=strip(s||'');
  if(cap && s.length>cap) return {head:s.slice(0,cap),rest:s.slice(cap)};
  return {head:s,rest:''};
}
// Expiry countdown — soonest-first ranking already puts the clock-urgent at the top; the
// per-card label updates every 30s so a card that drifts into the grace band or past
// expiry re-labels without a full refresh. ±1h grace band renders as "needs re-approve"
// so the operator sees the stale state (and the server will refuse approve past strict expiry).
function expireLabel(iso){
  const t=Date.parse(iso); if(isNaN(t)) return {text:'expiry: unknown',cls:'',usable:false};
  const dt=t-Date.now();
  const m=Math.floor(Math.abs(dt)/60000);
  const h=Math.floor(m/60), mm=m%60;
  const span=h>0?(h+'h '+mm+'m'):(m+'m');
  if(dt<=-3600000) return {text:'expired · needs re-approve',cls:'stale',usable:false};
  if(dt<=0) return {text:'in grace band ('+span+' past) · will refuse',cls:'urgent',usable:false};
  if(dt<=15*60000) return {text:'expires in '+span,cls:'urgent',usable:true};
  return {text:'expires in '+span,cls:'',usable:true};
}
function prepCard(it,opts){
  opts=opts||{};
  const pb=it.prepared_b||{};
  const c=el('article','pcard p-await'); c.dataset.id=it.id; c.dataset.pb='1';
  if(it.num) c.appendChild(el('div','p-num',numLabel(it.num)));
  c.appendChild(el('div','pbshape',strip(pb.shape_id||'')));
  c.appendChild(el(opts.compact?'h4':'h3','p-title',strip(it.title||'')));
  if(pb.description) c.appendChild(el('p','pbdesc',strip(pb.description)));
  // Meta block — byte_count PROMINENT (pro-dev HP#1), requester_user_id opaque, content_hash
  // visible so the operator can cross-check against what the apply wrapper will admit.
  const meta=el('div','pbmeta');
  const addMeta=(k,v,big)=>{meta.appendChild(el('span','k',k));const sv=el('span','v'+(big?' big':''),v);meta.appendChild(sv)};
  addMeta('size',(pb.byte_count||0).toLocaleString()+' bytes',true);
  addMeta('requested by',strip(pb.requester_user_id||''));
  addMeta('content hash',strip(pb.content_hash||''));
  if(pb.unlock_only) addMeta('mode','UNLOCK_ONLY · encrypted at rest');
  c.appendChild(meta);
  // Files BEFORE diff body — a hostile payload with a legit-looking path cannot be shadowed
  // by burying the path inside a scrolling diff hunk the eye never reaches.
  const files=(pb.file_list||[]).map(p=>strip(p||''));
  const fx=el('div','pbfiles'); fx.appendChild(el('h4',null,'files ('+files.length+')'));
  const ul=el('ul');
  const SHOW=10;
  files.slice(0,SHOW).forEach(p=>ul.appendChild(el('li',null,p)));
  fx.appendChild(ul);
  if(files.length>SHOW) fx.appendChild(el('div','more','+ '+(files.length-SHOW)+' more file(s)'));
  c.appendChild(fx);
  // Diff body — collapsed by default, rendered as text inside <pre>. No syntax highlighting,
  // no markdown, no link materialization. Length-cap with "show more" expansion.
  if(typeof pb.diff_body==='string' && pb.diff_body.length){
    const d=document.createElement('details'); d.className='pbdiff';
    const sum=document.createElement('summary'); sum.textContent='staged diff ('+pb.diff_body.length.toLocaleString()+' chars)';
    d.appendChild(sum);
    const parts=strText(pb.diff_body,DIFF_HEAD);
    const pre=document.createElement('pre'); pre.textContent=parts.head;
    d.appendChild(pre);
    if(parts.rest){
      const cap=el('div','cap','… '+parts.rest.length.toLocaleString()+' more chars. The content hash binds to the full body.');
      const btn=document.createElement('button'); btn.type='button'; btn.className='sec';
      btn.textContent='show more';
      btn.style.cssText='width:auto;margin:6px 0 0;padding:4px 10px;font-size:12px';
      btn.addEventListener('click',()=>{pre.textContent=parts.head+parts.rest;cap.remove();btn.remove()});
      cap.appendChild(btn); d.appendChild(cap);
    }
    c.appendChild(d);
  }
  // Second-order: a prior A.record-operator-apply's stdout may reach the console. Same adversarial
  // rendering — strip, cap, textContent only. Rendered as a labeled block so the register is
  // obvious ("this is output from a prior run, not from the daemon speaking").
  if(typeof pb.stdout_tail==='string' && pb.stdout_tail.length){
    const parts=strText(pb.stdout_tail,STDOUT_HEAD);
    const box=el('div','pbstdout');
    box.appendChild(el('span','lbl','prior-admit stdout (sanitized)'));
    const pre=document.createElement('div'); pre.textContent=parts.head+(parts.rest?' …':'');
    box.appendChild(pre);
    c.appendChild(box);
  }
  // Revert path — every B has a named inverse (design Rule 2). Shown as text; a user can
  // read it before approving and know how to walk the admit back.
  if(pb.revert_path){
    const r=el('p','pbrevert'); r.appendChild(el('span',null,'revert: '));
    const code=document.createElement('code'); code.textContent=strip(pb.revert_path); r.appendChild(code);
    c.appendChild(r);
  }
  // Expiry countdown
  const exp=expireLabel(pb.expires_at||'');
  const ex=el('div','pbexp'+(exp.cls?' '+exp.cls:''),exp.text);
  c.appendChild(ex);
  c.dataset.exp=pb.expires_at||'';
  // Decision state OR action controls
  if(it.decision){
    const d=it.decision;
    const box=el('div','pbdecision'+(d.kind==='approved'?' approved':' rejected'));
    box.textContent=(d.kind==='approved'?'Approved':'Rejected')+(d.ts?' · '+d.ts.replace(/\.\d+Z$/,'Z').replace('T',' ').replace('Z',' UTC'):'');
    c.appendChild(box);
  }else{
    const row=el('div','pbactions');
    const st=el('div','p-status'); st.setAttribute('role','status');
    const approve=el('button','approve','Approve'); approve.type='button';
    const reject=el('button','reject','Reject'); reject.type='button';
    const act=exp.usable;
    if(!act){approve.disabled=true; approve.title='Expired; the daemon will refuse this approve.';}
    // Two-step arm for approve (irreversible side-effects downstream): first tap arms,
    // second tap within 4s commits — but a MINIMUM 300ms must elapse between the two taps.
    // Rationale: without the min-delay a reflexive double-click
    // (trackpad bounce, autoclick, muscle-memory habit) submits in a single gesture, so the
    // "two step" was ceremonial. 300ms is above every documented double-click threshold
    // (OS default ~500ms, slowest Fitts/perception studies ~250ms for intentional discrete
    // taps) while still feeling snappy for a deliberate second tap. A too-fast second tap
    // keeps the control armed (does not reset) so one more deliberate press completes.
    approve.addEventListener('click',()=>{
      if(approve.disabled) return;
      if(!approve.classList.contains('arm')){
        approve.classList.add('arm'); approve.textContent='Approve? tap again';
        approve._armed_at=Date.now();
        clearTimeout(approve._t);
        approve._t=setTimeout(()=>{approve.classList.remove('arm'); approve.textContent='Approve'; approve._armed_at=0},4000);
        return;
      }
      // Enforce min-delay since arm — reject reflexive double-click without un-arming.
      if(Date.now()-(approve._armed_at||0)<300) return;
      clearTimeout(approve._t); approve._armed_at=0; doApprove(it,pb,approve,reject,st);
    });
    reject.addEventListener('click',()=>doReject(it,pb,approve,reject,st));
    row.appendChild(approve); row.appendChild(reject);
    c.appendChild(row); c.appendChild(st);
  }
  const m=el('div','p-meta');
  if((it.source||[]).length) m.appendChild(el('div',null,'From: '+it.source.map(strip).join('  ·  ')));
  if(opts.reason) m.appendChild(el('div',null,'Why this one first: '+opts.reason));
  c.appendChild(m);
  return c;
}
async function doApprove(it,pb,approve,reject,st){
  if(!tok.value){$('cfg').style.display='block';return}
  if(document.body.classList.contains('paused')){
    st.className='p-status bad'; st.textContent='CIRO is paused. Nothing was recorded.'; return;
  }
  approve.disabled=true; reject.disabled=true;
  st.className='p-status'; st.textContent='recording approve…';
  let r=null, j={};
  try{
    r=await fetch('/plate/approve',{method:'POST',headers:H(),body:JSON.stringify({id:it.id,approved_hash:pb.content_hash,conversation_id:CID})});
    try{j=await r.json()}catch(e){j={}}
  }catch(e){
    st.className='p-status bad';
    st.textContent='No answer from the console server. The approve may not have been recorded; the plate will refresh.';
    approve.disabled=false; reject.disabled=false;
    setTimeout(loadPlate,1500); return;
  }
  if(r.status===401){$('cfg').style.display='block';st.className='p-status bad';st.textContent='Not sent: that token did not work. Check settings.';approve.disabled=false;reject.disabled=false;return}
  // 429 is PRE-WIRED for a future daemon-side rate-limit (not emitted today — grep confirms
  // zero hits). When the daemon-side rate-limit lands, this handler receives it unchanged —
  // Retry-After from the header or j.retry_after from the body, whichever the daemon emits.
  if(r.status===429){
    const ra=j.retry_after||r.headers.get('Retry-After')||'';
    st.className='p-status bad';
    st.textContent='Rate limit: try again in '+(ra?ra+'s':'a few minutes')+'. Nothing was recorded.';
    approve.disabled=false; reject.disabled=false; return;
  }
  if(r.status===503 && j.error==='beta_paused'){
    document.body.classList.add('paused');
    st.className='p-status bad'; st.textContent='CIRO is paused for maintenance. Nothing was recorded.';
    approve.disabled=false; reject.disabled=false; return;
  }
  if(!r.ok){
    const why={hash_mismatch:'the staged content changed since you opened this; refreshing',
               expired:'the 24h approval window has passed; the daemon would refuse',
               already_decided:'this one is already decided',
               user_token_required:'only a signed-in person can approve',
               payload_too_large:'the request was too large',
               bad_request:'the request was malformed'}[j.error]||('the server said '+(j.error||r.status));
    st.className='p-status bad'; st.textContent='Not approved: '+why+'. Nothing new was recorded.';
    approve.disabled=false; reject.disabled=false;
    PLATE_SIG=''; setTimeout(loadPlate,1200); return;
  }
  if(j.conversation_id){CID=j.conversation_id;sset('c7',CID)}
  // bubble() escapes HTML (verified: esc() handles &<> in both branches), but hostile
  // Unicode in a daemon-written title (bidi override, variation selector, C1 control)
  // would pass through esc() unchanged. Strip at this seam just like prepCard does on
  // the card, so the convo mirror of an approve stays visually honest.
  const safeTitle=strip(it.title||''), safeShape=strip(pb.shape_id||'');
  const text='Plate · '+safeTitle+' → approve ('+safeShape+')';
  addConvo(bubble('you',text,'you'),true);
  const live=addConvo('<div class="bubble muted think">7 is thinking…</div>',true);
  if(j.run_id) poll(j.run_id,live);
  $('p-last').textContent=j.delivered
    ? 'Approved and sent to CIRO: '+safeTitle
    : 'Approved: '+safeTitle+'. The door did not take the message just now; your approve is still on record.';
  $('p-last').style.color=j.delivered?'':'var(--err)';
  PLATE_SIG=''; await loadPlate();
}
async function doReject(it,pb,approve,reject,st){
  if(!tok.value){$('cfg').style.display='block';return}
  if(document.body.classList.contains('paused')){
    st.className='p-status bad'; st.textContent='CIRO is paused. Nothing was recorded.'; return;
  }
  approve.disabled=true; reject.disabled=true;
  st.className='p-status'; st.textContent='recording reject…';
  let r=null, j={};
  try{
    r=await fetch('/plate/reject',{method:'POST',headers:H(),body:JSON.stringify({id:it.id,conversation_id:CID})});
    try{j=await r.json()}catch(e){j={}}
  }catch(e){
    st.className='p-status bad';
    st.textContent='No answer from the console server. The reject may not have been recorded; the plate will refresh.';
    approve.disabled=false; reject.disabled=false;
    setTimeout(loadPlate,1500); return;
  }
  if(r.status===401){$('cfg').style.display='block';st.className='p-status bad';st.textContent='Not sent: that token did not work. Check settings.';approve.disabled=false;reject.disabled=false;return}
  if(r.status===503 && j.error==='beta_paused'){
    document.body.classList.add('paused');
    st.className='p-status bad'; st.textContent='CIRO is paused for maintenance. Nothing was recorded.';
    approve.disabled=false; reject.disabled=false; return;
  }
  if(!r.ok){
    const why={already_decided:'this one is already decided',user_token_required:'only a signed-in person can reject'}[j.error]||('the server said '+(j.error||r.status));
    st.className='p-status bad'; st.textContent='Not rejected: '+why+'. Nothing new was recorded.';
    approve.disabled=false; reject.disabled=false;
    PLATE_SIG=''; setTimeout(loadPlate,1200); return;
  }
  if(j.conversation_id){CID=j.conversation_id;sset('c7',CID)}
  // Same strip discipline as doApprove — daemon-written fields never reach the convo
  // or status line un-sanitized, even though bubble()'s esc() is HTML-safe.
  const safeTitle=strip(it.title||''), safeShape=strip(pb.shape_id||'');
  const text='Plate · '+safeTitle+' → reject ('+safeShape+')';
  addConvo(bubble('you',text,'you'),true);
  const live=addConvo('<div class="bubble muted think">7 is thinking…</div>',true);
  if(j.run_id) poll(j.run_id,live);
  $('p-last').textContent='Rejected: '+safeTitle+'.';
  $('p-last').style.color='';
  PLATE_SIG=''; await loadPlate();
}
// Re-paint expiry labels every 30s so the clock stays honest without a full plate refresh.
setInterval(()=>{
  document.querySelectorAll('.pcard.p-await').forEach(c=>{
    const iso=c.dataset.exp||''; if(!iso) return;
    const lab=expireLabel(iso);
    const span=c.querySelector('.pbexp'); if(!span) return;
    span.textContent=lab.text;
    span.className='pbexp'+(lab.cls?' '+lab.cls:'');
    const ap=c.querySelector('.pbactions .approve');
    if(ap && !lab.usable){ap.disabled=true; ap.title='Expired; the daemon will refuse this approve.'}
  });
},30000);
function renderPlate(){
  const box=$('plate'), body=$('p-body'), zb=$('p-zoom');
  const items=(PLATE&&PLATE.items)||[];
  // BETA_PAUSED banner — the server reports this on every /plate; stat-at-every-ask reaches
  // the UI quickly without caching. The approve/reject buttons are also gated server-side.
  document.body.classList.toggle('paused', !!(PLATE&&PLATE.beta_paused));
  if(!items.length){box.hidden=true;document.body.classList.remove('has-plate','zoomed');return}
  const sig=JSON.stringify([PLATE,ZOOM]);
  if(sig===PLATE_SIG) return;
  PLATE_SIG=sig;
  box.hidden=false; document.body.classList.add('has-plate'); document.body.classList.toggle('zoomed',ZOOM);
  body.textContent='';
  const byId={}; items.forEach(it=>byId[it.id]=it);
  const awaitingOrder=PLATE.awaiting_approval||[];
  const awaiting=awaitingOrder.map(id=>byId[id]).filter(Boolean);
  if(!ZOOM){
    $('p-eyebrow').textContent='next on your plate';
    zb.textContent='see all on your plate ('+items.length+')';
    // Awaiting-approval renders FIRST when any exist — a hash-bound proposal is more
    // time-sensitive than a free-choice open question. Both can appear; the register is
    // visually distinct per the group's CSS.
    if(awaiting.length){
      const g=el('section','p-group');
      g.appendChild(el('h3',null,'Awaiting your approval ('+awaiting.length+')'));
      g.appendChild(prepCard(awaiting[0],{}));
      if(awaiting.length>1) g.appendChild(el('p','p-empty','+ '+(awaiting.length-1)+' more awaiting — see all on your plate'));
      body.appendChild(g);
    }
    if(PLATE.next) body.appendChild(card(PLATE.next,{reason:PLATE.reason}));
    else if(!awaiting.length) body.appendChild(el('p','p-empty','Nothing needs you right now.'));
    return;
  }
  $('p-eyebrow').textContent='all on your plate';
  zb.textContent='back to next';
  const groups=[
    ['Awaiting your approval',awaiting],
    ['Needs you',(PLATE.order||[]).map(id=>byId[id]).filter(Boolean)],
    ['Settled',items.filter(it=>it.group==='settled')],
    ['Resting',items.filter(it=>it.group==='resting')],
  ];
  groups.forEach(([name,list])=>{
    if(!list.length) return;
    const g=el('section','p-group'); g.appendChild(el('h3',null,name+' ('+list.length+')'));
    list.forEach(it=>g.appendChild(it.prepared_b?prepCard(it,{compact:true}):card(it,{compact:true})));
    body.appendChild(g);
  });
}
$('p-zoom').addEventListener('click',()=>{ZOOM=!ZOOM;renderPlate();$('plate').scrollTop=0;$('p-zoom').focus({preventScroll:true})});
async function loadPlate(){
  // Always attempt — the HttpOnly cookie may authorize us even when no bearer is pasted.
  // The response status also doubles as the auth probe that sets AUTHED: 200 → cookie (or
  // bearer) works; 401/503 → unauthed. First-visit arrivers 401 here harmlessly and go on
  // to see the reception + chips as designed.
  try{
    const r=await fetch('/plate',{headers:H()});
    if(r.status===401||r.status===503){AUTHED=false;PLATE=null;renderPlate();syncUndos();return}
    if(!r.ok){PLATE=null;renderPlate();syncUndos();return}
    AUTHED=true;
    PLATE=await r.json(); renderPlate(); syncUndos();
  }catch(e){}
}
// --- undo: take back a plate choice -------------------------------------------------------------
// A dock bubble is a plate act when its turn carries plate:{op:'act',id,act_ts}, which only the
// server writes (on /plate/act). Turns from before that marker existed fall back to an exact text
// match ("Plate · <title> → <choice>" for the item's current act), and only the newest such bubble
// gets the control. Undo is drawn only while that act is still the item's current act; once it is
// undone the bubble says so; after anything else it shows nothing.
const LEGACY_TS=/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/;   // a pre-marker act: whole seconds, no fraction
function plateItem(id){return ((PLATE&&PLATE.items)||[]).find(it=>it.id===id)||null}
function markPlate(d,meta,text){
  const b=d&&d.querySelector('.bubble.you'); if(!b) return;
  if(meta&&(meta.op==='act'||meta.op==='react')&&typeof meta.id==='string'&&typeof meta.act_ts==='string'){
    b.dataset.pid=meta.id; b.dataset.pts=meta.act_ts;
  }else if(!meta&&typeof text==='string'&&text.startsWith('Plate · ')&&!/ → undo \(was: /.test(text)){
    b.dataset.plegacy='1'; b._ptext=text;
  }else return;
  const slot=el('div','pundo'); b.appendChild(slot);
}
function syncUndos(){
  // Legacy bubbles: bind each current act to the OLDEST bubble whose text is exactly that act
  // (pre-undo, each legacy item had at most one tap bubble; the oldest match is that tap, not a
  // later typed copy).
  // RULE: the text fallback applies ONLY to acts recorded before the turn marker existed. Those
  // acts have second-resolution ts ("…:SSZ"); every act since carries microseconds ("…:SS.ffffffZ")
  // and its own bubble carries the marker. So an act with fractional seconds is never bound by
  // text: a typed message that mimics "Plate · <title> → <choice>" cannot pick up its Undo.
  const legacy=[...convo.querySelectorAll('.bubble.you[data-plegacy]:not([data-pid])')];
  ((PLATE&&PLATE.items)||[]).forEach(it=>{
    if(!it.act||typeof it.act.ts!=='string') return;
    if(!LEGACY_TS.test(it.act.ts)) return;   // post-change act: marker only, never text
    if(convo.querySelector('.bubble[data-pts="'+CSS.escape(it.act.ts||'')+'"]')) return;
    const want='Plate · '+it.title+' → '+it.act.choice;
    const b=legacy.find(x=>x._ptext===want&&!x.dataset.pid);
    if(b){b.dataset.pid=it.id;b.dataset.pts=it.act.ts}
  });
  convo.querySelectorAll('.bubble.you .pundo').forEach(slot=>paintUndo(slot.closest('.bubble')));
}
function paintUndo(b){
  const slot=b.querySelector('.pundo'); if(!slot) return;
  const it=b.dataset.pid?plateItem(b.dataset.pid):null;
  let state='none';
  if(it&&it.act&&it.act.ts===b.dataset.pts) state='live';
  else if(it&&(it.replaced||[]).indexOf(b.dataset.pts)>=0) state='replaced';
  else if(it&&(it.undone||[]).indexOf(b.dataset.pts)>=0) state='undone';
  if(slot.dataset.state===state) return;   // leave an armed or in-flight control alone
  change(()=>{
    slot.dataset.state=state; slot.textContent='';
    if(state==='live') undoInto(slot,it);
    else if(state==='replaced') slot.appendChild(el('span',null,'replaced'));
    else if(state==='undone') slot.appendChild(el('span',null,'undone'));
  });
}
// The Undo for an item's current act. When that act was a typed re-choose, Undo reopens the card
// and does NOT bring the earlier choice back; the control says so before anyone taps it.
function undoInto(slot,it){
  const rep=it.act&&it.act.replaced;
  if(rep){
    const msg='Undo reopens '+numLabel(it.num)+' ('+rep+' is not restored).';
    slot.appendChild(el('div','pnote',msg));
  }
  const b=undoBtn(it.id,it.act.ts,slot);
  if(rep) b.setAttribute('aria-label','Undo this choice. It reopens '+numLabel(it.num)+'; '+rep+' is not restored.');
  slot.appendChild(b);
}
// One control for the dock bubble and the settled card. First tap arms it ("Undo? tap again");
// a second tap within 4s sends; otherwise it goes back to "Undo". No browser dialog.
function undoBtn(id,actTs,slot){
  const b=el('button','undo','Undo'); b.type='button';
  b.setAttribute('aria-label','Undo this choice');
  b.addEventListener('click',()=>{
    if(b.disabled) return;
    if(!b.classList.contains('arm')){
      b.classList.add('arm'); b.textContent='Undo? tap again'; b.setAttribute('aria-label','Tap again to undo this choice');
      $('say').textContent='Tap again to undo';
      clearTimeout(b._t); b._t=setTimeout(()=>{b.classList.remove('arm');b.textContent='Undo';b.setAttribute('aria-label','Undo this choice');$('say').textContent=''},4000);
      return;
    }
    clearTimeout(b._t); doUndo(id,actTs,b,slot);
  });
  return b;
}
function undoNote(slot,msg){
  slot.dataset.state='msg'; slot.textContent='';
  slot.appendChild(el('span','bad',msg)); $('say').textContent=msg;
}
async function doUndo(id,actTs,b,slot){
  if(!tok.value){$('cfg').style.display='block';return}
  b.disabled=true; b.classList.remove('arm'); b.textContent='undoing…'; slot.dataset.state='busy';
  let r=null, j={};
  try{
    r=await fetch('/plate/undo',{method:'POST',headers:H(),body:JSON.stringify({id:id,act_ts:actTs,conversation_id:CID})});
    try{j=await r.json()}catch(e){j={}}
  }catch(e){
    undoNote(slot,'No answer from the console server. The undo may not have been recorded; the plate will refresh to show what is on record.');
    PLATE_SIG=''; setTimeout(loadPlate,1500); return;
  }
  if(r.status===401){$('cfg').style.display='block';undoNote(slot,'Not undone: that token did not work. Check settings.');return}
  if(!r.ok){
    const why={nothing_to_undo:'Already undone. Nothing new was recorded.',
               stale_undo:'You chose again since then, so this one was left alone.',
               unknown_item:'Not undone: this card is not on your plate.',
               user_token_required:'Not undone: only a signed-in person can undo a choice.',
               payload_too_large:'Not undone: the request was too large.'}[j.error]
            ||('Not undone: the server said '+(j.error||r.status)+'.');
    undoNote(slot,why); PLATE_SIG=''; setTimeout(loadPlate,2500); return;
  }
  // Recorded. The undo goes to CIRO in the dock like any message (no Undo on an undo).
  if(j.conversation_id){CID=j.conversation_id;sset('c7',CID)}
  addConvo(bubble('you',j.text||'Plate · undo','you'),true);
  const live=addConvo('<div class="bubble muted think">7 is thinking…</div>',true);
  if(j.run_id) poll(j.run_id,live);
  const it=plateItem(id), title=it?it.title:'', was=j.undid?j.undid.choice:'';
  const rep=j.undid&&j.undid.replaced, tag=it&&it.num?numLabel(it.num)+' ':'';
  const open=rep?' '+tag+'is open again; '+rep+' is not restored.':' Its choices are open again.';
  $('p-last').textContent=j.delivered
    ? 'Undone and sent to CIRO: '+title+' (was: '+was+').'+open
    : 'Undone: '+title+' (was: '+was+').'+open+' The door did not take the message just now; the undo is still on record.';
  $('p-last').style.color=j.delivered?'':'var(--err)';
  PLATE_SIG=''; await loadPlate();
}
async function tapChoice(it,choice,row,st){
  if(!tok.value){$('cfg').style.display='block';return}
  row.querySelectorAll('button').forEach(b=>b.disabled=true);
  st.className='p-status'; st.textContent='sending “'+choice+'”…';
  let r=null, j={};
  try{
    r=await fetch('/plate/act',{method:'POST',headers:H(),body:JSON.stringify({id:it.id,choice:choice,conversation_id:CID})});
    try{j=await r.json()}catch(e){j={}}
  }catch(e){
    st.className='p-status bad';
    st.textContent='No answer from the console server. Your choice may not have been recorded; the plate will refresh to show what is on record.';
    row.querySelectorAll('button').forEach(b=>b.disabled=false);
    setTimeout(loadPlate,1500); return;
  }
  if(r.status===401){$('cfg').style.display='block';st.className='p-status bad';st.textContent='Not sent: that token did not work. Check settings. Nothing was recorded.';row.querySelectorAll('button').forEach(b=>b.disabled=false);return}
  if(r.status===409){st.className='p-status bad';st.textContent='Already settled'+(j.act?' (you chose “'+j.act.choice+'”)':'')+'. Nothing new was recorded.';setTimeout(loadPlate,1500);return}
  if(!r.ok){
    const why={invalid_choice:'that choice is not one of this card’s choices',unknown_item:'this card is not on your plate',user_token_required:'only a signed-in person can settle a card',payload_too_large:'the request was too large'}[j.error]||('the server said '+(j.error||r.status));
    st.className='p-status bad'; st.textContent='Not sent: '+why+'. Nothing was recorded.';
    row.querySelectorAll('button').forEach(b=>b.disabled=false); return;
  }
  // Recorded. Show the message going to CIRO in the dock, exactly like a typed one.
  if(j.conversation_id){CID=j.conversation_id;sset('c7',CID)}
  markPlate(addConvo(bubble('you',j.text||('Plate · '+it.title+' → '+choice),'you'),true),
            j.act?{op:'act',id:j.act.id,act_ts:j.act.ts}:null);
  const live=addConvo('<div class="bubble muted think">7 is thinking…</div>',true);
  if(j.run_id) poll(j.run_id,live);
  $('p-last').textContent=j.delivered
    ? 'Sent to CIRO: '+it.title+' → '+choice
    : 'Recorded: '+it.title+' → '+choice+'. The door did not take the message just now; your choice is still on record.';
  if(!j.delivered) $('p-last').style.color='var(--err)'; else $('p-last').style.color='';
  PLATE_SIG=''; await loadPlate();
}
// --- typed commands: "#00012 revisit" in the chat -----------------------------------------------
// The server parses and records it (on /run, for a signed-in person only) and says what happened
// in j.plate. Here we only show that: a recorded choice binds the bubble to its act (so it gets
// Undo, like a tap), anything else gets one plain line saying nothing was recorded, and why.
function numLabel(n){return '#'+String(n).padStart(5,'0')}
function typedCommand(d,p){
  const b=d&&d.querySelector('.bubble.you'); if(!b) return;
  const tag=numLabel(p.num||0), t=p.title?' '+p.title:'';
  const list=(p.choices||[]).join('  ·  ');
  let msg='', bad=true;
  if((p.op==='act'||p.op==='react')&&p.act){
    markPlate(d,{op:p.op,id:p.id,act_ts:p.act.ts});
    msg='Recorded: '+tag+t+' → '+p.act.choice+(p.op==='react'&&p.undid?' (was: '+p.undid.choice+')':'')+'.';
    bad=false;
    $('p-last').textContent=msg; $('p-last').style.color='';
  }else if(p.op==='already'&&p.act){
    msg=tag+' is already “'+p.act.choice+'”. Nothing new was recorded.';
  }else if(p.error==='question'){
    msg='Nothing was recorded. The choices on '+tag+': '+list+'.';
  }else if(p.error==='ambiguous_choice'||p.error==='no_match'){
    msg=(p.error==='ambiguous_choice'?'That could be more than one choice on ':'No choice like that on ')+tag+'. Its choices: '+list+'. Nothing was recorded.';
  }else if(p.error==='unknown_item'){
    msg='Can’t act on '+tag+'. Nothing was recorded.';
  }else if(p.error==='user_token_required'){
    msg='Only a signed-in person can settle a card, so nothing was recorded.';
  }else{
    msg='Nothing was recorded (the server said '+(p.error||'no')+').';
  }
  change(()=>{const n=el('div','pcmd'+(bad?' bad':''),msg);const s=b.querySelector('.pundo');b.insertBefore(n,s||null)});
  $('say').textContent=msg;
  PLATE_SIG=''; loadPlate();
}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)loadPlate()});
tok.addEventListener('change',loadPlate);
loadPlate();
// Enter sends; Shift+Enter is a new line; an Enter that confirms an IME composition is left alone.
task.addEventListener('keydown',e=>{if(e.isComposing||e.keyCode===229)return;if((e.key==='Enter'||e.keyCode===13||e.which===13)&&!e.shiftKey){e.preventDefault();go();}});
</script></body></html>"""


def _make_handler(manager: RunManager, token: str | None,
                  plate_path: str | None = None) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class closed over the manager and configured token.

    plate_path: the plate record (plate.jsonl). None resolves per request to the bridge-dir
    default (CATHEDRAL_BRIDGE_DIR, else ~/.agnostic_runtime/cathedral/plate.jsonl).
    """

    def _plate() -> str:
        return plate_path or plate_mod.default_path()

    class TriggerHandler(BaseHTTPRequestHandler):
        # Bound every socket operation: StreamRequestHandler.setup() applies this as the
        # connection's timeout, so no read (request line, headers, or body) can block forever.
        timeout = READ_TIMEOUT

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def _is_https_request(self) -> bool:
            """Does this request look like it reached us over TLS?

            Two signals: a reverse proxy sets X-Forwarded-Proto: https, OR the raw
            request itself was TLS (self.rfile ultimately wraps an SSLSocket). The
            server itself speaks plain HTTP (bound to 127.0.0.1 by default), so in
            practice the proxy header is the signal that matters in production.

            A dev run on 127.0.0.1/localhost is NOT https — we want cookies to work
            without Secure so the dev doesn't have to run a local TLS proxy.
            Returning False keeps Secure off; the browser accepts the cookie over
            http://localhost per secure-context rules.
            """
            fwd = (self.headers.get("X-Forwarded-Proto") or "").strip().lower()
            if fwd == "https":
                return True
            # Belt-and-suspenders: X-Forwarded-Ssl: on is older but still seen.
            if (self.headers.get("X-Forwarded-Ssl") or "").strip().lower() == "on":
                return True
            return False

        def _token_cookie_header(self, token_value: str, max_age: int = 31536000) -> str:
            """Build a Set-Cookie value for the auth token.

            Secure is added when the request appears to be TLS; without that signal
            the cookie is still HttpOnly + SameSite=Lax (the floor is the same, the
            Secure flag just adds TLS-only transmission on top). Localhost dev stays
            testable (secure-context exception); a prod request behind an https
            proxy gets Secure.
            """
            secure = "; Secure" if self._is_https_request() else ""
            return (
                f"token={token_value}; Path=/; HttpOnly; SameSite=Lax; "
                f"Max-Age={max_age}{secure}"
            )

        def _emit_renewal_cookie(self) -> None:
            """If this request was authorized by cookie, re-emit the cookie with a
            fresh Max-Age. Sliding-window: an active session keeps itself alive;
            a session that goes idle for a year dead-ends to signup, as designed.

            Must be called AFTER send_response and BEFORE end_headers.
            """
            tok = getattr(self, "_renew_cookie_token", None)
            if tok:
                self.send_header("Set-Cookie", self._token_cookie_header(tok))

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._emit_renewal_cookie()
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, code: int, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # No-store: a redeployed console must reach returning clients, not a stale cache.
            # (A cached older console is the likely cause of divergent first-touch reports.)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self._emit_renewal_cookie()
            self.end_headers()
            self.wfile.write(body)

        def _resolve_user_token(self, token_str: str | None) -> tuple[str | None, str | None]:
            """Shared resolver: a token string → (user_id, role) if it maps to a user, else
            (None, None). Collapses the identical lookup that bearer-path and cookie-path
            used to do inline — same auth story on both doors.
            """
            if not token_str or not isinstance(token_str, str):
                return None, None
            rec = user_for_token(token_str)
            if rec is None:
                return None, None
            return rec.get("id"), rec.get("declared_role")

        def _cookie_token(self) -> str:
            """Extract the `token` cookie value from the Cookie header. Returns ''
            on absent / malformed; never raises.
            """
            cookie_header = self.headers.get("Cookie", "") or ""
            for part in cookie_header.split(";"):
                kv = part.strip().split("=", 1)
                if len(kv) == 2 and kv[0] == "token":
                    return kv[1]
            return ""

        def _authorized(self) -> bool:
            """Resolve the Bearer header and set ``self._user_id`` / ``self._role`` for the
            downstream handler.

            Two paths, in order:
              1) users.py — the Bearer resolves to a user record (user_id set, role set).
                 This is the normal user path. Shield-steward: this IS the re-ask-the-empty;
                 the ownership comparison downstream makes it an admit-atom instead of a bare
                 auth check.
              2) env token — the Bearer matches AGNOSTIC_TRIGGER_TOKEN (admin / back-compat).
                 user_id stays None; ownership checks bypass (admin can touch any record).

            A no-token server still 503s. A Bearer that matches neither returns 401. The
            handler attributes default to None so a path that forgets to check still gets a
            safe-by-default admin interpretation downstream — but a path that forgets to call
            `_authorized()` first will see them as None too (no silent user leakage).
            """
            self._user_id = None  # noqa: SLF001
            self._role = None     # noqa: SLF001
            self._renew_cookie_token = None  # noqa: SLF001
            auth = self.headers.get("Authorization", "")
            bearer = auth[7:] if auth.startswith("Bearer ") else ""
            # Path 1: user token in the Authorization header.
            uid, role = self._resolve_user_token(bearer)
            if uid is not None:
                self._user_id = uid
                self._role = role
                return True
            # Path 2: env admin token (back-compat). An env token means "admin / daemon" —
            # not a user. If the env token is unset AND the user path missed, the server IS
            # configured (users.py is the auth source); the bearer just didn't match → 401.
            if token and auth == f"Bearer {token}":
                return True
            # Path 3: cookie token (post-signup browser session). HttpOnly cookie set by
            # /signup; JS cannot read it, but the browser sends it on every same-origin fetch.
            # This is what lets a signed-up arriver reach the authed surface WITHOUT pasting a
            # token into any field. Parsed defensively — a malformed Cookie header never raises.
            cookie_token = self._cookie_token()
            if cookie_token:
                uid, role = self._resolve_user_token(cookie_token)
                if uid is not None:
                    self._user_id = uid
                    self._role = role
                    # Sliding-window: a cookie that just worked gets a fresh Max-Age on this
                    # response, so active use keeps the session alive and only idleness lets it lapse.
                    self._renew_cookie_token = cookie_token
                    return True
            # No valid auth. 503 only when NEITHER source is set up (no env AND no users on
            # disk) — a cold server that cannot authorize anyone yet.
            try:
                from agnostic_runtime.trigger import users as _users
                any_user_exists = bool(_users._load_raw())  # noqa: SLF001
            except Exception:
                any_user_exists = False
            if not token and not any_user_exists:
                self._send(503, {"error": "no_token_configured"})
                return False
            self._send(401, {"error": "unauthorized"})
            return False

        def _read_json(self) -> tuple[dict[str, Any] | None, bool]:
            # Parse Content-Length defensively: a missing header is an empty body, a non-integer
            # or negative one is a bad request (never an unhandled ValueError that kills the
            # thread with no response).
            raw_len = self.headers.get("Content-Length")
            try:
                length = int(raw_len) if raw_len is not None else 0
            except (TypeError, ValueError):
                self._send(400, {"error": "bad_request"})
                return None, False
            if length < 0:
                self._send(400, {"error": "bad_request"})
                return None, False
            # Refuse an oversized body up front — before reading a single byte — so a huge
            # (honest or hostile) Content-Length cannot make us block waiting on it.
            if length > MAX_BODY:
                self._send(413, {"error": "payload_too_large"})
                return None, False
            try:
                raw = self.rfile.read(length) if length > 0 else b""
            except (socket.timeout, TimeoutError, OSError):
                # The client declared more than it delivered; the bounded socket timeout fired
                # instead of hanging the worker. Answer if we still can, then drop the connection.
                try:
                    self._send(408, {"error": "request_timeout"})
                except OSError:
                    pass
                self.close_connection = True
                return None, False
            if not raw:
                return {}, True
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                self._send(400, {"error": "bad_json"})
                return None, False
            if not isinstance(data, dict):
                self._send(400, {"error": "bad_json"})
                return None, False
            return data, True

        def _tell_cathedral(self, text: str, data: dict, plate_meta: dict) -> dict:
            """A plate act or undo is on the record; now tell CIRO through the SAME path a
            typed message takes (RunManager.start on the cathedral door), so the door receives
            it like any message and can stop work in flight. delivered=False is reported,
            never hidden. Used by both /plate/act and /plate/undo."""
            cid = data.get("conversation_id")
            run_id = manager.start(text, model="cathedral",
                                   conversation_id=cid if isinstance(cid, str) else None,
                                   user_id=self._user_id, role=self._role, plate=plate_meta)
            status = manager.status(run_id, user_id=self._user_id)
            return {
                "text": text,
                "run_id": run_id,
                "conversation_id": status.get("conversation_id"),
                "delivered": status.get("error") != "undeliverable",
            }

        def _plate_command(self, task: str, model: Any) -> tuple[dict | None, dict | None]:
            """A typed "#00012 revisit" on /run: (reply for the console, turn marker).

            Parsed here, on the authenticated request, and nowhere else: a model reply, the
            attendant or the door never comes through /run as a person, so none of them can
            settle a card. Only on the CIRO path (the plate is CIRO's). The message
            itself always goes on to the door as typed; this only records the choice first.
            The env/admin token is refused the act (it is not a person) but still delivers.
            """
            parsed = plate_mod.parse_command(task)
            if parsed is None or model != "cathedral":
                return None, None
            num, query = parsed
            if self._user_id is None:
                return {"error": "user_token_required", "num": num}, None
            code, r = plate_mod.command(_plate(), num, query, self._user_id)
            r = {**r, "num": num}
            if code != 200 or r.get("op") == "already":
                return r, None
            return r, {"op": r["op"], "id": r["id"], "act_ts": r["act"]["ts"]}

        def _read_signup_body(self) -> tuple[dict[str, Any] | None, bool]:
            """Parse /signup's body. Accepts application/json (the JS path) OR
            application/x-www-form-urlencoded (the <noscript> fallback form). On
            parse error, replies 400 and returns (None, False).
            """
            raw_len = self.headers.get("Content-Length")
            try:
                length = int(raw_len) if raw_len is not None else 0
            except (TypeError, ValueError):
                self._send(400, {"error": "bad_request"})
                return None, False
            if length < 0 or length > MAX_BODY:
                self._send(413 if length > MAX_BODY else 400,
                           {"error": "payload_too_large" if length > MAX_BODY else "bad_request"})
                return None, False
            try:
                raw = self.rfile.read(length) if length > 0 else b""
            except (socket.timeout, TimeoutError, OSError):
                try:
                    self._send(408, {"error": "request_timeout"})
                except OSError:
                    pass
                self.close_connection = True
                return None, False
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype == "application/x-www-form-urlencoded":
                try:
                    from urllib.parse import parse_qs
                    parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                except (UnicodeDecodeError, ValueError):
                    self._send(400, {"error": "bad_form"})
                    return None, False
                # parse_qs returns {k: [v, ...]}; collapse to the first value per key.
                flat = {k: (v[0] if v else "") for k, v in parsed.items()}
                return flat, True
            # Default (and JS) path: JSON.
            if not raw:
                return {}, True
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                self._send(400, {"error": "bad_json"})
                return None, False
            if not isinstance(data, dict):
                self._send(400, {"error": "bad_json"})
                return None, False
            return data, True

        def _signup_reply(self, code: int, payload: dict[str, Any]) -> None:
            """Error path on /signup. Honors Accept: when the <noscript> form
            submitted (text/html expected), reply as a tiny HTML page the arriver
            can read without JS. Otherwise JSON for the fetch() caller.
            """
            if self._wants_html():
                self._send_signup_html(code, payload.get("message")
                                       or payload.get("error", "signup did not go through"))
                return
            self._send(code, payload)

        def _signup_success(self, rec: dict) -> None:
            """Success path. Sets the auth cookie; replies JSON (JS reload) or an
            HTML redirect page (noscript fallback)."""
            cookie = self._token_cookie_header(rec["token"])
            if self._wants_html():
                body = (
                    "<!doctype html><meta charset=utf-8>"
                    "<meta http-equiv=refresh content=\"0;url=/\">"
                    "<title>CIRO — signed up</title>"
                    "<p style=\"font-family:system-ui,sans-serif;padding:2rem\">"
                    "signed up. <a href=\"/\">Open CIRO</a>.</p>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(body)
                return
            body = json.dumps({"ok": True, "redirect": "/"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def _wants_html(self) -> bool:
            """Did this /signup request come from the noscript form? The form sets
            Accept: text/html by default; the fetch() path sends Accept: */* and
            Content-Type: application/json.
            """
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype == "application/x-www-form-urlencoded":
                return True
            accept = (self.headers.get("Accept") or "").lower()
            if "application/json" in accept:
                return False
            return "text/html" in accept

        def _send_signup_html(self, code: int, message: str) -> None:
            # html.escape — defense-in-depth. Current callers pass fixed strings,
            # but a future caller interpolating a user-supplied field would leak
            # the field into the DOM as HTML without this. One wrap closes it.
            import html as _html
            safe = _html.escape(message, quote=True)
            body = (
                "<!doctype html><meta charset=utf-8>"
                "<title>CIRO — signup</title>"
                "<main style=\"font-family:system-ui,sans-serif;max-width:30rem;margin:3rem auto;padding:0 1rem;line-height:1.5\">"
                f"<p>{safe}</p>"
                "<p><a href=\"/\">back to CIRO</a></p></main>"
            ).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # --- routing -----------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802
            # /signup is a PUBLIC passthrough (no Bearer required): the arriver's first
            # door. It validates an invite code + email, seeds a user record, consumes
            # the invite, and sets an HttpOnly cookie carrying the new token. The
            # browser then sends that cookie on every subsequent same-origin fetch; the
            # arriver never pastes a token.
            #
            # ORDER OF CHECKS (deliberate):
            #   1. incident pause → 503
            #   2. body / email shape → 400
            #   3. invite code presence or validity → 403 ("bad_invite" — same shape
            #      for empty, missing, and bad; a JSON probe cannot tell "didn't send"
            #      from "sent nothing matching", closing the enumeration oracle)
            #   4. email already registered → 409
            #   5. seed → 201 + cookie
            #   6. ONLY on success: consume the invite code (one-way admission)
            #
            # CRITICAL SECTION. Steps 3→6 all run inside a SINGLE held
            # ``invite_locked_section()``. The previous shape probed liveness BEFORE
            # taking the lock, which left a TOCTOU window: two arrivers typing the
            # same code with different emails could both pass the probe, both seed a
            # user record, and one consume lose — leaving an orphan user slot that
            # nobody owns. Holding the flock across probe + collision + seed + consume
            # restores the one-slot-one-admission invariant: the second arriver blocks
            # on the lock, re-probes after the first consumes, and sees bad_invite.
            #
            # The invite gate stands BEFORE the email-collision check, so the collision
            # branch is only reachable to arrivers who already proved they belong here.
            # A burnt code is a cost an attacker must pay to even probe an email — which
            # closes the enumeration oracle at the economic rather than cryptographic level
            # (the right shape for a private-by-default space; cf. the fenced-banyan posture).
            path = self.path.rstrip("/") or "/"
            parts = [p for p in path.split("/") if p]
            if parts == ["signup"]:
                # Default attributes so _emit_renewal_cookie never crashes if a handler path
                # didn't go through _authorized. (/signup never does.)
                self._renew_cookie_token = None  # noqa: SLF001
                if _beta_paused():
                    self._send(503, {"error": "beta_paused",
                                     "message": "CIRO is paused for maintenance. "
                                                "Please check back shortly."})
                    return
                data, ok = self._read_signup_body()
                if not ok:
                    return
                email = (data.get("email") or "").strip()
                # Basic shape only: non-empty, has '@' with text on both sides, ≤254 chars
                # (RFC 5321 practical cap). Deep validation (MX, confirm-email flow) is
                # intentionally out-of-scope for S1 — this is the honest floor.
                if (not email or "@" not in email
                        or email.startswith("@") or email.endswith("@")
                        or len(email) > 254 or " " in email):
                    return self._signup_reply(400, {"error": "bad_email"})
                declared_role = (data.get("declared_role") or "").strip() or "stranger"
                invite = (data.get("invite_code") or "").strip()
                # Hold the invite flock for the ENTIRE critical section. If fcntl is
                # unavailable (Windows) the CM yields False — we still run the same
                # branches; correctness holds against the single-code-double-use
                # pathology via _write_atomic's rename, and the TOCTOU window is the
                # same narrow one the brief notes is not what the beta defends against.
                with _invites.invite_locked_section():
                    # (3) Missing OR invalid invite collapse to one response. The
                    #     previous shape returned "invite_required" vs "bad_invite"
                    #     — a JSON probe could distinguish them, oraclising the
                    #     field. Now the response is the same for both.
                    if not invite or not _invites.is_code_live(invite):
                        _invites.record_failed_probe(
                            "bad_invite" if invite else "no_invite")
                        return self._signup_reply(403, {
                            "error": "bad_invite",
                            "message": "Signup requires an invite."})
                    # (4) NOT idempotent on email: refuse re-signup with an existing
                    #     email. Returning the existing user's token would let anyone
                    #     impersonate anyone just by knowing their email. The honest
                    #     minimum is to refuse and point at the (NAMED-not-built)
                    #     sign-in path: a magic-link flow carrying proof-of-email,
                    #     which S1 does NOT ship.
                    if user_for_email(email) is not None:
                        return self._signup_reply(409, {
                            "error": "email_in_use",
                            "message": "That email is already signed up. "
                                       "Sign-in by link is being wired."})
                    # (5) Seed the user.
                    try:
                        rec = seed_user(email, declared_role)
                    except ValueError:
                        return self._signup_reply(400, {"error": "bad_email"})
                    # (6) Consume the invite only after the user record is on disk.
                    #     Still inside the lock, so a race with another arriver using
                    #     the same code has already blocked on the flock above and
                    #     will re-probe after we release and see bad_invite.
                    #     ``_consume_locked`` requires the lock we hold.
                    _invites._consume_locked(invite, rec["id"])
                self._signup_success(rec)
                return
            if not self._authorized():
                return

            # BETA_PAUSED: refuse mutations while the sentinel is present. GETs still work
            # so the console can render the maintenance banner from /plate's reply.
            # /plate/approve and /plate/reject are safety-critical: a prepared-B
            # approve during an incident is exactly what the pause is there to prevent.
            #
            # No Retry-After here, by design: an incident pause is of
            # UNKNOWN duration — the operator sets BETA_PAUSED when something is wrong, and
            # removes it when it's fixed. A fabricated ETA ("retry in 60s") would be worse
            # than no ETA: a client that retries at that interval during an unresolved
            # incident hammers the server and lies to the person reading the banner. The
            # console polls /plate on resume (visibilitychange) and surfaces the paused
            # state at every mutation attempt, which is the honest signal.
            if _beta_paused() and parts and parts[0] in ("run", "plate"):
                self._send(503, {"error": "beta_paused",
                                 "message": "CIRO is paused for maintenance. "
                                            "Please check back shortly."})
                return

            if parts == ["run"]:
                data, ok = self._read_json()
                if not ok:
                    return
                task = data.get("task") or ""
                if not task.strip():
                    self._send(400, {"error": "missing_task"})
                    return
                model = data.get("model", "stub") or "stub"
                conversation_id = data.get("conversation_id")
                cmd, marker = self._plate_command(task, model)
                run_id = manager.start(task, model=model,
                                       conversation_id=conversation_id,
                                       user_id=self._user_id, role=self._role, plate=marker)
                status = manager.status(run_id, user_id=self._user_id)
                out = {"run_id": run_id, "conversation_id": status.get("conversation_id")}
                if cmd is not None:
                    out["plate"] = cmd
                self._send(200, out)
                return

            if len(parts) == 3 and parts[0] == "run" and parts[2] == "confirm":
                data, ok = self._read_json()
                if not ok:
                    return
                # Owner must match the run's user_id — the admit-atom's ownership gate.
                result = manager.confirm(parts[1], data.get("move_id"),
                                         user_id=self._user_id)
                if isinstance(result, dict) and result.get("error") == "unknown_run":
                    self._send(404, result)
                    return
                self._send(200, result)
                return

            if len(parts) == 3 and parts[0] == "run" and parts[2] == "decline":
                result = manager.decline(parts[1], user_id=self._user_id)
                if isinstance(result, dict) and result.get("error") == "unknown_run":
                    self._send(404, result)
                    return
                self._send(200, result)
                return

            if parts == ["plate", "act"]:
                # A human tap on a plate card. Only a USER token settles anything: the env/admin
                # token (which a daemon could hold) may read the plate but never act on it.
                data, ok = self._read_json()
                if not ok:
                    return
                if self._user_id is None:
                    self._send(403, {"error": "user_token_required"})
                    return
                code, result = plate_mod.act(_plate(), data.get("id"), data.get("choice"),
                                             self._user_id)
                if code != 200:
                    self._send(code, result)
                    return
                text = "Plate · " + result["title"] + " → " + result["act"]["choice"]
                out = self._tell_cathedral(text, data, {
                    "op": "act", "id": result["act"]["id"], "act_ts": result["act"]["ts"]})
                self._send(200, {"act": result["act"], **out})
                return

            if parts == ["plate", "approve"]:
                # Prepared-B approve. The hash-bind is enforced in plate_mod.approve_prepared;
                # a stale browser view (the item was re-staged with a new hash) returns 409
                # hash_mismatch so the console reloads and renders the current state. Only a
                # user token admits — an env/admin token reads the plate but never speaks for
                # operator at the approve seam (mirrors /plate/act).
                data, ok = self._read_json()
                if not ok:
                    return
                if self._user_id is None:
                    self._send(403, {"error": "user_token_required"})
                    return
                code, result = plate_mod.approve_prepared(
                    _plate(), data.get("id"), data.get("approved_hash"), self._user_id)
                if code != 200:
                    self._send(code, result)
                    return
                # Tell CIRO through the SAME path a typed message takes, so the door
                # receives it and the (future) daemon-side admit can act.
                # Text names the shape + title; the daemon reads the plate record for the
                # authoritative approve event (never trusts message text).
                text = ("Plate · " + result["title"] + " → approve ("
                        + result["shape_id"] + ")")
                out = self._tell_cathedral(text, data, {
                    "op": "approve", "id": data.get("id"),
                    "act_ts": result["decision"]["ts"]})
                self._send(200, {"decision": result["decision"], **out})
                return

            if parts == ["plate", "reject"]:
                data, ok = self._read_json()
                if not ok:
                    return
                if self._user_id is None:
                    self._send(403, {"error": "user_token_required"})
                    return
                code, result = plate_mod.reject_prepared(
                    _plate(), data.get("id"), self._user_id)
                if code != 200:
                    self._send(code, result)
                    return
                text = ("Plate · " + result["title"] + " → reject ("
                        + result["shape_id"] + ")")
                out = self._tell_cathedral(text, data, {
                    "op": "reject", "id": data.get("id"),
                    "act_ts": result["decision"]["ts"]})
                self._send(200, {"decision": result["decision"], **out})
                return

            if parts == ["plate", "undo"]:
                # Take back a plate choice. Same auth as /plate/act: a user token only, and only
                # the person who made the act; the record keeps the act and appends the undo.
                data, ok = self._read_json()
                if not ok:
                    return
                if self._user_id is None:
                    self._send(403, {"error": "user_token_required"})
                    return
                code, result = plate_mod.undo(_plate(), data.get("id"), self._user_id,
                                              data.get("act_ts"))
                if code != 200:
                    self._send(code, result)
                    return
                undid = result["undid"]
                was = undid["choice"]
                if undid.get("replaced"):
                    # Undoing a typed re-choose reopens the card; the earlier choice stays undone.
                    was += "; " + undid["replaced"] + " is not restored"
                text = "Plate · " + result["title"] + " → undo (was: " + was + ")"
                out = self._tell_cathedral(text, data, {
                    "op": "undo", "id": result["undo"]["id"], "act_ts": undid["ts"]})
                self._send(200, {"undo": result["undo"], "undid": undid, **out})
                return

            self._send(404, {"error": "not_found"})

        def do_HEAD(self) -> None:  # noqa: N802
            # HEAD = GET minus body. Proxy through do_GET with wfile swapped to a
            # BytesIO sink, then write back only the headers portion. Keeps status
            # code, content-type, and routing identical to GET on every path.
            # Fixes BaseHTTPRequestHandler's default 501-on-HEAD for crawlers/
            # health-checks while visitors (who use GET) are unaffected.
            real_wfile = self.wfile
            buf = io.BytesIO()
            self.wfile = buf  # type: ignore[assignment]
            try:
                self.do_GET()
            finally:
                self.wfile = real_wfile
            data = buf.getvalue()
            sep = b"\r\n\r\n"
            idx = data.find(sep)
            real_wfile.write(data[: idx + len(sep)] if idx >= 0 else data)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.rstrip("/") or "/"
            parts = [p for p in path.split("/") if p]

            # Public console page (no auth): plain HTML that then makes token'd API calls.
            if parts in ([], ["console"]):
                self._send_html(200, _CONSOLE_HTML)
                return

            # /why — the article the landing's link promises. Public, no-auth, holding page
            # until the full piece is written. Honest: 'being written' is better than JSON-401.
            if parts == ["why"]:
                self._send_html(200, _WHY_HOLDING_HTML)
                return

            if not self._authorized():
                return

            if len(parts) == 2 and parts[0] == "run":
                status = manager.status(parts[1], user_id=self._user_id)
                if isinstance(status, dict) and status.get("error") == "unknown_run":
                    self._send(404, status)
                    return
                self._send(200, status)
                return

            if len(parts) == 2 and parts[0] == "thread":
                thread = manager.thread(parts[1], user_id=self._user_id)
                if isinstance(thread, dict) and thread.get("error") == "unknown_conversation":
                    self._send(404, thread)
                    return
                self._send(200, thread)
                return

            if parts == ["plate"]:
                # The fold, filtered to what this caller owns (uniform: another user's plate
                # reads as an empty plate, not a refusal that confirms it exists). The
                # BETA_PAUSED sentinel is reported here so the console can render the
                # maintenance banner on the next poll — the console calls /plate on every
                # visibility-change tick, so pause/unpause reaches the UI quickly.
                view = plate_mod.plate_view(_plate(), self._user_id)
                if _beta_paused():
                    view["beta_paused"] = True
                self._send(200, view)
                return

            self._send(404, {"error": "not_found"})

    return TriggerHandler


def make_server(
    manager: RunManager,
    token: str | None,
    host: str = "127.0.0.1",
    port: int = 0,
    plate_path: str | None = None,
) -> tuple[ThreadingHTTPServer, int]:
    """Bind a trigger server and return (httpd, actual_port) without serving yet."""
    handler = _make_handler(manager, token, plate_path)
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd, httpd.server_address[1]


def main() -> None:
    """Run the trigger service from environment configuration."""
    token = os.environ.get("AGNOSTIC_TRIGGER_TOKEN")
    port = int(os.environ.get("AGNOSTIC_TRIGGER_PORT", "8770"))
    # The deployed server persists transcripts so a conversation survives a restart. A default
    # under the home dir means "always durable" without configuration; the env var can override.
    state_path = os.environ.get("AGNOSTIC_TRIGGER_STATE") or os.path.expanduser("~/.agnostic_runtime/threads.json")
    manager = RunManager(state_path=state_path)
    # host=127.0.0.1: local-only. Exposing beyond localhost is the human's explicit choice.
    httpd, actual_port = make_server(manager, token, host="127.0.0.1", port=port)
    print(f"trigger listening on http://127.0.0.1:{actual_port}")
    print(f"threads persisted to {state_path}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
