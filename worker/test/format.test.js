import test from 'node:test';
import assert from 'node:assert/strict';
import { answerLayout, markdownToHtml, previewHtml, htmlToText, safeUrl } from '../src/format.js';

export function assertBalanced(html) {
  const stack = [];
  for (const match of html.matchAll(/<(\/?)(\w+)(?:\s[^>]*)?>/g)) {
    const [, closing, tag] = match;
    assert.ok(['b', 'i', 'pre', 'code', 'a', 'blockquote'].includes(tag), tag);
    if (closing) assert.equal(stack.pop(), tag, html);
    else stack.push(tag);
  }
  assert.deepEqual(stack, [], html);
}

test('source HTML and entities are escaped before Markdown conversion', () => {
  assert.equal(markdownToHtml('<script>a & b</script> &lt;b&gt;'), '&lt;script&gt;a &amp; b&lt;/script&gt; &amp;lt;b&amp;gt;');
  assert.equal(markdownToHtml('**<candidate> & result**'), '<b>&lt;candidate&gt; &amp; result</b>');
});

test('fences omit the language and protect stars, underscores, HTML and newlines', () => {
  assert.equal(markdownToHtml('```python\nx = a * b\nif x < 4: foo_bar & baz\n```'), '<pre><code>x = a * b\nif x &lt; 4: foo_bar &amp; baz</code></pre>');
  assert.equal(markdownToHtml('```js\n**unclosed fence**'), '<pre><code>**unclosed fence**</code></pre>');
  assert.equal(markdownToHtml('`a_b * c < 4` and _yes_'), '<code>a_b * c &lt; 4</code> and <i>yes</i>');
  assert.equal(markdownToHtml('``a `literal` * b``'), '<code>a `literal` * b</code>');
});

test('nested emphasis and malformed delimiters always produce balanced tags', () => {
  assert.equal(markdownToHtml('**bold *italic* end**'), '<b>bold <i>italic</i> end</b>');
  assert.equal(markdownToHtml('***both***'), '<b><i>both</i></b>');
  assert.equal(markdownToHtml('__bold__ _italic_ *also*'), '<b>bold</b> <i>italic</i> <i>also</i>');
  assert.equal(markdownToHtml('foo_bar_baz foo__bar__baz'), 'foo_bar_baz foo__bar__baz');
  for (const input of ['**unclosed', '*a **b* c**', '**a _b** c_', '### **broken', '*'.repeat(500), '*a '.repeat(1000)]) {
    assertBalanced(markdownToHtml(input));
  }
  const codeInBold = markdownToHtml('**before `a_b` after**');
  assertBalanced(codeInBold);
  assert.doesNotMatch(codeInBold, /<b>[^<]*<code>/);
  assert.equal(markdownToHtml('**[before `code` after](https://example.com)**'), '<b><a href="https://example.com">before </a></b><code>code</code><b><a href="https://example.com"> after</a></b>');
  assert.equal(markdownToHtml('# **Title** with `code`'), '<b>Title with </b><code>code</code>');
});

test('links accept only http/https and escape quoted attributes and label HTML', () => {
  assert.equal(markdownToHtml('[<site>](https://example.com/?a=1&b="x")'), '<a href="https://example.com/?a=1&amp;b=&quot;x&quot;">&lt;site&gt;</a>');
  assert.equal(markdownToHtml('[**docs**](http://example.com)'), '<a href="http://example.com"><b>docs</b></a>');
  for (const url of ['javascript:alert', 'data:text/html,hello', 'tg://user?id=42', '//example.com', 'https://', 'https://example.com/\npath']) {
    assert.equal(safeUrl(url), null);
    assert.doesNotMatch(markdownToHtml(`[click](${url})`), /<a /);
  }
});

test('headings, unordered lists and consecutive quote lines use supported HTML', () => {
  assert.equal(markdownToHtml('# One\n## Two\n### Three\n- first\n* second\n> **quote**\n> continuation'), '<b>One</b>\n<b>Two</b>\n<b>Three</b>\n• first\n• second\n<blockquote><b>quote</b>\ncontinuation</blockquote>');
});

test('long answers use rendered length and previews prefer paragraph boundaries', () => {
  assert.equal(answerLayout('x'.repeat(3500)).attachment, null);
  const markdown = '**' + 'a'.repeat(900) + '**\n\n' + 'b'.repeat(3000);
  const layout = answerLayout(markdown);
  assert.equal(layout.attachment, markdown);
  assert.equal(layout.html, '<b>' + 'a'.repeat(900) + '</b>\n\n<i>…full answer attached</i>');
  assertBalanced(layout.html);
  assert.ok(answerLayout('&'.repeat(701)).attachment);
  assertBalanced(answerLayout('```js\n' + '* < & 🚀 '.repeat(1000) + '\n```').html);
});

test('hard previews preserve nested tags, entities and whole Unicode characters', () => {
  const html = markdownToHtml('**a _' + '🚀<& '.repeat(1000) + '_**');
  for (const limit of [30, 31, 100, 1499, 1500]) {
    const preview = previewHtml(html, limit);
    assertBalanced(preview);
    assert.ok(preview.length <= limit);
    assert.doesNotMatch(preview, /&(?!amp;|lt;|gt;|quot;)/);
    assert.equal(preview.isWellFormed(), true);
  }
  assert.equal(htmlToText('<b>&lt;x&gt; &amp; &amp;lt;</b> <a href="https://a.test/?x=1&amp;y=2">docs</a>'), '<x> & &lt; docs (https://a.test/?x=1&y=2)');
});
