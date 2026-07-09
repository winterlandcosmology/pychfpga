import html
import json


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE_HTML__</title>
  <style>
    :root {
      --bg: #0b1020;
      --panel: rgba(15, 23, 42, 0.88);
      --panel-border: rgba(148, 163, 184, 0.22);
      --panel-strong: rgba(30, 41, 59, 0.94);
      --text: #e2e8f0;
      --muted: #94a3b8;
      --ok: #34d399;
      --warn: #f59e0b;
      --bad: #f87171;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font-family: "Segoe UI", "Helvetica Neue", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(125, 211, 252, 0.18), transparent 32%),
        radial-gradient(circle at top right, rgba(245, 158, 11, 0.10), transparent 24%),
        linear-gradient(180deg, #08101e 0%, #0f172a 100%);
    }

    main {
      max-width: 1800px;
      margin: 0 auto;
      padding: 24px;
    }

    .hero {
      display: grid;
      grid-template-columns: minmax(0, 1.8fr) minmax(320px, 1fr);
      gap: 16px;
      margin-bottom: 18px;
    }

    .hero-panel,
    .status-panel,
    .trace-card {
      background: var(--panel);
      border: 1px solid var(--panel-border);
      border-radius: 18px;
      box-shadow: 0 18px 50px rgba(2, 6, 23, 0.35);
      backdrop-filter: blur(12px);
    }

    .hero-panel {
      padding: 22px 24px;
    }

    .hero-panel h1 {
      margin: 0 0 8px;
      font-size: clamp(2rem, 4vw, 3.2rem);
      line-height: 1;
      letter-spacing: -0.04em;
    }

    .hero-panel p {
      margin: 0;
      max-width: 60ch;
      color: var(--muted);
      line-height: 1.5;
    }

    .status-panel {
      padding: 18px 20px;
      display: grid;
      gap: 12px;
      align-content: start;
    }

    .selector-card {
      display: grid;
      gap: 8px;
      padding: 12px;
      border-radius: 14px;
      background: var(--panel-strong);
      border: 1px solid rgba(148, 163, 184, 0.16);
    }

    .selector-label {
      font-size: 0.78rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .selector-input {
      width: 100%;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      background: rgba(15, 23, 42, 0.92);
      color: var(--text);
      font: inherit;
    }

    .status-title {
      margin: 0;
      font-size: 0.8rem;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: var(--muted);
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.9);
      border: 1px solid rgba(148, 163, 184, 0.2);
      font-weight: 600;
    }

    .status-pill::before {
      content: "";
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--warn);
      box-shadow: 0 0 0 4px rgba(245, 158, 11, 0.15);
    }

    .status-pill.live::before {
      background: var(--ok);
      box-shadow: 0 0 0 4px rgba(52, 211, 153, 0.15);
    }

    .status-pill.stale::before {
      background: var(--warn);
    }

    .status-pill.error::before {
      background: var(--bad);
      box-shadow: 0 0 0 4px rgba(248, 113, 113, 0.15);
    }

    .stat-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }

    .stat-card {
      padding: 12px;
      border-radius: 14px;
      background: var(--panel-strong);
      border: 1px solid rgba(148, 163, 184, 0.16);
    }

    .stat-card .label {
      font-size: 0.78rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .stat-card .value {
      margin-top: 8px;
      font-size: 1rem;
      font-weight: 600;
      word-break: break-word;
    }

    #grid {
      display: grid;
      grid-template-columns: repeat(__GRID_COLUMNS__, minmax(0, 1fr));
      gap: 14px;
    }

    .trace-card {
      padding: 12px;
    }

    .trace-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 10px;
      gap: 8px;
    }

    .trace-name {
      font-weight: 700;
      letter-spacing: 0.04em;
    }

    .trace-count {
      color: var(--muted);
      font-size: 0.86rem;
    }

    canvas {
      width: 100%;
      height: 148px;
      display: block;
      border-radius: 12px;
      background: linear-gradient(180deg, rgba(15, 23, 42, 0.96) 0%, rgba(17, 24, 39, 0.96) 100%);
      border: 1px solid rgba(148, 163, 184, 0.12);
    }

    .trace-meta {
      display: flex;
      justify-content: space-between;
      margin-top: 10px;
      color: var(--muted);
      font-size: 0.82rem;
    }

    @media (max-width: 1320px) {
      #grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }

    @media (max-width: 900px) {
      .hero {
        grid-template-columns: 1fr;
      }

      #grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <article class="hero-panel">
        <h1>__TITLE_HTML__</h1>
        <p>__DESCRIPTION_HTML__</p>
      </article>
      <aside class="status-panel">
        <p class="status-title">__STATUS_TITLE_HTML__</p>
        <div id="status-pill" class="status-pill">Starting</div>
        <div id="board-selector-wrap" class="selector-card" hidden>
          <label class="selector-label" for="board-select">Board</label>
          <select id="board-select" class="selector-input"></select>
        </div>
        <div class="stat-grid">
          <div class="stat-card">
            <div class="label">Last Update</div>
            <div id="status-age" class="value">-</div>
          </div>
          <div class="stat-card">
            <div class="label">Timestamp</div>
            <div id="status-ts" class="value">-</div>
          </div>
          <div class="stat-card">
            <div class="label">Frame Counts</div>
            <div id="status-counts" class="value">-</div>
          </div>
          <div class="stat-card">
            <div class="label">Acquisition</div>
            <div id="status-error" class="value">Waiting for first frame</div>
          </div>
        </div>
      </aside>
    </section>
    <section id="grid"></section>
  </main>

  <script>
    const pollIntervalMs = __POLL_INTERVAL_MS__;
    const gridColumns = __GRID_COLUMNS__;
    const reverseWithinRows = __REVERSE_WITHIN_ROWS__;
    const colorByRow = __COLOR_BY_ROW__;
    const traceRangeLabel = __TRACE_RANGE_LABEL_JSON__;
    const grid = document.getElementById('grid');
    const statusPill = document.getElementById('status-pill');
    const boardSelectorWrap = document.getElementById('board-selector-wrap');
    const boardSelect = document.getElementById('board-select');
    const statusAge = document.getElementById('status-age');
    const statusTimestamp = document.getElementById('status-ts');
    const statusCounts = document.getElementById('status-counts');
    const statusError = document.getElementById('status-error');
    const colors = ['#7dd3fc', '#f59e0b', '#c084fc', '#34d399'];
    let cards = [];
    let selectedBoardKey = new URL(window.location.href).searchParams.get('board');
    let boardOptionsSignature = '';

    function updateBoardUrl(boardKey) {
      const url = new URL(window.location.href);
      if (boardKey) {
        url.searchParams.set('board', boardKey);
      } else {
        url.searchParams.delete('board');
      }
      window.history.replaceState({}, '', url);
    }

    boardSelect.addEventListener('change', () => {
      selectedBoardKey = boardSelect.value || null;
      updateBoardUrl(selectedBoardKey);
      refresh();
    });

    function buildDisplayOrder(count) {
      const order = [];
      for (let rowStart = 0; rowStart < count; rowStart += gridColumns) {
        const row = [];
        for (let offset = 0; offset < gridColumns && rowStart + offset < count; offset += 1) {
          row.push(rowStart + offset);
        }
        if (reverseWithinRows) {
          row.reverse();
        }
        order.push(...row);
      }
      return order;
    }

    function traceColor(displayIndex) {
      const colorIndex = colorByRow ? Math.floor(displayIndex / gridColumns) : displayIndex % gridColumns;
      return colors[colorIndex % colors.length];
    }

    function ensureCards(names) {
      if (cards.length === names.length) {
        return;
      }

      grid.innerHTML = '';
      const order = buildDisplayOrder(names.length);
      cards = order.map((sourceIndex, displayIndex) => {
        const name = names[sourceIndex];
        const card = document.createElement('article');
        card.className = 'trace-card';

        const head = document.createElement('div');
        head.className = 'trace-head';

        const title = document.createElement('div');
        title.className = 'trace-name';
        title.textContent = name;

        const count = document.createElement('div');
        count.className = 'trace-count';
        count.textContent = 'Count -';

        head.appendChild(title);
        head.appendChild(count);

        const canvas = document.createElement('canvas');
        canvas.width = 480;
        canvas.height = 180;

        const meta = document.createElement('div');
        meta.className = 'trace-meta';

        const leftMeta = document.createElement('span');
        leftMeta.textContent = traceRangeLabel;

        const rightMeta = document.createElement('span');
        rightMeta.textContent = `Trace ${displayIndex + 1}`;

        meta.appendChild(leftMeta);
        meta.appendChild(rightMeta);

        card.appendChild(head);
        card.appendChild(canvas);
        card.appendChild(meta);
        grid.appendChild(card);

        return {
          sourceIndex,
          canvas,
          ctx: canvas.getContext('2d'),
          count,
        };
      });
    }

    function setStatus(payload) {
      let statusClass = 'status-pill';
      let statusText = 'Waiting for data';

      if (payload.error) {
        statusClass += ' error';
        statusText = 'Acquisition error';
      } else if (!payload.has_data) {
        statusClass += ' stale';
        statusText = 'Starting';
      } else if (payload.age_s !== null && payload.age_s > Math.max(2.5, (pollIntervalMs / 1000) * 2)) {
        statusClass += ' stale';
        statusText = 'Stale data';
      } else {
        statusClass += ' live';
        statusText = 'Live';
      }

      statusPill.className = statusClass;
      statusPill.textContent = statusText;
      statusAge.textContent = payload.age_s === null ? '-' : `${payload.age_s.toFixed(2)} s`;
      statusTimestamp.textContent = payload.timestamp === null ? '-' : String(payload.timestamp);
      statusCounts.textContent = payload.counts.length ? payload.counts.join(', ') : '-';
      statusError.textContent = payload.error || (payload.has_data ? 'Receiving frames' : 'Waiting for first frame');
    }

    function syncBoardSelector(payload) {
      const boards = Array.isArray(payload.boards) ? payload.boards : [];

      if (boards.length === 0) {
        boardSelectorWrap.hidden = true;
        boardSelect.innerHTML = '';
        selectedBoardKey = null;
        return;
      }

      boardSelectorWrap.hidden = false;
      const signature = JSON.stringify(boards.map((board) => [board.key, board.label]));
      if (signature !== boardOptionsSignature) {
        boardOptionsSignature = signature;
        boardSelect.innerHTML = '';
        boards.forEach((board) => {
          const option = document.createElement('option');
          option.value = board.key;
          option.textContent = board.label;
          boardSelect.appendChild(option);
        });
      }

      const knownBoardKeys = new Set(boards.map((board) => board.key));
      if (!selectedBoardKey || !knownBoardKeys.has(selectedBoardKey)) {
        selectedBoardKey = payload.selected_board || boards[0].key;
      }

      if (boardSelect.value !== selectedBoardKey) {
        boardSelect.value = selectedBoardKey;
      }
      updateBoardUrl(selectedBoardKey);
    }

    function drawTrace(card, data, color) {
      const canvas = card.canvas;
      const ctx = card.ctx;
      const width = canvas.width;
      const height = canvas.height;
      const left = 10;
      const top = 8;
      const plotWidth = width - left * 2;
      const plotHeight = height - top * 2;
      const midY = top + plotHeight / 2;

      ctx.clearRect(0, 0, width, height);

      ctx.strokeStyle = 'rgba(148, 163, 184, 0.12)';
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i += 1) {
        const y = top + (plotHeight * i) / 4;
        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(left + plotWidth, y);
        ctx.stroke();
      }

      ctx.strokeStyle = 'rgba(125, 211, 252, 0.22)';
      ctx.beginPath();
      ctx.moveTo(left, midY);
      ctx.lineTo(left + plotWidth, midY);
      ctx.stroke();

      if (!Array.isArray(data) || data.length === 0) {
        return;
      }

      ctx.strokeStyle = color;
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      for (let i = 0; i < data.length; i += 1) {
        const x = left + (plotWidth * i) / Math.max(1, data.length - 1);
        const normalized = (Number(data[i]) + 128) / 255;
        const y = top + (1 - normalized) * plotHeight;
        if (i === 0) {
          ctx.moveTo(x, y);
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.stroke();
    }

    async function refresh() {
      try {
        const latestUrl = selectedBoardKey
          ? `/api/latest?board=${encodeURIComponent(selectedBoardKey)}`
          : '/api/latest';
        const response = await fetch(latestUrl, { cache: 'no-store' });
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }

        const payload = await response.json();
        syncBoardSelector(payload);
        ensureCards(payload.input_names || []);
        setStatus(payload);

        cards.forEach((card, displayIndex) => {
          const sourceIndex = card.sourceIndex;
          const channelData = (payload.data || [])[sourceIndex] || [];
          card.count.textContent = `Count ${payload.counts[sourceIndex] ?? '-'}`;
          drawTrace(card, channelData, traceColor(displayIndex));
        });
      } catch (error) {
        statusPill.className = 'status-pill error';
        statusPill.textContent = 'Disconnected';
        statusAge.textContent = '-';
        statusError.textContent = error.message;
      }
    }

    refresh();
    setInterval(refresh, pollIntervalMs);
  </script>
</body>
</html>
"""


def build_waveform_monitor_html(
    title,
    description,
    poll_interval_ms,
    trace_range_label='Range -128..127',
    status_title='Monitor Status',
    columns=4,
    reverse_within_rows=False,
    color_by_row=False,
):
    rendered = HTML_TEMPLATE
    replacements = {
        '__TITLE_HTML__': html.escape(title),
        '__DESCRIPTION_HTML__': html.escape(description),
        '__STATUS_TITLE_HTML__': html.escape(status_title),
        '__POLL_INTERVAL_MS__': str(int(poll_interval_ms)),
        '__GRID_COLUMNS__': str(int(columns)),
        '__REVERSE_WITHIN_ROWS__': json.dumps(bool(reverse_within_rows)),
        '__COLOR_BY_ROW__': json.dumps(bool(color_by_row)),
        '__TRACE_RANGE_LABEL_JSON__': json.dumps(trace_range_label),
    }

    for token, value in replacements.items():
        rendered = rendered.replace(token, value)

    return rendered
