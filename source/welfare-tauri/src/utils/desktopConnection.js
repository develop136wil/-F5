// Only explicitly classified transport failures get a short grace period.
// Authentication, unknown IPC failures, startup and shutdown still fail closed.
export function advanceConnection(previous, next) {
    const blockedUntilReady = next.phase === 'ready' && !next.shutdown_requested ? false
        : previous.blockedUntilReady || next.shutdown_requested || next.phase === 'error'
            || next.phase === 'stopping' || (next.phase === 'starting' && next.transient !== true);
    const failures = next.phase === 'starting' && next.transient === true && !next.shutdown_requested
        ? (previous.transientFailures || 0) + 1 : 0;
    return { ...next, transientFailures: failures, blockedUntilReady: !!blockedUntilReady };
}

export function connectionView(hasConnected, status) {
    const ready = status.phase === 'ready' && !status.shutdown_requested;
    const grace = hasConnected && status.phase === 'starting' && status.transient === true
        && !status.shutdown_requested && !status.blockedUntilReady
        && status.transientFailures > 0 && status.transientFailures < 3;
    return {
        retainApp: hasConnected || status.phase === 'ready',
        blockActions: !ready && !grace,
        reconnecting: hasConnected && status.phase === 'starting' && !status.shutdown_requested,
    };
}
