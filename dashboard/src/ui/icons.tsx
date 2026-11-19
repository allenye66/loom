/** Small stroke icons (currentColor, so they follow the surrounding text tone). Inline SVG — no emoji,
 *  no network fetch, crisp at any size. */
type IconProps = { size?: number; className?: string };

const base = (size: number, className?: string) => ({
  width: size,
  height: size,
  viewBox: '0 0 16 16',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.4,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  className,
  'aria-hidden': true,
});

/** Notifications on. */
export function BellIcon({ size = 13, className }: IconProps) {
  return (
    <svg {...base(size, className)}>
      <path d="M3.6 11.2V7.4a4.4 4.4 0 0 1 8.8 0v3.8l1.1 1.3H2.5l1.1-1.3z" />
      <path d="M6.5 14a1.6 1.6 0 0 0 3 0" />
    </svg>
  );
}

/** Notifications off / muted — the bell with a slash. */
export function BellOffIcon({ size = 13, className }: IconProps) {
  return (
    <svg {...base(size, className)}>
      <path d="M12.4 9.4V7.4a4.4 4.4 0 0 0-6.9-3.6M3.6 6.1v5.1l-1.1 1.3h9.3" />
      <path d="M6.5 14a1.6 1.6 0 0 0 3 0" />
      <path d="M2 2l12 12" />
    </svg>
  );
}
