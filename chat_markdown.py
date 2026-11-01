# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Markdown in the agent's chat messages (2026-11-01): the model writes **bold**, *italic*, lists, `code`, fenced code blocks, headings, quotes, tables and links;
the chat page now draws them instead of showing the symbols.

One renderer, in the browser (the server sends the message escaped, inside <div class=md>, so without scripts it still reads as plain text). It builds
elements with createElement/textContent only -- never innerHTML -- so nothing in a message can become markup or script, and a link is only made for
http(s):// and mailto: addresses. Only the assistant's messages are rendered; what a person typed stays exactly as typed. History, the copy button
(which copies the original text) and the stored messages are untouched.

window.NoriMd.render(el, text) fills `el`; window.NoriMd.hydrate(root) renders every unrendered `.md` under root."""

MARKDOWN_JS = "<script>" + r"""
(function(){
  var MAX = 100000;
  function el(tag, cls){ var e = document.createElement(tag); if (cls) e.className = cls; return e; }
  function isWord(c){ return !!c && /[A-Za-z0-9]/.test(c); }

  // Inline: `code`, [text](url), bare http(s) URLs, **bold**, __bold__, ~~strike~~, *italic*, _italic_. Spans never cross a line.
  var INLINE = /`([^`\n]+)`|\[([^\]\n]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s<>"']+)|\*\*([^\s*](?:[^\n]*?\S)?)\*\*(?!\*)|__([^\s_](?:[^\n]*?[^\s_])?)__|~~([^\s~](?:[^\n]*?[^\s~])?)~~|\*([^\s*](?:[^*\n]*?[^\s*])?)\*|_([^\s_](?:[^_\n]*?[^\s_])?)_|\*\*\*([^\s*](?:[^\n]*?[^\s*])?)\*\*\*/g;
  var SAFE_URL = /^(https?:\/\/|mailto:)/i;

  function text(parent, s){
    var parts = s.split('\n');
    for (var i = 0; i < parts.length; i++){
      if (i) parent.appendChild(el('br'));
      if (parts[i]) parent.appendChild(document.createTextNode(parts[i]));
    }
  }
  function link(parent, url, label){
    var a = el('a'); a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
    if (typeof label === 'string') a.textContent = label; else a.appendChild(label);
    parent.appendChild(a);
  }
  function inline(parent, s){
    var re = new RegExp(INLINE.source, 'g'), pos = 0, m;
    while ((m = re.exec(s))){
      var i = m.index, end = i + m[0].length, node;
      // _x_ and __x__ only count at word edges, so snake_case_names stay as they are.
      if ((m[6] !== undefined || m[9] !== undefined) && (isWord(s.charAt(i - 1)) || isWord(s.charAt(end)))){ re.lastIndex = i + 1; continue; }
      if (m[2] !== undefined && !SAFE_URL.test(m[3])){ re.lastIndex = i + 1; continue; }
      if (i > pos) text(parent, s.slice(pos, i));
      if (m[1] !== undefined){ node = el('code'); node.textContent = m[1]; parent.appendChild(node); }
      else if (m[2] !== undefined){ node = el('span'); inline(node, m[2]); link(parent, m[3], node); }
      else if (m[4] !== undefined){
        var url = m[4], tail = '';
        while (/[.,;:!?'")\]]$/.test(url)){
          var c = url.charAt(url.length - 1);
          if (c === ')' && url.split('(').length >= url.split(')').length) break;
          tail = c + tail; url = url.slice(0, -1);
        }
        link(parent, url, url); if (tail) text(parent, tail);
      }
      else if (m[5] !== undefined || m[6] !== undefined){ node = el('strong'); inline(node, m[5] !== undefined ? m[5] : m[6]); parent.appendChild(node); }
      else if (m[7] !== undefined){ node = el('del'); inline(node, m[7]); parent.appendChild(node); }
      else if (m[10] !== undefined){ node = el('strong'); var em = el('em'); inline(em, m[10]); node.appendChild(em); parent.appendChild(node); }
      else { node = el('em'); inline(node, m[8] !== undefined ? m[8] : m[9]); parent.appendChild(node); }
      pos = end;
    }
    if (pos < s.length) text(parent, s.slice(pos));
  }

  var FENCE = /^\s*(```|~~~)\s*([\w+#.-]*)\s*$/;
  var HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  var RULE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
  var ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  var QUOTE = /^\s{0,3}>\s?(.*)$/;
  var TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  function indentOf(s){ var n = 0; for (var i = 0; i < s.length; i++){ var c = s.charAt(i); if (c === ' ') n++; else if (c === '\t') n += 4; else break; } return n; }
  function cells(line){
    var s = line.trim();
    if (s.charAt(0) === '|') s = s.slice(1);
    if (s.charAt(s.length - 1) === '|' && s.charAt(s.length - 2) !== '\\') s = s.slice(0, -1);
    var out = [], cur = '';
    for (var i = 0; i < s.length; i++){
      var ch = s.charAt(i);
      if (ch === '\\' && s.charAt(i + 1) === '|'){ cur += '|'; i++; }
      else if (ch === '|'){ out.push(cur.trim()); cur = ''; }
      else cur += ch;
    }
    out.push(cur.trim());
    return out;
  }
  function startsBlock(lines, i){
    var l = lines[i];
    return FENCE.test(l) || HEADING.test(l) || RULE.test(l) || ITEM.test(l) || QUOTE.test(l) ||
      (l.indexOf('|') >= 0 && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]) && lines[i + 1].indexOf('-') >= 0);
  }

  function list(parent, lines, i){
    var first = ITEM.exec(lines[i]), base = indentOf(first[1]), ordered = /\d/.test(first[2].charAt(0));
    var ul = el(ordered ? 'ol' : 'ul');
    if (ordered && parseInt(first[2], 10) !== 1) ul.start = parseInt(first[2], 10);
    while (i < lines.length){
      var m = ITEM.exec(lines[i]);
      if (!m || indentOf(m[1]) !== base || /\d/.test(m[2].charAt(0)) !== ordered) break;
      var li = el('li'), body = [m[3]];
      i++;
      // continuation lines and nested lists belong to this item
      for (;;){
        if (i >= lines.length) break;
        var l = lines[i];
        if (!l.trim()){
          var j = i; while (j < lines.length && !lines[j].trim()) j++;
          if (j < lines.length && indentOf(lines[j]) > base){ i = j; continue; }
          break;
        }
        var ind = indentOf(l), im = ITEM.exec(l);
        if (ind > base && im){
          inline(li, body.join('\n')); body = null;
          i = list(li, lines, i);
          continue;
        }
        if (ind > base && body){ body.push(l.trim()); i++; continue; }
        break;
      }
      if (body) inline(li, body.join('\n'));
      ul.appendChild(li);
      // a blank line between items of the same list does not end it
      var k = i; while (k < lines.length && !lines[k].trim()) k++;
      if (k > i && k < lines.length){ var nm = ITEM.exec(lines[k]); if (nm && indentOf(nm[1]) === base && /\d/.test(nm[2].charAt(0)) === ordered) i = k; }
    }
    parent.appendChild(ul);
    return i;
  }

  function blocks(parent, lines){
    var i = 0;
    while (i < lines.length){
      var l = lines[i], m;
      if (!l.trim()){ i++; continue; }
      if ((m = FENCE.exec(l))){
        var fence = m[1], code = [];
        i++;
        while (i < lines.length && lines[i].trim() !== fence) { code.push(lines[i]); i++; }
        i++;
        var pre = el('pre'), c = el('code');
        if (m[2]) pre.dataset.lang = m[2];
        c.textContent = code.join('\n'); pre.appendChild(c); parent.appendChild(pre);
        continue;
      }
      if ((m = HEADING.exec(l))){
        var h = el('h' + Math.min(m[1].length + 2, 6), 'md-h'); inline(h, m[2]); parent.appendChild(h); i++; continue;
      }
      if (RULE.test(l)){ parent.appendChild(el('hr')); i++; continue; }
      if (QUOTE.test(l)){
        var q = [];
        while (i < lines.length && (m = QUOTE.exec(lines[i]))){ q.push(m[1]); i++; }
        var bq = el('blockquote'); blocks(bq, q); parent.appendChild(bq); continue;
      }
      if (ITEM.test(l)){ i = list(parent, lines, i); continue; }
      if (l.indexOf('|') >= 0 && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]) && lines[i + 1].indexOf('-') >= 0){
        var head = cells(l), aligns = cells(lines[i + 1]).map(function(a){ return /^:-*:$/.test(a) ? 'center' : /-:$/.test(a) ? 'right' : /^:/.test(a) ? 'left' : ''; });
        var wrap = el('div', 'md-table'), t = el('table'), thead = el('thead'), tr = el('tr');
        head.forEach(function(x, n){ var th = el('th'); if (aligns[n]) th.style.textAlign = aligns[n]; inline(th, x); tr.appendChild(th); });
        thead.appendChild(tr); t.appendChild(thead);
        var tb = el('tbody'); i += 2;
        while (i < lines.length && lines[i].trim() && lines[i].indexOf('|') >= 0){
          var row = cells(lines[i]), r = el('tr');
          for (var n = 0; n < head.length; n++){ var td = el('td'); if (aligns[n]) td.style.textAlign = aligns[n]; inline(td, row[n] || ''); r.appendChild(td); }
          tb.appendChild(r); i++;
        }
        t.appendChild(tb); wrap.appendChild(t); parent.appendChild(wrap);
        continue;
      }
      var para = [l]; i++;
      while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)){ para.push(lines[i]); i++; }
      var p = el('p'); inline(p, para.join('\n')); parent.appendChild(p);
    }
  }

  function render(target, source){
    var s = String(source == null ? '' : source).replace(/\r\n?/g, '\n');
    while (target.firstChild) target.removeChild(target.firstChild);
    if (s.length > MAX){ target.textContent = s; target.classList.add('md-plain'); return target; }
    try {
      var frag = document.createDocumentFragment();
      blocks(frag, s.split('\n'));
      target.appendChild(frag);
      target.classList.add('md-done');
    } catch (e) {
      while (target.firstChild) target.removeChild(target.firstChild);
      target.textContent = s;
      target.classList.add('md-plain');
    }
    return target;
  }
  function hydrate(root){
    var nodes = (root || document).querySelectorAll('.md:not(.md-done):not(.md-plain)');
    for (var i = 0; i < nodes.length; i++) render(nodes[i], nodes[i].textContent);
  }
  window.NoriMd = {render: render, hydrate: hydrate};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function(){ hydrate(); }); else hydrate();
})();
</script>"""
