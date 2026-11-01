# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Markdown in chat messages (chat_markdown.py, 2026-11-01). The renderer is browser JavaScript, so this runs the real script under node against a
tiny stand-in DOM and compares the HTML it builds. Also checks the server wires it up: assistant messages are wrapped for it, what a person typed is
not, and nothing a message says can become markup."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_markdown_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_markdown_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import chat_markdown  # noqa: E402

HARNESS = r"""
const fs = require('fs');
class Text { constructor(t){ this.nodeType = 3; this.data = t; } }
class El {
  constructor(tag){ this.nodeType = 1; this.tagName = tag; this.children = []; this.attrs = {}; this.dataset = {}; this.style = {}; this.className = '';
    const self = this; this.classList = { add(c){ if (!self.className.split(' ').includes(c)) self.className = (self.className + ' ' + c).trim(); } }; }
  get firstChild(){ return this.children[0] || null; }
  appendChild(c){ if (c.isFrag){ c.children.forEach(x => this.children.push(x)); c.children = []; } else this.children.push(c); return c; }
  removeChild(c){ this.children.splice(this.children.indexOf(c), 1); return c; }
  set textContent(t){ this.children = t === '' ? [] : [new Text(String(t))]; }
  get textContent(){ return this.children.map(c => c.nodeType === 3 ? c.data : c.textContent).join(''); }
  set href(v){ this.attrs.href = v; } set target(v){ this.attrs.target = v; } set rel(v){ this.attrs.rel = v; } set start(v){ this.attrs.start = v; }
  setAttribute(k, v){ this.attrs[k] = v; }
}
const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
function html(n){
  if (n.nodeType === 3) return esc(n.data);
  let a = '';
  if (n.className) a += ' class="' + n.className + '"';
  for (const k of Object.keys(n.attrs)) a += ' ' + k + '="' + esc(String(n.attrs[k])) + '"';
  for (const k of Object.keys(n.dataset)) a += ' data-' + k + '="' + esc(String(n.dataset[k])) + '"';
  if (n.style.textAlign) a += ' align="' + n.style.textAlign + '"';
  if (['br', 'hr'].includes(n.tagName)) return '<' + n.tagName + a + '>';
  return '<' + n.tagName + a + '>' + n.children.map(html).join('') + '</' + n.tagName + '>';
}
global.window = {};
global.document = {
  readyState: 'complete',
  createElement: t => new El(t),
  createTextNode: t => new Text(t),
  createDocumentFragment: () => { const f = new El('#frag'); f.isFrag = true; return f; },
  querySelectorAll: () => [], addEventListener(){},
};
eval(fs.readFileSync(process.argv[2], 'utf8'));
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = cases.map(src => { const e = new El('div'); window.NoriMd.render(e, src); return { html: html(e), cls: e.className }; });
process.stdout.write(JSON.stringify(out));
"""


