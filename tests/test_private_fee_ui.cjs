// No DOM needed: verify that choosing a peer can never mutate the other ASIC.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('dashboard/app.js', 'utf8');
const helpers = source.slice(source.indexOf('function feeTestChoices()'), source.indexOf('function renderFeeTest()'));
let allowed = true;
const context = vm.createContext({
  serviceAdmin: () => allowed, number: String, selectedFeeTestPort: null,
  routeConfig: {fee_test: {listen_port: 3334, worker: 'own.worker', percent: 100}},
  latestSessions: [{worker: 'own.worker'}],
  pilotSnapshot: {fee_test: {listen_port: 3333, worker: 'peer.worker', percent: 100}, sessions: [{worker: 'peer.worker'}]},
});
vm.runInContext(helpers, context);
assert.equal(context.selectedFeeTest().path, '/api/fee-test');
context.selectedFeeTestPort = '3333';
assert.equal(context.selectedFeeTest().path, '/api/pilot-3333/fee-test');
assert.equal(context.selectedFeeTest().test.worker, 'peer.worker');
assert.match(context.feeTargetSummary(), /:3333 DevFee 100%.*:3334 DevFee 100%/);
context.pilotSnapshot = null;
assert.equal(context.selectedFeeTest(), undefined, 'Unavailable peer must not fall back to mutating 3334');
context.selectedFeeTestPort = '3334';
assert.equal(context.selectedFeeTest().test.worker, 'own.worker');
allowed = false;
assert.equal(context.feeTestChoices().length, 0);
assert.equal(context.selectedFeeTest(), undefined);
console.log('Private fee port routing passed');
