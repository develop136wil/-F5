"""Offline counterexamples from expert feedback; no business data or remote I/O."""
import asyncio
import json
import os
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import aiohttp
import api_server
import database
import notifier
from services import app_lifecycle, policy_undo
from services.age_values import month_value, valid_month, validate_restored_months
from services import public_transport as net

class OriginTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, origin=None, token='', supplied=None, method='POST'):
        consumed=[]; messages=[]
        async def application(scope, receive, send):
            consumed.append(await receive())
            await send({'type':'http.response.start','status':204,'headers':[]})
            await send({'type':'http.response.body','body':b''})
        headers=[(b'content-type',b'multipart/form-data; boundary=x')]
        if origin is not None:headers.append((b'origin',origin))
        if supplied is not None:headers.append((b'x-welfare-session',supplied))
        async def receive():return {'type':'http.request','body':b'UNTRUSTED UPLOAD','more_body':False}
        async def send(message):messages.append(message)
        with patch.dict(os.environ,WELFARE_DESKTOP_TOKEN=token):
            await app_lifecycle.DesktopAdmissionMiddleware(application)(
                {'type':'http','path':'/api/db/restore','method':method,'headers':headers},receive,send)
        return messages[0]['status'], consumed

    async def test_foreign_multipart_rejected_before_body_with_or_without_token(self):
        for origin in (b'https://outside.example',b'null',b'http://localhost.attacker:8421'):
            for secret in ('','synthetic'):
                with self.subTest(origin=origin,secret=bool(secret)):
                    status,body=await self.call(origin,secret,secret.encode())
                    self.assertEqual(status,403);self.assertEqual(body,[])

    async def test_local_ui_and_command_line_still_work(self):
        for origin in (None,b'http://localhost:1420',b'http://tauri.localhost'):
            self.assertEqual((await self.call(origin))[0],204)
        self.assertEqual((await self.call(b'http://tauri.localhost','session',b'session'))[0],204)
        self.assertEqual((await self.call(b'http://tauri.localhost','session',b'wrong'))[0],403)

