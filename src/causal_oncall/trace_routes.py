"""SSE handler + trace-UI HTML renderer.

Sits between the FastAPI route (in ``app.py``) and the
:class:`TraceBroadcaster`. Two narrow helpers:

* :func:`render_trace_page` returns the HTML string for the single-page
  trace UI. No frameworks, no build step — vanilla HTML + a sprinkle of
  ``EventSource`` JavaScript.
* :func:`stream_sse_for_problem` is the async generator the FastAPI route
  hands to ``StreamingResponse``. Yields one bytes-encoded SSE frame per
  event the broadcaster publishes for the given ``problem_id``.

These functions own the SSE protocol mechanics (frame ids, retry hint)
so the FastAPI route reduces to a one-liner ``return StreamingResponse(...)``
and stays inside the ``# pragma: no cover`` glue layer.
"""

from __future__ import annotations

import html
from collections.abc import AsyncIterator

from causal_oncall.trace_broadcaster import TraceBroadcaster

#: SSE clients use the ``retry:`` directive to set their reconnect delay
#: in milliseconds. 3s is long enough that a transient blip doesn't
#: thrash, short enough that the viewer doesn't notice a hiccup.
_SSE_RETRY_MS = 3000


def render_trace_page(problem_id: str) -> str:
    """Render the single-page HTML trace UI for one problem id."""
    safe_id = html.escape(problem_id, quote=True)
    stream_url = f"/webhook/dynatrace-problem/stream/{safe_id}"
    # Tightly-scoped HTML — vanilla, no external deps, works behind a
    # corporate proxy that blocks CDNs.
    return _TRACE_HTML_TEMPLATE.format(safe_id=safe_id, stream_url=stream_url)


async def stream_sse_for_problem(
    broadcaster: TraceBroadcaster, problem_id: str
) -> AsyncIterator[bytes]:
    """Yield SSE frames for every event published on ``problem_id``."""
    event_id = 0
    retry_directive = f"retry: {_SSE_RETRY_MS}\n".encode()
    async for event in broadcaster.subscribe(problem_id):
        event_id += 1
        frame = event.to_sse(event_id=event_id).encode("utf-8")
        if event_id == 1:
            # Combine the retry hint with the first event so the client
            # gets backoff guidance in the same chunk.
            yield retry_directive + frame
        else:
            yield frame


_TRACE_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Causal On-Call - trace {safe_id}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    background: #0d1117; color: #c9d1d9; margin: 0; padding: 24px;
  }}
  h1 {{ color: #58a6ff; font-size: 18px; margin: 0 0 4px; }}
  .meta {{ color: #8b949e; margin-bottom: 24px; font-size: 12px; }}
  .row {{
    border-left: 3px solid #30363d; padding: 8px 12px; margin: 6px 0;
    background: #161b22; border-radius: 0 6px 6px 0;
  }}
  .row.dispatched {{ border-left-color: #f78166; }}
  .row.completed {{ border-left-color: #56d364; }}
  .row.brief {{ border-left-color: #58a6ff; background: #1c2128; }}
  .row.memory {{ border-left-color: #d29922; }}
  .row.start {{ border-left-color: #8b949e; }}
  .row .kind {{ color: #8b949e; font-size: 11px; text-transform: uppercase; }}
  .row pre {{ margin: 4px 0 0; white-space: pre-wrap; word-break: break-all; }}
</style>
</head>
<body>
<h1>Causal On-Call live trace</h1>
<div class="meta">problem_id = {safe_id}</div>
<div id="events"></div>
<script>
(function () {{
  var sink = document.getElementById('events');
  var src = new EventSource('{stream_url}');
  function append(kind, data) {{
    var cls = 'row';
    if (kind === 'specialist-dispatched') cls += ' dispatched';
    else if (kind === 'specialist-completed') cls += ' completed';
    else if (kind === 'brief-ready') cls += ' brief';
    else if (kind === 'memory-short-circuit') cls += ' memory';
    else if (kind === 'orchestrator-started') cls += ' start';
    var div = document.createElement('div');
    div.className = cls;
    div.innerHTML = '<span class="kind">' + kind + '</span>' +
                    '<pre>' + JSON.stringify(data, null, 2) + '</pre>';
    sink.appendChild(div);
  }}
  ['orchestrator-started','specialist-dispatched','specialist-completed',
   'synthesizer-started','brief-ready','memory-short-circuit','error'].forEach(function (k) {{
    src.addEventListener(k, function (ev) {{
      try {{ append(k, JSON.parse(ev.data)); }}
      catch (e) {{ append(k, {{raw: ev.data}}); }}
      if (k === 'brief-ready') src.close();
    }});
  }});
  src.onerror = function () {{
    // Server closed cleanly after brief-ready -> nothing to do.
  }};
}})();
</script>
</body>
</html>
"""
