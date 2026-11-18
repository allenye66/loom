/** A small spinning ring in the accent colour — "something is loading". */
export function Spinner({ size = 14, className = '' }: { size?: number; className?: string }) {
  return (
    <span
      role="status"
      aria-label="loading"
      style={{ width: size, height: size }}
      className={`inline-block shrink-0 rounded-full border-2 border-edge border-t-accent animate-spin ${className}`}
    />
  );
}
