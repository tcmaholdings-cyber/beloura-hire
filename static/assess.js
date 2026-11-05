/* Assessment portal client. One visible test: the live fan chats. Everything else is inferred from this one session.
   - A SILENT connection probe runs in the background on load and is POSTed once; there is no visible step and no Continue button.
   - Rich per-reply telemetry is captured for every message: time to first key, typing duration, keystroke and backspace counts,
     an inter-keystroke-interval summary, blocked paste attempts, instant/programmatic inserts, tab-blur count and time, reply latency and length.
   - Replies can be CHUNKED: the applicant sends several short messages in a row; the fan only replies when they pause (idle gap) or press
     "over to them". The reply clock is per fan message (the whole burst), not per chunk. */
(function () {
  'use strict';
  var cfg = JSON.parse(document.getElementById('cfg').textContent);
  var tele = { blur: 0, paste: 0, t0: performance.now() };
  var blurStart = 0;
  window.addEventListener('blur', function () { tele.blur++; blurStart = performance.now(); if (CHUNK) CHUNK.blur++; });
  window.addEventListener('focus', function () { if (blurStart && CHUNK) { CHUNK.blurMs += performance.now() - blurStart; } blurStart = 0; });
  document.addEventListener('visibilitychange', function () { if (document.hidden) { tele.blur++; blurStart = performance.now(); if (CHUNK) CHUNK.blur++; } });

  var CHUNK = null; // the telemetry accumulator for the message currently being typed

  function $(id) { return document.getElementById(id); }
  function status(msg, bad) { var el = $('status'); if (!el) return; el.textContent = msg || ''; el.className = 'status' + (bad ? ' bad' : ''); }
  function telemetry() { return { blur: tele.blur, paste: tele.paste, seconds: Math.round((performance.now() - tele.t0) / 1000) }; }
  function escapeHtml(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function fmtClock(s) { s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2); }

  function blockPaste(el) {
    ['paste', 'drop'].forEach(function (ev) { el.addEventListener(ev, function (e) { e.preventDefault(); tele.paste++; if (CHUNK) CHUNK.paste++; status('Pasting is off for this part. Please type.'); }); });
    el.addEventListener('keydown', function (e) { if ((e.ctrlKey || e.metaKey) && (e.key === 'v' || e.key === 'V' || e.key === 'Insert')) { e.preventDefault(); tele.paste++; if (CHUNK) CHUNK.paste++; status('Pasting is off for this part. Please type.'); } });
    el.addEventListener('contextmenu', function (e) { e.preventDefault(); });
  }
  async function post(url, body) {
    var r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    var j = {}; try { j = await r.json(); } catch (e) { }
    if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
    return j;
  }
  async function submit(payload) {
    payload.telemetry = telemetry();
    var btn = $('next'); if (btn) btn.disabled = true;
    status('Saving');
    try {
      var j = await post(cfg.submit_url, payload);
      if (j.next) location.href = j.next;
    } catch (e) { status('Could not save: ' + e.message + '. Try again.', true); if (btn) btn.disabled = false; }
  }

  /* ---------------------------------------------------------------- silent connection probe */
  async function silentProbe() {
    try {
      var result = { ping: [], down: [], up: [] };
      for (var i = 0; i < cfg.ping_n; i++) {
        var t0 = performance.now();
        var r = await fetch(cfg.ping_url + '?r=' + Math.random(), { cache: 'no-store' }); await r.text();
        result.ping.push(Math.round((performance.now() - t0) * 10) / 10);
      }
      for (var d = 0; d < cfg.down_n; d++) {
        var t1 = performance.now();
        var rb = await fetch(cfg.blob_url + '?bytes=' + cfg.down_bytes + '&r=' + Math.random(), { cache: 'no-store' });
        var buf = await rb.arrayBuffer();
        result.down.push({ bytes: buf.byteLength, ms: Math.round(performance.now() - t1) });
      }
      var payload = new Uint8Array(cfg.up_bytes); for (var k = 0; k < payload.length; k += 4096) payload[k] = (k / 4096) & 255;
      for (var u = 0; u < cfg.up_n; u++) {
        var t2 = performance.now();
        var ru = await fetch(cfg.upload_url + '?r=' + Math.random(), { method: 'POST', body: payload, headers: { 'Content-Type': 'application/octet-stream' }, cache: 'no-store' });
        await ru.json();
        result.up.push({ bytes: payload.length, ms: Math.round(performance.now() - t2) });
      }
      await post(cfg.probe_url, result);
    } catch (e) { /* the probe is best-effort; a failure just leaves connection unknown */ }
  }

  /* ---------------------------------------------------------------- per-chunk telemetry */
  function newChunk(startAt) {
    return { start: startAt, firstKey: 0, lastAt: 0, prevLen: 0, sawKey: false, keystrokes: 0, backspaces: 0,
             intervals: [], lastKeyAt: 0, paste: 0, instant: 0, blur: 0, blurMs: 0 };
  }
  function bindChunkRecorder(el) {
    el.addEventListener('keydown', function (e) {
      if (e.key === 'Tab') { e.preventDefault(); return; }
      if (!CHUNK) return;
      var now = performance.now();
      if (!CHUNK.firstKey) CHUNK.firstKey = now;
      if (CHUNK.lastKeyAt) CHUNK.intervals.push(now - CHUNK.lastKeyAt);
      CHUNK.lastKeyAt = now; CHUNK.lastAt = now; CHUNK.sawKey = true;
      if (e.key === 'Backspace' || e.key === 'Delete') CHUNK.backspaces++;
      else if (e.key && e.key.length === 1) CHUNK.keystrokes++;
    });
    el.addEventListener('input', function () {
      if (!CHUNK) return;
      var now = performance.now(), len = el.value.length, delta = len - CHUNK.prevLen;
      if (delta > 12 && !CHUNK.sawKey) CHUNK.instant++;   // a block of text arrived with no matching keystroke
      CHUNK.sawKey = false; CHUNK.prevLen = len; CHUNK.lastAt = now;
    });
  }
  function summarize(ch, shownAt, length) {
    var iv = ch.intervals, n = iv.length, mean = 0, min = 0, sub50 = 0, sd = 0;
    if (n) {
      var sum = 0, i; for (i = 0; i < n; i++) { sum += iv[i]; if (iv[i] < 50) sub50++; }
      mean = sum / n; min = Math.min.apply(null, iv);
      var v = 0; for (i = 0; i < n; i++) v += (iv[i] - mean) * (iv[i] - mean); sd = Math.sqrt(v / n);
    }
    var typingMs = (ch.firstKey && ch.lastAt) ? Math.max(0, ch.lastAt - ch.firstKey) : 0;
    return {
      ttfk_ms: ch.firstKey ? Math.max(0, Math.round(ch.firstKey - ch.start)) : 0,
      typing_ms: Math.round(typingMs),
      keystrokes: ch.keystrokes, backspaces: ch.backspaces,
      iki: { mean: Math.round(mean), stdev: Math.round(sd), min: Math.round(min), sub50: sub50, count: n },
      paste: ch.paste, instant_inserts: ch.instant, blur: ch.blur, blur_ms: Math.round(ch.blurMs),
      latency_ms: Math.round(performance.now() - shownAt), length: length
    };
  }

  /* ---------------------------------------------------------------- live chats (chunked) */
  function live() {
    var thread = $('thread'), reply = $('reply'), send = $('send'), over = $('over'), clock = $('c-clock'),
        title = $('c-title'), cn = $('c-n'), nextBtn = $('next');
    var state = null, left = 0, timer = null, idle = null, shownAt = 0, busy = false, ended = false, sentAny = false;
    blockPaste(reply);
    bindChunkRecorder(reply);

    function bubble(role, text, cls) {
      var d = document.createElement('div');
      d.className = 'wa-msg ' + (cls || (role === 'fan' ? 'wa-in' : 'wa-out'));
      var t = document.createElement('div'); t.className = 'wa-text'; t.textContent = text || (role === 'applicant' ? '(no reply)' : ''); d.appendChild(t);
      thread.appendChild(d); thread.scrollTop = thread.scrollHeight;
    }
    var scriptSteps = Array.prototype.slice.call(document.querySelectorAll('.script-step'));
    var scriptIdx = 0;   // the ladder is sent in order; this is the next step to send
    function refreshLadder(canSend) {
      scriptSteps.forEach(function (b, i) {
        b.classList.toggle('done', i < scriptIdx);
        b.disabled = !(canSend && i === scriptIdx);
        b.classList.toggle('next', canSend && i === scriptIdx);
      });
    }
    var EMOJIS = ['\uD83D\uDE0A','\uD83D\uDE09','\uD83D\uDE0F','\uD83D\uDE0D','\uD83E\uDD70','\uD83D\uDE18','\uD83D\uDE1A','\uD83D\uDE0B','\uD83D\uDE1C','\uD83D\uDE1D','\uD83E\uDD2D','\uD83D\uDE48','\uD83E\uDD7A','\uD83E\uDD75','\uD83D\uDE33','\uD83D\uDE22','\uD83D\uDE2D','\uD83D\uDE44','\uD83D\uDE02','\uD83D\uDE05','\uD83D\uDE07','\uD83D\uDE08','\uD83D\uDC40','\uD83D\uDD25','\uD83C\uDF51','\uD83C\uDF46','\uD83D\uDCA6','\uD83D\uDC8B','\u2764\uFE0F','\uD83D\uDC97','\uD83D\uDC93','\uD83D\uDC95','\uD83D\uDC9C','\uD83D\uDC9D'];
    var emojibar = document.getElementById('emojibar');
    function insertEmoji(ch) {
      if (reply.disabled) return;
      var a = reply.selectionStart == null ? reply.value.length : reply.selectionStart;
      var b = reply.selectionEnd == null ? reply.value.length : reply.selectionEnd;
      reply.value = reply.value.slice(0, a) + ch + reply.value.slice(b);
      var pos = a + ch.length; reply.selectionStart = reply.selectionEnd = pos;
      if (CHUNK) { CHUNK.keystrokes++; CHUNK.sawKey = true; CHUNK.prevLen = reply.value.length; if (!CHUNK.firstKey) CHUNK.firstKey = performance.now(); CHUNK.lastAt = performance.now(); }
      reply.focus();
      reply.dispatchEvent(new Event('input'));
    }
    if (emojibar) EMOJIS.forEach(function (ch) {
      var b = document.createElement('button'); b.type = 'button'; b.className = 'emoji'; b.textContent = ch; b.tabIndex = -1;
      b.addEventListener('mousedown', function (e) { e.preventDefault(); });
      b.addEventListener('click', function () { insertEmoji(ch); });
      emojibar.appendChild(b);
    });
    var typingEl = null;
    function showTyping() {
      if (typingEl) return;
      typingEl = document.createElement('div');
      typingEl.className = 'wa-msg wa-in wa-typing';
      typingEl.innerHTML = '<div class="wa-text"><span class="dots"><i></i><i></i><i></i></span></div>';
      thread.appendChild(typingEl); thread.scrollTop = thread.scrollHeight;
    }
    function hideTyping() { if (typingEl) { typingEl.remove(); typingEl = null; } }
    function enable(on) { reply.disabled = !on; send.disabled = !on; if (over) over.disabled = !on; refreshLadder(on); if (emojibar) emojibar.classList.toggle('off', !on); if (on) reply.focus(); }
    function clearTimers() { clearInterval(timer); clearTimeout(idle); timer = null; idle = null; }
    function armIdle() { clearTimeout(idle); idle = setTimeout(maybeHandOver, (cfg.chunk_idle_seconds || 5) * 1000); }
    function maybeHandOver() {   // a pause only hands over to the fan when nothing is half-typed; it never auto-sends a draft
      if (busy || ended) return;
      if (reply.value.trim()) { armIdle(); return; }   // still composing: wait, do not send the draft
      endTurn();
    }

    function beginTurn() {
      ended = false; sentAny = false; CHUNK = newChunk(performance.now()); CHUNK.prevLen = reply.value.length;
      enable(true);
      shownAt = performance.now();
      clearInterval(timer); if (clock) { clock.textContent = ''; clock.classList.remove('low'); }
      status('Your reply. Send your messages, then pause or press Over to them.');
    }
    function showScenario(sc, turns) {
      thread.innerHTML = '';
      scriptIdx = 0; refreshLadder(false);   // fresh script run per fan
      title.textContent = 'Chat ' + sc.n + ' of ' + state.total + ': ' + sc.title;
      cn.textContent = 'reply as the creator';
      bubble('sys', 'New chat. The fan is ' + sc.title.toLowerCase() + '.', 'sys');
      (turns || []).forEach(function (t) { bubble(t.role, t.body); });
    }
    async function load() {
      try { state = await fetch(cfg.state_url, { cache: 'no-store' }).then(function (r) { return r.json(); }); }
      catch (e) { status('Could not load the chat: ' + e.message, true); return; }
      if (state.all_done) { finishAll(); return; }
      showScenario(state.scenario, state.turns);
      if (state.your_turn) beginTurn(); else enable(false);
    }
    function finishAll() {
      clearTimers(); clock.textContent = ''; enable(false); $('compose').hidden = true;
      title.textContent = 'All chats done'; cn.textContent = '';
      status('Press Finish to see your result.');
      nextBtn.hidden = false; nextBtn.disabled = false;
      nextBtn.onclick = function () { submit({}); };
    }
    reply.addEventListener('input', function () { if (state && state.your_turn && !busy && !ended) armIdle(); });

    function sendChunk() {   // one message in the burst; the fan stays quiet
      if (busy || ended || reply.disabled) return;
      var text = reply.value.trim();
      if (!text) return;
      var tel = summarize(CHUNK, shownAt, reply.value.length);
      bubble('applicant', reply.value);
      reply.value = ''; CHUNK = newChunk(performance.now());
      sentAny = true; clearTimeout(idle); armIdle();
      post(cfg.reply_url, { scenario: state.scenario.key, text: text, telemetry: tel, latency_ms: tel.latency_ms, end_turn: false })
        .catch(function (e) { status('Could not send: ' + e.message, true); });
    }

    function sendStep(btn) {   // tap the next script step: send its caption + media as content (one chunk, not typing)
      if (busy || ended || reply.disabled || btn.disabled) return;
      var name = btn.getAttribute('data-name'), kind = btn.getAttribute('data-kind'),
          price = btn.getAttribute('data-price'), media = btn.getAttribute('data-media'), caption = btn.getAttribute('data-caption');
      var d = document.createElement('div');
      d.className = 'wa-msg wa-out ' + (kind === 'ppv' ? 'wa-ppv' : 'wa-tease');
      var tag = kind === 'ppv' ? ('\uD83D\uDD12 ' + name + (price ? ' — ' + price : '')) : ('\uD83D\uDCF7 ' + name);
      d.innerHTML = '<div class="wa-text"><span class="ppv-lock">' + escapeHtml(tag) + '</span><span class="cap-sent">' + escapeHtml(caption) + '</span><span class="media-sent">' + escapeHtml(media) + '</span></div>';
      thread.appendChild(d); thread.scrollTop = thread.scrollHeight;
      scriptIdx++; refreshLadder(true);
      sentAny = true; clearTimeout(idle); armIdle();
      post(cfg.reply_url, { scenario: state.scenario.key, content: { name: name, kind: kind, price: price, media: media, caption: caption }, telemetry: { content: true }, end_turn: false })
        .catch(function (e) { status('Could not send: ' + e.message, true); });
    }
    scriptSteps.forEach(function (b) { b.addEventListener('click', function () { sendStep(b); }); });
    async function endTurn() {    // pause (box empty) or over-to-them: the fan (or judge) now responds. Never cuts off typing.
      if (busy || ended || !state || !state.scenario) return;
      ended = true; busy = true; clearTimers(); enable(false);
      var text = reply.value.trim(), raw = reply.value;
      var tel = text ? summarize(CHUNK, shownAt, raw.length) : null;
      if (text) bubble('applicant', raw);
      reply.value = '';
      status('Sent.');
      try {
        showTyping();
        var j = await post(cfg.reply_url, { scenario: state.scenario.key, text: text, telemetry: tel, latency_ms: tel ? tel.latency_ms : null, end_turn: true });
        hideTyping();
        if (!j.done) { bubble('fan', j.fan); busy = false; beginTurn(); }
        else if (j.all_done) { finishAll(); }
        else { bubble('sys', 'Chat over. Next one coming up.', 'sys'); await new Promise(function (r) { setTimeout(r, 1200); }); busy = false; await load(); }
      } catch (e) { hideTyping(); status('Could not send: ' + e.message + '. Reloading.', true); setTimeout(load, 1500); }
      if (!state || state.scenario) busy = false;
    }
    send.onclick = function () { sendChunk(); };
    if (over) over.onclick = function () { endTurn(); };
    reply.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChunk(); } });

    silentProbe();
    load();
  }

  var run = { live: live };
  if (run[cfg.section]) run[cfg.section]();
})();
