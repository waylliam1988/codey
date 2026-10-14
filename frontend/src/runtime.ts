export type RuntimeApi = Record<string, any>;

const target: RuntimeApi = {};
const testBridgeEnabled = () =>
  (import.meta.env && import.meta.env.MODE === 'test') ||
  (typeof window !== 'undefined' && (window as Window & { __TEST_BRIDGE__?: boolean }).__TEST_BRIDGE__ === true);

export const runtime = new Proxy(target, {
  set(object, property, value) {
    object[property as string] = value;
    if (testBridgeEnabled()) (window as unknown as Record<string, any>)[property as string] = value;
    return true;
  },
  get(object, property) {
    if (object[property as string] !== undefined) return object[property as string];
    if (testBridgeEnabled()) return (window as unknown as Record<string, any>)[property as string];
    return undefined;
  },
}) as RuntimeApi;
