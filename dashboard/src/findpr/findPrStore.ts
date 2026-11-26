import { useSyncExternalStore } from 'react';

/** Open/closed state of the "find a PR" modal (module store: the sidebar button and anything else can open it). */
let open = false;
const subs = new Set<() => void>();
const emit = () => subs.forEach((f) => f());
export const openFindPr = () => {
  if (!open) {
    open = true;
    emit();
  }
};
export const closeFindPr = () => {
  if (open) {
    open = false;
    emit();
  }
};
export const useFindPrOpen = () =>
  useSyncExternalStore(
    (f) => {
      subs.add(f);
      return () => void subs.delete(f);
    },
    () => open,
  );
