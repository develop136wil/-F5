"""Budget-sized embedding batches; fake SDK only, no original application data."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services import ai_coordinator
from tests.test_embedding_input_safety import load_embedding_service


class EmbeddingBatchBudget(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = load_embedding_service()
        self.inputs = []
        self.provider = Mock(side_effect=self.response)
        self.client = SimpleNamespace(models=SimpleNamespace(embed_content=self.provider))
        self.slot, self.sync_slot = AsyncMock(), Mock()
        self.planning = Mock(return_value=18000)
        self.request_planning = Mock(return_value=None)
        self.register = Mock()
        for patcher in (
            patch.object(ai_coordinator,'input_token_budget',self.planning),
            patch.object(ai_coordinator,'input_request_budget',self.request_planning,create=True),
            patch.object(ai_coordinator,'acquire_model_slot',self.slot),
            patch.object(ai_coordinator,'reserve_sync_model_slot',self.sync_slot),
            patch.object(ai_coordinator,'register_api_error',self.register),
        ):
            patcher.start(); self.addCleanup(patcher.stop)

    def response(self, *, contents, **kwargs):
        prepared = self.inputs
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[float(prepared.index(text)),1.0]) for text in contents])

    def expected(self, texts, excluded=()):
        return [None if not text.strip() or index in excluded else [float(index),1.0]
                for index,text in enumerate(texts)]

    async def invoke(self, texts, *, asynchronous):
        self.inputs = list(texts)
        if asynchronous:
            return await self.service.embed_texts_batch_async(texts,client=self.client)
        return self.service.embed_texts_batch(texts,client=self.client)

    def reset_calls(self):
        for mock in (self.provider,self.slot,self.sync_slot,self.planning,self.request_planning,self.register):
            mock.reset_mock(side_effect=False,return_value=False)

    async def test_current_and_reconfirmation_budgets_split_only_as_needed(self):
        texts = [(chr(0xAC00+index)+' 복지 ')*120 for index in range(18)]
        for budget in (18000,10000):
            for asynchronous in (False,True):
                with self.subTest(budget=budget,asynchronous=asynchronous):
                    self.reset_calls(); self.planning.return_value = budget
                    result = await self.invoke(texts,asynchronous=asynchronous)
                    self.assertEqual(result,self.expected(texts))
                    chunks = [call.kwargs['contents'] for call in self.provider.call_args_list]
                    self.assertGreater(len(chunks),1)
                    self.assertLess(len(chunks),len(texts))
                    self.assertEqual([text for chunk in chunks for text in chunk],texts)
                    for chunk in chunks:
                        self.assertLessEqual(ai_coordinator.estimate_input_tokens(chunk),budget)
                    # Every nonfinal chunk was filled as far as the next item permits.
                    for first,following in zip(chunks,chunks[1:]):
                        self.assertGreater(ai_coordinator.estimate_input_tokens(first+[following[0]]),budget)
                    reservations = self.slot if asynchronous else self.sync_slot
                    other = self.sync_slot if asynchronous else self.slot
                    self.assertEqual(reservations.call_count,len(chunks))
                    self.assertEqual(other.call_count,0)
                    self.assertEqual([call.kwargs['input_tokens'] for call in reservations.call_args_list],
                        [ai_coordinator.estimate_input_tokens(chunk) for chunk in chunks])
                    self.assertEqual([call.kwargs['request_units'] for call in reservations.call_args_list],
                        [len(chunk) for chunk in chunks])

    async def test_item_and_token_budgets_both_bound_maximal_ordered_chunks(self):
        texts=[chr(0xAC00+index)*80 for index in range(7)]
        self.planning.return_value=1100  # two inputs at most by token budget
        for request_budget, expected_sizes in ((1,[1]*7),(2,[2,2,2,1]),(3,[2,2,2,1]),(None,[2,2,2,1])):
            for asynchronous in (False,True):
                with self.subTest(request_budget=request_budget,asynchronous=asynchronous):
                    self.reset_calls(); self.request_planning.return_value=request_budget
                    result=await self.invoke(texts,asynchronous=asynchronous)
                    self.assertEqual(result,self.expected(texts))
                    self.assertEqual([len(call.kwargs['contents']) for call in self.provider.call_args_list],expected_sizes)
                    selected=self.slot if asynchronous else self.sync_slot
                    other=self.sync_slot if asynchronous else self.slot
                    self.assertEqual([call.kwargs['request_units'] for call in selected.call_args_list],expected_sizes)
                    other.assert_not_called()

    async def test_last_remaining_item_is_sent_and_later_items_are_preserved(self):
        texts=['', '가'*8000, '지원', '기관', '행사']
        for asynchronous in (False,True):
            self.reset_calls(); self.request_planning.side_effect=[1,0]
            result=await self.invoke(texts,asynchronous=asynchronous)
            self.assertEqual(result,[None,None,[2.0,1.0],None,None])
            self.provider.assert_called_once()
            self.assertEqual(self.provider.call_args.kwargs['contents'],['지원'])
            selected=self.slot if asynchronous else self.sync_slot
            self.assertEqual(selected.call_args.kwargs['request_units'],1)
            self.assertEqual(selected.call_count,1)

    async def test_item_budget_is_replanned_after_each_dispatch(self):
        texts=['지원','기관','행사','제도','알림']
        self.planning.return_value=None  # request units alone still split a legacy token policy
        for asynchronous in (False,True):
            self.reset_calls(); self.request_planning.side_effect=[3,1,0]
            result=await self.invoke(texts,asynchronous=asynchronous)
            self.assertEqual(result,self.expected(texts,excluded=(4,)))
            self.assertEqual([len(call.kwargs['contents']) for call in self.provider.call_args_list],[3,1])

    async def test_zero_invalid_or_unavailable_item_budget_makes_no_reservation_or_api(self):
        for budget in (0,True,-1,'20'):
            for asynchronous in (False,True):
                with self.subTest(budget=budget,asynchronous=asynchronous):
                    self.reset_calls(); self.request_planning.return_value=budget
                    self.assertEqual(await self.invoke(['지원','기관'],asynchronous=asynchronous),[None,None])
                    self.provider.assert_not_called(); self.slot.assert_not_called(); self.sync_slot.assert_not_called()
        self.request_planning.side_effect=OSError('synthetic budget unavailable')
        for asynchronous in (False,True):
            self.assertEqual(await self.invoke(['지원'],asynchronous=asynchronous),[None])
        self.provider.assert_not_called(); self.slot.assert_not_called(); self.sync_slot.assert_not_called()

    async def test_unconfirmed_policy_keeps_full_inputs_within_local_limit_and_empty_index_mapping(self):
        self.planning.return_value = None
        texts = ['긴 본문 '*100,'', '다른 본문 '*100, ' ', '마지막']
        for asynchronous in (False,True):
            with self.subTest(asynchronous=asynchronous):
                self.reset_calls()
                result = await self.invoke(texts,asynchronous=asynchronous)
                self.assertEqual(result,self.expected(texts))
                self.provider.assert_called_once()
                self.assertEqual(self.provider.call_args.kwargs['contents'],[text for text in texts if text.strip()])

    async def test_individually_oversized_text_is_not_further_truncated_or_sent(self):
        texts = ['가'*8000,'지원','', '나'*7000, '제도']
        for asynchronous in (False,True):
            with self.subTest(asynchronous=asynchronous):
                self.reset_calls()
                result = await self.invoke(texts,asynchronous=asynchronous)
                self.assertEqual(result,self.expected(texts,excluded=(0,3)))
                self.provider.assert_called_once()
                self.assertEqual(self.provider.call_args.kwargs['contents'],['지원','제도'])
                self.assertEqual(len(self.service.logger.warning.call_args_list),1)
                self.service.logger.reset_mock()

    async def test_all_oversized_inputs_leave_none_without_reservation(self):
        for asynchronous in (False,True):
            self.reset_calls()
            self.assertEqual(await self.invoke(['가'*8000,'나'*8000],asynchronous=asynchronous),[None,None])
            self.provider.assert_not_called(); self.slot.assert_not_called(); self.sync_slot.assert_not_called()

    async def test_existing_8000_character_prefix_is_not_sent_as_full_document(self):
        self.planning.return_value=9000
        text='a'*9000
        self.assertEqual(await self.invoke([text,'안내'],asynchronous=True),[None,[1.0,1.0]])
        self.assertEqual(self.provider.call_args.kwargs['contents'],['안내'])

    async def test_zero_invalid_or_unavailable_budget_never_reserves_or_calls(self):
        for budget in (0,True,-1,'18000'):
            for asynchronous in (False,True):
                with self.subTest(budget=budget,asynchronous=asynchronous):
                    self.reset_calls(); self.planning.return_value=budget
                    self.assertEqual(await self.invoke(['지원','기관'],asynchronous=asynchronous),[None,None])
                    self.provider.assert_not_called(); self.slot.assert_not_called(); self.sync_slot.assert_not_called()
        self.planning.side_effect=OSError('synthetic no access')
        self.assertEqual(await self.invoke(['지원'],asynchronous=True),[None])
        self.provider.assert_not_called()

    async def test_mid_batch_daily_limit_or_ledger_problem_preserves_prefix(self):
        texts=[chr(0xAC00+index)*80 for index in range(5)]
        self.planning.return_value=1100  # two inputs, not three
        for error in (ai_coordinator.AiLocalBudgetExhausted('synthetic'),
                      ai_coordinator.AiDailyQuotaExhausted('synthetic'),
                      ai_coordinator.AiQuotaStateUnavailable('synthetic'),
                      ai_coordinator.AiInputTokenBudgetExceeded('synthetic'),
                      ai_coordinator._RateWindowWait(1)):
            for asynchronous in (False,True):
                with self.subTest(error=type(error).__name__,asynchronous=asynchronous):
                    self.reset_calls()
                    selected=self.slot if asynchronous else self.sync_slot
                    selected.side_effect=[None,error]
                    result=await self.invoke(texts,asynchronous=asynchronous)
                    self.assertEqual(result,self.expected(texts,excluded=(2,3,4)))
                    self.provider.assert_called_once()
                    self.assertEqual(selected.call_count,2)
                    self.register.assert_not_called()
                    selected.side_effect=None

    async def test_new_penalty_budget_is_read_for_every_next_chunk(self):
        texts=[chr(0xAC00+index)*80 for index in range(5)]
        self.planning.side_effect=[1100,650,650,650]
        self.assertEqual(await self.invoke(texts,asynchronous=True),self.expected(texts))
        self.assertEqual([len(call.kwargs['contents']) for call in self.provider.call_args_list],[2,1,1,1])
        self.assertEqual(self.slot.await_count,4)
        self.sync_slot.assert_not_called()

    async def test_provider_429_never_auto_retries_later_chunks(self):
        texts=[chr(0xAC00+index)*80 for index in range(5)]
        self.planning.return_value=1100
        for asynchronous in (False,True):
            self.reset_calls()
            first=self.response
            count=0
            def throttled(**kwargs):
                nonlocal count
                count+=1
                if count==2:
                    raise RuntimeError('429 RESOURCE_EXHAUSTED synthetic')
                return first(**kwargs)
            self.provider.side_effect=throttled
            result=await self.invoke(texts,asynchronous=asynchronous)
            self.assertEqual(result,self.expected(texts,excluded=(2,3,4)))
            self.assertEqual(self.provider.call_count,2)
            self.register.assert_called_once()
            self.assertNotIn('synthetic',repr(self.service.logger.warning.call_args_list))
            self.service.logger.reset_mock()

    async def test_daily_provider_failure_stops_and_marks_existing_daily_hold(self):
        self.planning.return_value=650
        self.provider.side_effect=RuntimeError('RESOURCE_EXHAUSTED PerDay synthetic')
        self.service._mark_daily_quota_exhausted=Mock()
        self.assertEqual(await self.invoke(['가'*80,'나'*80],asynchronous=True),[None,None])
        self.provider.assert_called_once()
        self.service._mark_daily_quota_exhausted.assert_called_once()

    async def test_sensitive_later_input_blocks_entire_batch_before_planning(self):
        texts=['가'*8000, '지원 '*2000+'010-1234-5678']
        for asynchronous in (False,True):
            self.assertEqual(await self.invoke(texts,asynchronous=asynchronous),[None,None])
        self.planning.assert_not_called(); self.provider.assert_not_called()
        self.request_planning.assert_not_called()
        self.slot.assert_not_called(); self.sync_slot.assert_not_called()

    async def test_async_list_mutation_during_admission_does_not_reuse_old_reservation(self):
        for replacement in ('다른 문장','010-1234-5678'):
            texts=['안내','기관']
            self.reset_calls()
            async def reserve(_model,**kwargs): texts[1]=replacement
            self.slot.side_effect=reserve
            self.assertEqual(await self.invoke(texts,asynchronous=True),[None,None])
            self.provider.assert_not_called(); self.sync_slot.assert_not_called()

    async def test_mutation_during_full_preflight_cannot_replace_reviewed_snapshot(self):
        for asynchronous in (False,True):
            texts=['기관','안내']
            safety=self.service._input_allows_embedding
            def review(*inputs):
                allowed=safety(*inputs)
                texts[1]='010-1234-5678'
                return allowed
            self.reset_calls()
            with patch.object(self.service,'_input_allows_embedding',side_effect=review):
                self.assertEqual(await self.invoke(texts,asynchronous=asynchronous),[None,None])
            self.provider.assert_not_called()

    async def test_response_count_mismatch_cannot_shift_vectors(self):
        self.planning.return_value=1100
        texts=[chr(0xAC00+index)*80 for index in range(5)]
        for count in (0,1,3):
            for asynchronous in (False,True):
                self.reset_calls()
                self.provider.side_effect=None
                self.provider.return_value=SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0]) for _ in range(count)])
                self.assertEqual(await self.invoke(texts,asynchronous=asynchronous),[None]*5)
                self.provider.assert_called_once()

    async def test_empty_response_vector_preserves_its_own_position(self):
        self.inputs=['지원','','기관']
        self.provider.side_effect=None
        self.provider.return_value=SimpleNamespace(embeddings=[SimpleNamespace(values=[]),SimpleNamespace(values=[2.0])])
        self.assertEqual(await self.service.embed_texts_batch_async(self.inputs,client=self.client),[None,None,[2.0]])

    def test_public_batch_handoff_argument_cannot_cover_multiple_requests(self):
        self.planning.return_value=650
        self.inputs=['가'*80,'나'*80,'다'*80]
        for token in (True,self.service._ASYNC_QUOTA_RESERVED):
            self.reset_calls()
            result=self.service.embed_texts_batch(self.inputs,client=self.client,_quota_reservation=token)
            self.assertEqual(result,self.expected(self.inputs))
            self.assertEqual(self.sync_slot.call_count,3)
            self.assertEqual(self.provider.call_count,3)


class ActualBatchLedger(unittest.IsolatedAsyncioTestCase):
    async def test_real_ledger_counts_all_items_and_ignores_legacy_daily_cap(self):
        for asynchronous in (False,True):
            with self.subTest(asynchronous=asynchronous), tempfile.TemporaryDirectory(prefix='batch-ledger-') as folder:
                spec=importlib.util.spec_from_file_location('synthetic_batch_coordinator',ai_coordinator.__file__)
                coordinator=importlib.util.module_from_spec(spec); spec.loader.exec_module(coordinator)
                service=load_embedding_service(); service.ai_coordinator=coordinator
                state=Path(folder)/'state.json'
                coordinator._state_path=lambda:str(state)
                coordinator.input_token_budget=lambda model:1100
                provider=Mock(side_effect=lambda **kwargs:SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0]) for _ in kwargs['contents']]))
                client=SimpleNamespace(models=SimpleNamespace(embed_content=provider))
                texts=[chr(0xAC00+index)*80 for index in range(5)]
                with patch.dict(os.environ,{'AI_DAILY_REQUEST_LIMIT':'4'}):
                    if asynchronous:
                        result=await service.embed_texts_batch_async(texts,client=client)
                    else:
                        result=service.embed_texts_batch(texts,client=client)
                self.assertEqual(result,[[1.0]]*5)
                self.assertEqual(provider.call_count,3)
                self.assertEqual(sum(json.loads(state.read_text(encoding='utf-8'))['calls'].values()),5)

    async def test_batch_is_not_cut_to_legacy_daily_remaining_units(self):
        for asynchronous in (False,True):
            with self.subTest(asynchronous=asynchronous), tempfile.TemporaryDirectory(prefix='batch-last-unit-') as folder:
                spec=importlib.util.spec_from_file_location('synthetic_batch_last_coordinator',ai_coordinator.__file__)
                coordinator=importlib.util.module_from_spec(spec); spec.loader.exec_module(coordinator)
                service=load_embedding_service(); service.ai_coordinator=coordinator
                state=Path(folder)/'state.json'
                coordinator._state_path=lambda:str(state)
                coordinator.input_token_budget=lambda model:18000
                provider=Mock(side_effect=lambda **kwargs:SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0]) for _ in kwargs['contents']]))
                client=SimpleNamespace(models=SimpleNamespace(embed_content=provider))
                with patch.dict(os.environ,{'AI_DAILY_REQUEST_LIMIT':'5'}):
                    coordinator.reserve_sync_model_slot(service.EMBED_MODEL,
                        input_tokens=coordinator.estimate_input_tokens(['사전 검증']*4),request_units=4)
                    if asynchronous:
                        result=await service.embed_texts_batch_async(['지원','기관'],client=client)
                    else:
                        result=service.embed_texts_batch(['지원','기관'],client=client)
                self.assertEqual(result,[[1.0],[1.0]])
                self.assertEqual(provider.call_count,1)
                self.assertEqual(provider.call_args.kwargs['contents'],['지원','기관'])
                self.assertEqual(sum(json.loads(state.read_text(encoding='utf-8'))['calls'].values()),6)

    async def test_real_provider_hold_preserves_completed_batch_prefix(self):
        for asynchronous in (False,True):
            with self.subTest(asynchronous=asynchronous), tempfile.TemporaryDirectory(prefix='batch-provider-hold-') as folder:
                spec=importlib.util.spec_from_file_location('synthetic_batch_provider_hold',ai_coordinator.__file__)
                coordinator=importlib.util.module_from_spec(spec); spec.loader.exec_module(coordinator)
                service=load_embedding_service(); service.ai_coordinator=coordinator
                state=Path(folder)/'state.json'
                coordinator._state_path=lambda:str(state)
                coordinator.input_token_budget=lambda model:1100
                def complete_then_observe_provider_exhaustion(**kwargs):
                    coordinator.mark_daily_exhausted(service.EMBED_MODEL)
                    return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0]) for _ in kwargs['contents']])
                provider=Mock(side_effect=complete_then_observe_provider_exhaustion)
                client=SimpleNamespace(models=SimpleNamespace(embed_content=provider))
                texts=[chr(0xAC00+index)*80 for index in range(5)]
                if asynchronous:
                    result=await service.embed_texts_batch_async(texts,client=client)
                else:
                    result=service.embed_texts_batch(texts,client=client)
                self.assertEqual(result,[[1.0],[1.0],None,None,None])
                provider.assert_called_once()
                saved=json.loads(state.read_text(encoding='utf-8'))
                self.assertEqual(sum(saved['calls'].values()),2)
                self.assertEqual(saved['exhausted'][service.EMBED_MODEL],saved['date_pacific'])