def render(*sources):
    d = tempfile.mkdtemp(prefix="md_run_")
    js = chat_markdown.MARKDOWN_JS
    js = js[len("<script>"):-len("</script>")]
    paths = {}
    for name, content in (("h.js", HARNESS), ("md.js", js), ("cases.json", json.dumps(list(sources)))):
        paths[name] = os.path.join(d, name)
        with open(paths[name], "w", encoding="utf-8") as f:
            f.write(content)
    r = subprocess.run(["node", paths["h.js"], paths["md.js"], paths["cases.json"]], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return [x["html"] for x in json.loads(r.stdout)]


def one(src):
    return render(src)[0]


try:
    _NODE = subprocess.run(["node", "--version"], capture_output=True).returncode == 0
except FileNotFoundError:
    _NODE = False


@unittest.skipUnless(_NODE, "node isn't available")
class TheRenderer(unittest.TestCase):
    def test_inline_styles(self):
        self.assertEqual(one("a **bold** and *italic* and ~~gone~~ and `code`"),
                         "<div class=\"md-done\"><p>a <strong>bold</strong> and <em>italic</em> and <del>gone</del> and <code>code</code></p></div>")
        self.assertEqual(one("__bold__ and _italic_"), "<div class=\"md-done\"><p><strong>bold</strong> and <em>italic</em></p></div>")
        self.assertIn("<strong>a <em>b</em></strong>", one("**a *b***"))
        self.assertIn("<strong><em>both</em></strong>", one("***both***"))

    def test_symbols_that_are_not_markup_stay_as_they_are(self):
        self.assertEqual(one("snake_case_name and my_var_2"), "<div class=\"md-done\"><p>snake_case_name and my_var_2</p></div>")
        self.assertEqual(one("2 * 3 * 4 = 24"), "<div class=\"md-done\"><p>2 * 3 * 4 = 24</p></div>")
        self.assertEqual(one("a ** b"), "<div class=\"md-done\"><p>a ** b</p></div>")
        self.assertEqual(one("an unclosed **bold"), "<div class=\"md-done\"><p>an unclosed **bold</p></div>")
        self.assertEqual(one("price is $5 & <b>not bold</b>"), "<div class=\"md-done\"><p>price is $5 &amp; &lt;b&gt;not bold&lt;/b&gt;</p></div>")

    def test_code_is_not_formatted_inside(self):
        self.assertEqual(one("use `**not bold**` here"), "<div class=\"md-done\"><p>use <code>**not bold**</code> here</p></div>")
        self.assertEqual(one("```py\nx = [1, 2]  # *no* **style**\n  indented\n```"),
                         "<div class=\"md-done\"><pre data-lang=\"py\"><code>x = [1, 2]  # *no* **style**\n  indented</code></pre></div>")
        self.assertEqual(one("```\n<script>alert(1)</script>\n```"), "<div class=\"md-done\"><pre><code>&lt;script&gt;alert(1)&lt;/script&gt;</code></pre></div>")
        self.assertIn("<pre><code>unclosed", one("```\nunclosed"))

    def test_lists(self):
        self.assertEqual(one("- one\n- two\n- three"), "<div class=\"md-done\"><ul><li>one</li><li>two</li><li>three</li></ul></div>")
        self.assertEqual(one("1. a\n2. b"), "<div class=\"md-done\"><ol><li>a</li><li>b</li></ol></div>")
        self.assertIn("<ol start=\"3\">", one("3. c\n4. d"))
        self.assertEqual(one("- a\n  - b\n  - c\n- d"), "<div class=\"md-done\"><ul><li>a<ul><li>b</li><li>c</li></ul></li><li>d</li></ul></div>")
        self.assertEqual(one("* a\n\n* b"), "<div class=\"md-done\"><ul><li>a</li><li>b</li></ul></div>", "a blank line between items keeps one list")
        self.assertEqual(one("intro:\n- a\n- b\nafter"), "<div class=\"md-done\"><p>intro:</p><ul><li>a</li><li>b</li></ul><p>after</p></div>")
        self.assertEqual(one("- **Name**: the thing\n- *Other*: more"), "<div class=\"md-done\"><ul><li><strong>Name</strong>: the thing</li><li><em>Other</em>: more</li></ul></div>")

    def test_headings_quotes_rules(self):
        self.assertEqual(one("# Big\n## Smaller\ntext"), "<div class=\"md-done\"><h3 class=\"md-h\">Big</h3><h4 class=\"md-h\">Smaller</h4><p>text</p></div>")
        self.assertEqual(one("> quoted *text*\n> more"), "<div class=\"md-done\"><blockquote><p>quoted <em>text</em><br>more</p></blockquote></div>")
        self.assertEqual(one("a\n\n---\n\nb"), "<div class=\"md-done\"><p>a</p><hr><p>b</p></div>")

    def test_paragraphs_keep_their_line_breaks(self):
        self.assertEqual(one("line one\nline two\n\nnew paragraph"), "<div class=\"md-done\"><p>line one<br>line two</p><p>new paragraph</p></div>")
        self.assertEqual(one(""), "<div class=\"md-done\"></div>")

    def test_tables(self):
        self.assertEqual(one("| Name | Qty |\n|---|--:|\n| **a** | 1 |\n| b | 22 |"),
                         "<div class=\"md-done\"><div class=\"md-table\"><table><thead><tr><th>Name</th><th align=\"right\">Qty</th></tr></thead>"
                         "<tbody><tr><td><strong>a</strong></td><td align=\"right\">1</td></tr><tr><td>b</td><td align=\"right\">22</td></tr></tbody></table></div></div>")
        self.assertEqual(one("a | b is not a table"), "<div class=\"md-done\"><p>a | b is not a table</p></div>")

    def test_links_only_for_safe_addresses(self):
        self.assertEqual(one("[docs](https://example.com/a?b=1)"),
                         "<div class=\"md-done\"><p><a href=\"https://example.com/a?b=1\" target=\"_blank\" rel=\"noopener noreferrer\"><span>docs</span></a></p></div>")
        out = one("[x](javascript:alert(1)) [y](data:text/html,hi) [z](//evil.example)")
        self.assertNotIn("<a ", out)
        self.assertNotIn("href", out)
        self.assertIn("mailto:a@b.co", one("[mail](mailto:a@b.co)"))
        bare = one("see https://example.com/x, then (https://example.com/y).")
        self.assertIn("href=\"https://example.com/x\"", bare)
        self.assertIn("href=\"https://example.com/y\"", bare)
        self.assertIn(", then (", bare)
        self.assertIn("href=\"https://en.wikipedia.org/wiki/A_(b)\"", one("https://en.wikipedia.org/wiki/A_(b)"))

    def test_nothing_in_a_message_becomes_markup(self):
        hostile = ["<img src=x onerror=alert(1)>", "<script>alert(1)</script>", "**<b onclick=x>**", "[<i>x</i>](https://e.co)", "`<u>`", "| <a> |\n|--|\n| <s> |",
                   "- <iframe src=x>", "# <h1>", "> <p>", "\"quote' & amp;"]
        for src in hostile:
            out = one(src)
            for tag in ("<img", "<script", "<iframe", "<b ", "<u>", "<s>", "<a>", "<i>", "<h1>", "<p>x"):
                self.assertNotIn(tag, out, (src, out))

    def test_odd_input_does_not_hang_or_crash(self):
        nasty = ["*" * 5000, "_" * 5000, "**a" * 2000, "[a](" * 2000, "- " * 3000, "| a " * 2000, "`" * 5001, ("> " * 50) + "x", "\n" * 3000, "1. a\n" + "  " * 300 + "- b"]
        for src, out in zip(nasty, render(*nasty)):
            self.assertTrue(out.startswith("<div"), src[:20])

    def test_a_long_realistic_reply(self):
        reply = ("Here's the plan for **Saturday**:\n\n1. Pick up the *groceries*\n2. Fix the `nginx` config:\n\n```bash\nsudo nginx -t\nsudo systemctl reload nginx\n```\n\n"
                 "| Day | Task |\n|---|---|\n| Sat | Groceries |\n\n> Remember: the CA expires soon.\n\n- done\n- [link](https://example.com)\n")
        out = one(reply)
        for part in ("<strong>Saturday</strong>", "<ol><li>Pick up the <em>groceries</em></li>", "<code>nginx</code>", "<pre data-lang=\"bash\">", "<table>", "<blockquote>", "<ul><li>done</li>"):
            self.assertIn(part, out)


class TheServerSide(unittest.TestCase):
    def test_the_script_is_a_single_script_tag_that_parses(self):
        js = chat_markdown.MARKDOWN_JS
        self.assertTrue(js.startswith("<script>") and js.endswith("</script>"))
        self.assertEqual(js.count("</script>"), 1)
        self.assertNotIn("innerHTML", js, "built with createElement/textContent only")
        self.assertNotIn("insertAdjacentHTML", js)

    def test_the_chat_page_wires_it_for_assistant_messages_only(self):
        import server
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("extra_js=MARKDOWN_JS + CHAT_JS", src)
        self.assertIn('<div class=md>{esc(m[\'content\'])}</div>" if m["role"] == "assistant"', src)
        live = server.CHAT_JS
        self.assertIn("m.role==='assistant'&&m.kind!=='persona_draft'&&window.NoriMd", live)
        self.assertIn(".md.md-done{white-space:normal", server.CSS if hasattr(server, "CSS") else src)


if __name__ == "__main__":
    unittest.main()
