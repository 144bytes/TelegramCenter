/** The few parts of Node's own test runner these tests use. The project
 *  has no @types/node and needs no more than this. */
declare module "node:test" {
  export default function test(name: string, fn: () => void | Promise<void>): void;
}

declare module "node:assert/strict" {
  const assert: {
    equal(actual: unknown, expected: unknown, message?: string): void;
    deepEqual(actual: unknown, expected: unknown, message?: string): void;
  };
  export default assert;
}
