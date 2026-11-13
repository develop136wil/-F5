import test from 'node:test';
import assert from 'node:assert/strict';
import { POLICY_TASK_LABELS, fetchPolicyTaskStatuses, policyTaskPresentation } from '../src/utils/policyTaskPresentation.js';
test('six policy statuses use one read and preserve order, progress and abort signal', async () => {
    const signal = new AbortController().signal;
    const snapshot = POLICY_TASK_LABELS.map((label,index)=>({label,running:index<2,checked:2,total:4}));
    let calls=0;
    const tasks=await fetchPolicyTaskStatuses(async supplied=>{calls++;assert.equal(supplied,signal);return snapshot;},signal);
    assert.equal(calls,1);
    assert.equal(tasks,snapshot);
    assert.equal(policyTaskPresentation(tasks).label,'정책 최신화');
    assert.equal(policyTaskPresentation(tasks).percent,50);
});
test('idle snapshot is one read; failed or incomplete snapshots never become idle', async () => {
    let calls=0;
    const idle=POLICY_TASK_LABELS.map(label=>({label,running:false}));
    assert.equal(policyTaskPresentation(await fetchPolicyTaskStatuses(async()=>{calls++;return idle;})),null);
    assert.equal(calls,1);
    const failure=new Error('temporary');
    await assert.rejects(fetchPolicyTaskStatuses(async()=>{throw failure;}),error=>error===failure);
    for(const value of [null,[],idle.slice(1),[...idle.slice(0,5),{label:POLICY_TASK_LABELS[5]}]]) {
        await assert.rejects(fetchPolicyTaskStatuses(async()=>value),/진행 상태/);
    }
});
test('idle work has no progress card', () => assert.equal(policyTaskPresentation([{running:false}]), null));
test('parent task wins and unknown total is not fake zero percent', () => {
    assert.equal(policyTaskPresentation([{running:true,label:'parent'}, {running:true,label:'child',total:10,checked:3}]).percent, null);
});
test('valid counts are clamped', () => {
    assert.equal(policyTaskPresentation([{running:true,total:10,checked:3}]).percent, 30);
    assert.equal(policyTaskPresentation([{running:true,total:10,checked:20}]).percent, 100);
});
