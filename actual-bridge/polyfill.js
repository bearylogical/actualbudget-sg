// Browser-global stubs required by @actual-app/api in a Node.js environment
globalThis.navigator ??= { userAgent: 'node' };
globalThis.window   ??= globalThis;
