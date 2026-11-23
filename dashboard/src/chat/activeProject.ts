import { useSyncExternalStore } from 'react';

/** The project (registered repo) the sidebar currently shows — published by the sidebar so other
 *  parts of the shell (the notes modal and its count) can scope themselves to it. */
let name: string | undefined;
const subs = new Set<() => void>();
export function setActiveProjectName(n: string | undefined) {
  if (n === name) return;
  name = n;
  subs.forEach((f) => f());
}
export const useActiveProjectName = () =>
  useSyncExternalStore(
    (f) => {
      subs.add(f);
      return () => void subs.delete(f);
    },
    () => name,
  );