class MonthTests(unittest.TestCase):
    def test_contract(self):
        for raw in (0,1,1800,1801,9999,10000,'0','18','9999','10000',None,''):
            with self.subTest(raw=raw):self.assertTrue(valid_month(raw))
        for raw in (-1,True,1.5,'1.5','<img src=x onerror=alert(1)>','1e2',[],{}):
            with self.subTest(raw=raw):self.assertFalse(valid_month(raw));self.assertIsNone(month_value(raw))

    def test_unbounded_month_values_survive_actual_restore_without_rewrite(self):
        from contextlib import closing
        with tempfile.TemporaryDirectory() as td:
            live=Path(td)/'live.db'; incoming=Path(td)/'incoming.db'; safety=Path(td)/'safety.db'
            database.close_all_connections()
            with patch.object(database,'DB_FILE',str(live)):
                try:
                    database.init_db()
                    with database.get_connection() as db:
                        for pid,value in enumerate((9999,'9999',10000),1):
                            db.execute('INSERT INTO notion_policies(id,db_key,page_id,notion_fields) VALUES(?,?,?,?)',
                                       (pid,'test',str(pid),json.dumps({'start_age_months':0,'end_age_months':value})))
                        db.commit()
                    database.backup_to(str(incoming))
                    self.assertIsNone(api_server._validate_restore_candidate(str(incoming)))
                    api_server._commit_database_restore(str(incoming),str(live),str(safety))
                    with database.get_connection() as db:
                        values=[json.loads(row[0])['end_age_months'] for row in db.execute('SELECT notion_fields FROM notion_policies ORDER BY id')]
                    self.assertEqual(values,[9999,'9999',10000])
                finally:
                    database.close_all_connections()

    def test_report_preserves_numeric_sentinel_without_inventing_age_meaning(self):
        parsed=notifier._parse_item_data({'start_age_months':0,'ai_data':json.dumps({'end_age_months':9999})})
        self.assertEqual(parsed['end_age'],9999)

    def test_report_renders_untrusted_month_as_blank_not_markup(self):
        hostile='<img src=x onerror=alert(1)>'
        item={'title':'synthetic','link':'https://example.org','start_age_months':hostile,
              'ai_data':json.dumps({'end_age_months':hostile})}
        output=Path(notifier.generate_notion_report([item])).read_text(encoding='utf-8')
        self.assertNotIn(hostile,output)
        self.assertNotIn('onerror=',output)
        self.assertIsNone(notifier._parse_item_data(item)['start_age'])

    def test_restore_checks_columns_and_nested_json(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE welfare_items(start_age_months,ai_data)')
            db.execute('INSERT INTO welfare_items VALUES (?,?)',(12,'{}'))
            self.assertIsNone(validate_restored_months(db,{'welfare_items'}))
            db.execute('UPDATE welfare_items SET ai_data=?',(json.dumps({'end_age_months':'<script>'}),))
            self.assertIsNotNone(validate_restored_months(db,{'welfare_items'}))

class RestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_restore_marks_forged_receipt_unavailable_before_notion(self):
        await self.restore_case(False)

    async def test_imported_sql_cannot_reactivate_receipt_after_restore(self):
        await self.restore_case(True)

    async def restore_case(self, imported_code):
        with tempfile.TemporaryDirectory() as td:
            live=Path(td)/'live.db'; incoming=Path(td)/'incoming.db'; safety=Path(td)/'safety.db'
            database.close_all_connections()
            with patch.object(database,'DB_FILE',str(live)):
                database.init_db()
                with database.get_connection() as db:
                    policy_undo.schema(db)
                    db.execute("INSERT INTO notion_policies(id,db_key,page_id,notion_fields) VALUES(1,'test','page','{}')")
                    db.execute("INSERT INTO policy_apply_receipts(id,policy_id,page_id,field,fkey,property_id,before_json,after_json,before_value,state) VALUES('receipt',1,'page','support','support_detail','property',?,?,?,'applied')",
                               (json.dumps({'rich_text':[{'text':{'content':'hidden remote write'}}]}),json.dumps({'rich_text':[]}),json.dumps('benign display')))
                    if imported_code:
                        db.execute("CREATE TRIGGER revive AFTER UPDATE ON policy_apply_receipts WHEN new.state='restored' BEGIN UPDATE policy_apply_receipts SET state='applied' WHERE id=new.id; END")
                    db.commit()
                database.backup_to(str(incoming))
                self.assertIsNone(api_server._validate_restore_candidate(str(incoming)))
                api_server._commit_database_restore(str(incoming),str(live),str(safety))
                with patch.object(policy_undo.notion_service,'get_page',new_callable=AsyncMock) as read,patch.object(policy_undo.notion_service,'update_page_properties',new_callable=AsyncMock) as write:
                    result=await policy_undo.undo(1,'support','receipt')
                    self.assertEqual(result['error'],'UNDO_UNAVAILABLE')
                    read.assert_not_called();write.assert_not_called()
                with database.get_connection() as db:
                    self.assertEqual(db.execute('SELECT state FROM policy_apply_receipts').fetchone()[0],'restored')
                    self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='revive'").fetchone())
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name IN ('welfare_items_ai','welfare_items_ad','welfare_items_au')").fetchone()[0],3)
                database.close_all_connections()

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_static_fetch_cannot_skip_private_address_check(self):
        import ai_classifier
        from unittest.mock import Mock
        session=SimpleNamespace(get=Mock(side_effect=AssertionError('must not connect')))
        await ai_classifier.fetch_page_content_async(session,'http://127.0.0.1/private')
        session.get.assert_not_called()

    async def test_attachment_injected_validator_cannot_override_private_address_boundary(self):
        from services.news_attachments import _download_attachment
        from unittest.mock import Mock
        session=SimpleNamespace(get=Mock(side_effect=AssertionError('must not connect')))
        async def accepts(url):return url
        with self.assertRaises(ValueError):
            await _download_attachment(session,{'url':'http://127.0.0.1/file.pdf'},url_validator=accepts)
        session.get.assert_not_called()

    async def test_context_uses_authenticated_proxy_and_closes_it(self):
        context=SimpleNamespace(on=Mock(),close=AsyncMock())
        browser=SimpleNamespace(new_context=AsyncMock(return_value=context))
        result=await net.new_public_context(browser,ignore_https_errors=True)
        self.assertIs(result,context)
        options=browser.new_context.call_args.kwargs
        self.assertFalse(options['ignore_https_errors'])
        self.assertEqual(options['proxy']['bypass'],'<-loopback>')
        self.assertTrue(options['proxy']['password'])
        context.on.assert_called_once()
        context.on.call_args.args[1]()  # Real Playwright emits this on close.

    async def test_actual_dns_rows_checked_and_passed_to_connector_without_reresolve(self):
        delegate=SimpleNamespace(resolve=AsyncMock(),close=AsyncMock())
        def rows(ip):return [dict(hostname='source.example',host=ip,port=443,family=socket.AF_INET,proto=0,flags=0)]
        delegate.resolve.side_effect=[rows('8.8.8.8'),rows('127.0.0.1')]
        resolver=net.PublicResolver(delegate)
        connector=aiohttp.TCPConnector(resolver=resolver,use_dns_cache=False)
        try:
            accepted=await connector._resolve_host('source.example',443)
            self.assertEqual(accepted[0]['host'],'8.8.8.8')
            infos=connector._convert_hosts_to_addr_infos(accepted)
            self.assertEqual(infos[0][4][0],'8.8.8.8')
            self.assertEqual(delegate.resolve.await_count,1)
            with self.assertRaises(OSError):await connector._resolve_host('source.example',443)
        finally:await connector.close()

    async def test_mixed_public_private_dns_answer_rejected(self):
        d=SimpleNamespace(resolve=AsyncMock(return_value=[{'host':'8.8.8.8'},{'host':'10.0.0.1'}]),close=AsyncMock())
        with self.assertRaises(OSError):await net.PublicResolver(d).resolve('source.example',443)

    async def test_private_literal_forms_rejected(self):
        for url in ('http://127.0.0.1','http://localhost','http://10.0.0.1',
                    'http://169.254.169.254','http://[::1]','http://[::ffff:127.0.0.1]',
                    'file:///x','https://user:password@example.org','https://example.org\\@127.0.0.1'):
            with self.subTest(url=url),self.assertRaises(ValueError):net.public_url(url)

    async def test_redirect_to_private_never_starts_second_request(self):
        class Context:
            async def __aenter__(self):return SimpleNamespace(status=302,headers={'Location':'http://127.0.0.1/private'})
            async def __aexit__(self,*args):pass
        calls=[]
        class Session:
            def get(self,url,**kwargs):calls.append((url,kwargs));return Context()
        with self.assertRaises(ValueError):
            async with net.public_get(Session(),'https://source.example'):pass
        self.assertEqual(len(calls),1);self.assertFalse(calls[0][1]['allow_redirects'])

class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_authentication_and_private_tunnel_fail_before_connect(self):
        from services.public_browser_proxy import PublicBrowserProxy
        proxy=await PublicBrowserProxy().start()
        port=proxy.server.sockets[0].getsockname()[1]
        try:
            for authenticated,status in ((False,b'407'),(True,b'502')):
                reader,writer=await asyncio.open_connection('127.0.0.1',port)
                credential=b'Proxy-Authorization: '+proxy.authorization+b'\r\n' if authenticated else b''
                writer.write(b'CONNECT 127.0.0.1:8421 HTTP/1.1\r\n'+credential+b'\r\n')
                await writer.drain()
                with patch.object(proxy,'_open_address',new_callable=AsyncMock) as connect:
                    reply=await asyncio.wait_for(reader.read(),2)
                    self.assertIn(status,reply.split(b'\r\n')[0]);connect.assert_not_called()
                writer.close();await writer.wait_closed()
        finally:proxy.close()

    async def test_dns_result_is_exact_numeric_connection_target(self):
        from services.public_browser_proxy import PublicBrowserProxy
        proxy=PublicBrowserProxy()
        row=(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))
        loop=asyncio.get_running_loop()
        with patch.object(loop,'getaddrinfo',new_callable=AsyncMock,return_value=[row]) as resolve,patch.object(proxy,'_open_address',new_callable=AsyncMock,return_value=('reader','writer')) as connect:
            self.assertEqual(await proxy._connect('public.example',443),('reader','writer'))
            resolve.assert_awaited_once();connect.assert_awaited_once_with(row)
        private=(socket.AF_INET,socket.SOCK_STREAM,6,'',('10.0.0.1',443))
        with patch.object(loop,'getaddrinfo',new_callable=AsyncMock,return_value=[row,private]),patch.object(proxy,'_open_address',new_callable=AsyncMock) as connect:
            with self.assertRaises(ValueError):await proxy._connect('public.example',443)
            connect.assert_not_called()
