import test from 'node:test';
import assert from 'node:assert/strict';
import { advanceConnection, connectionView } from '../src/utils/desktopConnection.js';

test('first launch waits without mounting the application', () => {
    assert.deepEqual(connectionView(false, { phase: 'starting' }), {
        retainApp: false, blockActions: true, reconnecting: false,
    });
});
test('ready connection allows application use', () => {
    assert.deepEqual(connectionView(false, { phase: 'ready' }), {
        retainApp: true, blockActions: false, reconnecting: false,
    });
});
test('temporary outage retains screen but blocks new actions', () => {
    assert.deepEqual(connectionView(true, { phase: 'starting' }), {
        retainApp: true, blockActions: true, reconnecting: true,
    });
    assert.equal(connectionView(true, { phase: 'ready' }).blockActions, false);
});
test('actual exit and shutdown never become usable or show reconnect advice', () => {
    for (const phase of ['error', 'stopping']) {
        assert.deepEqual(connectionView(true, { phase }), {
            retainApp: true, blockActions: true, reconnecting: false,
        });
    }
});

test('two transport failures preserve editing; third failure blocks until recovery', () => {
    let status = { phase: 'ready' };
    for (let i = 1; i <= 4; i++) {
        status = advanceConnection(status, { phase: 'starting', transient: true });
        assert.equal(status.transientFailures, i);
        assert.equal(connectionView(true, status).blockActions, i >= 3);
        assert.equal(connectionView(true, status).retainApp, true);
        assert.equal(connectionView(false, status).blockActions, true);
    }
    status = advanceConnection(status, { phase: 'ready' });
    assert.equal(status.transientFailures, 0);
    assert.equal(connectionView(true, status).blockActions, false);
    status = advanceConnection(status, { phase: 'starting', transient: true });
    assert.equal(connectionView(true, status).blockActions, false);
});

test('untrusted replies and shutdown never receive transport grace', () => {
    for (const next of [
        { phase: 'starting' }, { phase: 'starting', transient: false },
        { phase: 'error', transient: true }, { phase: 'stopping', transient: true },
        { phase: 'ready', shutdown_requested: true },
        { phase: 'starting', transient: true, shutdown_requested: true },
    ]) {
        let status = advanceConnection({ phase: 'ready' }, next);
        assert.equal(connectionView(true, status).blockActions, true);
        status = advanceConnection(status, { phase: 'starting', transient: true });
        assert.equal(connectionView(true, status).blockActions, true, 'no grace after a hard failure');
        status = advanceConnection(status, { phase: 'ready' });
        assert.equal(connectionView(true, status).blockActions, false);
    }
});
