import type { AgentId } from '../api';

/** Tiny brand-evoking marks so a chat's CLI reads at a glance even in the dense
 *  sidebar: claude = coral spark (Anthropic), codex = green hexagon (OpenAI),
 *  grok = angular broken X (xAI; strokes in currentColor so it follows the text
 *  tone of wherever it sits). Inline SVG — no network fetch, crisp at any size. */
export function AgentIcon({ agent, size = 11, className }: { agent?: AgentId | null; size?: number; className?: string }) {
  if (!agent) return null;
  const common = { width: size, height: size, viewBox: '0 0 16 16', fill: 'none', className };
  if (agent === 'claude') {
    return (
      <svg {...common} aria-hidden>
        <path
          d="M8 1.5v3.4M8 11.1v3.4M1.5 8h3.4M11.1 8h3.4M3.4 3.4l2.4 2.4M10.2 10.2l2.4 2.4M12.6 3.4l-2.4 2.4M5.8 10.2l-2.4 2.4"
          stroke="#D97757"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
      </svg>
    );
  }
  if (agent === 'codex') {
    return (
      <svg {...common} aria-hidden>
        <path d="M8 1.8l5.4 3.1v6.2L8 14.2l-5.4-3.1V4.9L8 1.8z" stroke="#10a37f" strokeWidth="1.5" strokeLinejoin="round" />
      </svg>
    );
  }
  return (
    <svg {...common} aria-hidden>
      <path d="M2.6 2.6l10.8 10.8M13.4 2.6L9.1 6.9M6.9 9.1l-4.3 4.3" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}
