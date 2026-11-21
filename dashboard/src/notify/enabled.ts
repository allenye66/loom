/** Desktop notifications on/off (per browser) — the permission toggle shared by the panel and the notifier. */
import { useEffect, useState } from 'react';

const KEY = 'loom.notify';
export const notificationsSupported = typeof window !== 'undefined' && 'Notification' in window;

function read(): boolean {
  try {
    return localStorage.getItem(KEY) === '1';
  } catch {
    return false;
  }
}
const listeners = new Set<() => void>();
function write(on: boolean) {
  try {
    localStorage.setItem(KEY, on ? '1' : '0');
  } catch {
    /* private mode — the toggle just won't persist */
  }
  listeners.forEach((l) => l());
}

/** Desktop notifications on/off (per browser). Turning on asks the browser for permission. */
export function useNotifyEnabled(): [boolean, () => void, NotificationPermission | 'unsupported'] {
  const [on, setOn] = useState(read);
  const [perm, setPerm] = useState<NotificationPermission | 'unsupported'>(notificationsSupported ? Notification.permission : 'unsupported');
  useEffect(() => {
    const l = () => setOn(read());
    listeners.add(l);
    return () => {
      listeners.delete(l);
    };
  }, []);
  const toggle = async () => {
    if (on) return write(false);
    if (notificationsSupported && Notification.permission === 'default') setPerm(await Notification.requestPermission());
    else if (notificationsSupported) setPerm(Notification.permission);
    write(true);
  };
  return [on && perm !== 'denied', toggle, perm];
}
