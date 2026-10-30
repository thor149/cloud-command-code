// Only this module creates HTML from Markdown. Source HTML is always literal text.
export const escapeHtml = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const attribute = value => escapeHtml(value).replaceAll('"', '&quot;');
export function safeUrl(value) {
  if (typeof value !== 'string' || !/^https?:\/\//i.test(value) || /[\s\x00-\x1f]/.test(value)) return null;
  try { return ['http:', 'https:'].includes(new URL(value).protocol) ? value : null; } catch { return null; }
}
export const linkHtml = (label, url) => safeUrl(url) ? `<a href="${attribute(url)}">${escapeHtml(label)}</a>` : escapeHtml(label);

function parseInline(source, allowLinks = true) {
  const root = { children: [] }, stack = [root];
  const add = node => stack.at(-1).children.push(node);
  for (let i = 0; i < source.length;) {
    // Code is opaque: neither its Markdown nor its underscores are interpreted.
    if (source[i] === '`') {
      const marker = /^`+/.exec(source.slice(i))[0];
      const end = source.indexOf(marker, i + marker.length);
      if (end !== -1) {
        add({ code: source.slice(i + marker.length, end) }); i = end + marker.length; continue;
      }
      add(marker); i += marker.length; continue;
    }
    if (allowLinks && source[i] === '[') {
      const match = /^\[([^\]\n]+)\]\(([^\s]*?)\)/.exec(source.slice(i));
      if (match) {
        const url = safeUrl(match[2]);
        // Unsafe schemes remain escaped literal Markdown, never clickable HTML.
        add(url ? { link: url, label: match[1] } : match[0]);
        i += match[0].length; continue;
      }
    }
    const top = stack.at(-1);
    const closing = top.marker && source.startsWith(top.marker, i)
      && /\S/.test(source[i - 1] || '') && (top.marker[0] !== '_' || !/[\p{L}\p{N}_]/u.test(source[i + top.marker.length] || ''));
    if (closing) { top.closed = true; stack.pop(); i += top.marker.length; continue; }
    const marker = ['**', '__', '*', '_'].find(m => source.startsWith(m, i)
      && /\S/.test(source[i + m.length] || '')
      && (m[0] !== '_' || !/[\p{L}\p{N}_]/u.test(source[i - 1] || '')));
    if (marker && stack.length < 32) {
      const node = { marker, tag: marker.length === 2 ? 'b' : 'i', children: [] };
      add(node); stack.push(node); i += marker.length; continue;
    }
    add(source[i]); i++;
  }
  return root.children;
}

function inline(source, heading = false) {
  const output = [];
  let active = [];
  const style = tag => ({ open: `<${tag}>`, close: `</${tag}>` });
  function emit(html, styles) {
    let common = 0;
    while (common < Math.min(styles.length, active.length) && styles[common].open === active[common].open) common++;
    for (let i = active.length - 1; i >= common; i--) output.push(active[i].close);
    for (let i = common; i < styles.length; i++) output.push(styles[i].open);
    output.push(html); active = styles;
  }
  function render(nodes, styles) {
    for (const node of nodes) {
      if (typeof node === 'string') emit(escapeHtml(node), styles);
      // Code is emitted outside emphasis and links, as required by Telegram.
      else if ('code' in node) emit(`<code>${escapeHtml(node.code)}</code>`, []);
      else if (node.link) render(parseInline(node.label, false), [...styles, { open: `<a href="${attribute(node.link)}">`, close: '</a>' }]);
      else if (!node.closed) { emit(escapeHtml(node.marker), styles); render(node.children, styles); }
      else render(node.children, styles.some(s => s.open === `<${node.tag}>`) ? styles : [...styles, style(node.tag)]);
    }
  }
  render(parseInline(source), heading ? [style('b')] : []);
  emit('', []);
  return output.join('');
}

export function markdownToHtml(markdown) {
  const lines = String(markdown ?? '').replaceAll('\r\n', '\n').split('\n');
  const output = [];
  for (let i = 0; i < lines.length; i++) {
    const fence = /^\s*(`{3,}|~{3,})[^`]*$/.exec(lines[i]);
    if (fence) {
      const code = [], close = new RegExp(`^\\s*${fence[1][0]}{${fence[1].length},}\\s*$`);
      while (++i < lines.length && !close.test(lines[i])) code.push(lines[i]);
      output.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`);
    } else if (/^\s*>/.test(lines[i])) {
      const quote = [];
      do { quote.push(inline(lines[i].replace(/^\s*>\s?/, ''))); i++; } while (i < lines.length && /^\s*>/.test(lines[i]));
      i--; output.push(`<blockquote>${quote.join('\n')}</blockquote>`);
    } else {
      const heading = /^#{1,3}\s+(.+)$/.exec(lines[i]);
      output.push(heading ? inline(heading[1], true) : inline(lines[i].replace(/^(\s*)[-*] /, '$1• ')));
    }
  }
  return output.join('\n');
}

// Truncate generated HTML, counting tags too. Never split an entity, surrogate
// pair, or tag; close the open stack so previews are independently valid HTML.
export function previewHtml(html, limit = 1500) {
  let output = '', paragraph = '', stack = [];
  for (const token of html.match(/<[^>]*>|&(?:#\d+|#x[\da-f]+|\w+);|[^]/giu) || []) {
    const next = [...stack];
    if (token.startsWith('</')) next.pop();
    else if (token.startsWith('<')) next.push(/^<(\w+)/.exec(token)[1]);
    const closers = [...next].reverse().map(tag => `</${tag}>`).join('');
    if (output.length + token.length + closers.length > limit) break;
    output += token; stack = next;
    if (output.endsWith('\n\n') && output.trim()) paragraph = output.trimEnd() + closers;
  }
  const balanced = output.trimEnd() + [...stack].reverse().map(tag => `</${tag}>`).join('');
  return paragraph || balanced;
}

export function answerLayout(markdown) {
  const html = markdownToHtml(markdown);
  return html.length > 3500 ? { html: `${previewHtml(html)}\n\n<i>…full answer attached</i>`, attachment: String(markdown) }
    : { html, attachment: null };
}

export const htmlToText = html => html.replace(/<a href="([^"]*)">([\s\S]*?)<\/a>/g, '$2 ($1)').replace(/<[^>]*>/g, '').replaceAll('&lt;', '<').replaceAll('&gt;', '>')
  .replaceAll('&quot;', '"').replaceAll('&amp;', '&');
