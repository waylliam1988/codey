export {};

declare global {
  interface Window {
    __TEST_BRIDGE__?: boolean;
  }
}
