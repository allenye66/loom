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

/** Visible (shown). */
export function EyeIcon({ size = 13, className }: IconProps) {
  return (
    <svg {...base(size, className)}>
      <path d="M1.5 8s2.4-4.5 6.5-4.5S14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z" />
      <circle cx="8" cy="8" r="1.9" />
    </svg>
  );
}

/** Hide — the eye with a slash. */
export function EyeOffIcon({ size = 13, className }: IconProps) {
  return (
    <svg {...base(size, className)}>
      <path d="M6.2 3.7A6.6 6.6 0 0 1 8 3.5c4.1 0 6.5 4.5 6.5 4.5a11 11 0 0 1-1.9 2.4M4 5.2A11 11 0 0 0 1.5 8S3.9 12.5 8 12.5a6.4 6.4 0 0 0 2.7-.6" />
      <path d="M6.6 6.7a1.9 1.9 0 0 0 2.7 2.6" />
      <path d="M2 2l12 12" />
    </svg>
  );
}
