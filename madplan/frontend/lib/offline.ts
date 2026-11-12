/**
 * Working without a network, as in a store with poor signal.
 *
 * The service worker in public/sw.js keeps the app and the last copy of the shopping list and plan.
 * Ticks on the shopping list made while offline are kept here, in this browser only, and sent when
 * the network is back. A tick sets the item to checked or not, so sending it late does no harm.
 */

const QUEUE_KEY = "madplan-offline-ticks";

type Queue = Record<string, boolean>;

function readQueue(): Queue {
  try {
    return JSON.parse(window.localStorage.getItem(QUEUE_KEY) ?? "{}") as Queue;
  } catch {
    return {};
  }
}

function writeQueue(queue: Queue) {
  try {
    if (Object.keys(queue).length === 0) window.localStorage.removeItem(QUEUE_KEY);
    else window.localStorage.setItem(QUEUE_KEY, JSON.stringify(queue));
  } catch {
    // Private browsing can refuse storage. The tick then waits for the network like before.
  }
}

/** True when a request failed because there was no network, rather than a refusal. */
export function isOffline(error: unknown): boolean {
  return error instanceof TypeError || (typeof navigator !== "undefined" && !navigator.onLine);
}

export function queueTick(itemId: number, checked: boolean) {
  writeQueue({ ...readQueue(), [String(itemId)]: checked });
}

export function queuedTicks(): Queue {
  return readQueue();
}

/** Sends what was ticked offline. Returns how many went through. */
export async function flushTicks(send: (itemId: number, checked: boolean) => Promise<unknown>): Promise<number> {
  const queue = readQueue();
  let sent = 0;
  for (const [id, checked] of Object.entries(queue)) {
    try {
      await send(Number(id), checked);
      delete queue[id];
      sent += 1;
    } catch (error) {
      if (isOffline(error)) break;
      // Refused, such as an item removed meanwhile. Nothing to retry.
      delete queue[id];
    }
  }
  writeQueue(queue);
  return sent;
}

export function registerServiceWorker() {
  // Inside Home Assistant the app lives under Home Assistant's own address and login, which a
  // service worker of the app's own cannot work with. Ticks made offline are still queued.
}

/** On logging out, what was kept for offline use goes, so the next person sees none of it. */
export function clearOfflineData() {
  writeQueue({});
  if (typeof navigator !== "undefined" && navigator.serviceWorker?.controller) {
    navigator.serviceWorker.controller.postMessage("logout");
  }
}
