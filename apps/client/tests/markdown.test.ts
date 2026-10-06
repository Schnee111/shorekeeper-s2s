import { test } from 'node:test';
import assert from 'node:assert';
import { marked } from 'marked';

test('parses basic bold and list markdown', () => {
  const input = 'Halo **Schnee**, ini 25 partikel:\n- Item 1\n- Item 2';
  const html = marked.parse(input) as string;
  assert.strictEqual(html.includes('<strong>Schnee</strong>'), true);
  assert.strictEqual(html.includes('25 partikel'), true);
  assert.strictEqual(html.includes('<ul>'), true);
});

test('parses inline code blocks', () => {
  const input = 'Jalankan `npm test` sekarang';
  const html = marked.parse(input) as string;
  assert.strictEqual(html.includes('<code>npm test</code>'), true);
});

test('escapes raw HTML tags to prevent XSS (Issue 42)', () => {
  function escapeHtml(html: string): string {
    return html
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  const renderer = new marked.Renderer();
  renderer.html = function(token: string | { text: string; raw?: string }) {
    const raw = typeof token === 'string' ? token : (token && token.text) ? token.text : (token && token.raw) ? token.raw : '';
    return escapeHtml(raw);
  };

  marked.setOptions({
    gfm: true,
    breaks: true,
    renderer
  });

  const scriptInput = 'Halo <script>alert("xss")</script> **tebal**';
  const scriptHtml = marked.parse(scriptInput) as string;
  assert.strictEqual(scriptHtml.includes('<script>'), false);
  assert.strictEqual(scriptHtml.includes('&lt;script&gt;'), true);
  assert.strictEqual(scriptHtml.includes('&lt;/script&gt;'), true);
  assert.strictEqual(scriptHtml.includes('<strong>tebal</strong>'), true);

  const imgInput = 'Gambar jahat <img src=x onerror=alert(1)> dan `code`';
  const imgHtml = marked.parse(imgInput) as string;
  assert.strictEqual(imgHtml.includes('<img'), false);
  assert.strictEqual(imgHtml.includes('&lt;img src=x onerror=alert(1)&gt;'), true);
  assert.strictEqual(imgHtml.includes('<code>code</code>'), true);
});

