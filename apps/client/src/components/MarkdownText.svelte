<script lang="ts">
  import { marked, Renderer } from 'marked';

  let { text = '' }: { text: string } = $props();

  function escapeHtml(html: string): string {
    return html
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // Configure marked for clean inline/block markdown and prevent raw HTML execution (XSS mitigation)
  const renderer = new Renderer();
  renderer.html = function(token: string | { text: string; raw?: string }) {
    const raw = typeof token === 'string' ? token : (token && token.text) ? token.text : (token && token.raw) ? token.raw : '';
    return escapeHtml(raw);
  };

  marked.setOptions({
    gfm: true,
    breaks: true,
    renderer
  });

  const parsedHtml = $derived(text ? (marked.parse(text) as string) : '');
</script>

<div class="markdown-content text-xs text-zinc-200 leading-relaxed">
  {@html parsedHtml}
</div>

<style>
  :global(.markdown-content p) {
    margin: 0 0 0.35rem 0;
  }
  :global(.markdown-content p:last-child) {
    margin-bottom: 0;
  }
  :global(.markdown-content ul), :global(.markdown-content ol) {
    padding-left: 1.1rem;
    margin: 0.25rem 0;
  }
  :global(.markdown-content li) {
    margin: 0.1rem 0;
  }
  :global(.markdown-content strong) {
    color: #f4f4f5;
    font-weight: 600;
  }
  :global(.markdown-content code) {
    font-family: monospace;
    background: rgba(255, 255, 255, 0.08);
    padding: 0.1rem 0.3rem;
    border-radius: 0.25rem;
    color: #67e8f9;
  }
</style>