import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from app.workspace_server import Workspace
from app.api_client import DeepSeekClient
from app import conversation_budget as cb


class ConversationBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.w=Workspace(Path(self.temp.name))
        self.w.store.model_config=lambda:{'base_url':'https://api.deepseek.com/v1','model':'deepseek-flash','key':'test'}
        self.a=self.w.store.import_article('测试原文',[{'type':'text','text':'钙钛矿阈值为83摄氏度。'}])
        self.key=self.a['id']+':'+self.a['version']+':'
        self.client=Mock(model='deepseek-flash',base_url='https://api.deepseek.com/v1')
        self.client.chat.return_value='阈值为83摄氏度。[S1]'
        self.w.client=lambda:self.client

    def tearDown(self):
        self.temp.cleanup()

    def test_verified_capacity_default_and_validation(self):
        self.assertEqual(cb.policy(self.w.store)['capacity'],1000000)
        self.assertEqual(cb.policy(self.w.store)['budget'],16384)
        self.assertEqual(cb.save_policy(self.w.store,32768)['budget'],32768)
        for value in (1000001,4096,True,'16384'):
            with self.assertRaises(ValueError):cb.save_policy(self.w.store,value)

    def test_unverified_provider_cannot_inherit_official_capacity(self):
        self.w.store.model_config=lambda:{'base_url':'https://example.invalid','model':'deepseek-flash'}
        self.assertIsNone(cb.policy(self.w.store)['capacity'])
        with self.assertRaises(ValueError):cb.save_policy(self.w.store,32768)

    def test_image_reserve_requests_budget_change_without_closing(self):
        saved={'messages':[{'role':'user','content':'Existing question'}],'draft':'Image question'}
        self.w.store.put('sessions',self.key,saved)
        messages=[{'role':'user','content':[{'type':'text','text':'Explain'},{'type':'image_url','image_url':{'url':'data:image/png;base64,test'}}]}]
        with self.assertRaisesRegex(ValueError,'图片预留'):
            cb.guard(self.w.store,self.key,'Image question',messages,cb.policy(self.w.store))
        self.assertEqual(self.w.store.get('sessions',self.key),saved)
        cb.save_policy(self.w.store,32768)
        cb.guard(self.w.store,self.key,'Image question',messages,cb.policy(self.w.store))

    def test_full_history_closes_before_network_and_keeps_handoff(self):
        old={'messages':[{'role':'user','content':'历史研究问题'*3000}], 'draft':''}
        self.w.store.put('sessions',self.key,old)
        j={'cancel':threading.Event()}
        self.w.work(j,'chat',{'article_id':self.a['id'],'question':'钙钛矿阈值？'})
        self.assertEqual(j['status'],'done')
        self.assertTrue(j['result']['closed'])
        self.assertIn('历史研究问题',j['result']['handoff'])
        self.assertIn('钙钛矿阈值',j['result']['handoff'])
        self.client.chat.assert_not_called()
        fresh=cb.restart(self.w.store,self.key)
        self.assertEqual(fresh['messages'],[])
        self.assertEqual(len(fresh['archives']),1)
        self.assertEqual(fresh['draft'],'钙钛矿阈值？')
        self.assertLessEqual(len(fresh['carryover']),1500)

    def test_closed_conversation_cannot_be_reopened_by_larger_budget(self):
        cb.close(self.w.store,self.key,'问题','预算已满')
        cb.save_policy(self.w.store,1000000)
        with self.assertRaises(cb.ConversationEnded):
            cb.guard(self.w.store,self.key,'新问题',[],cb.policy(self.w.store))

    def test_reported_over_budget_keeps_last_answer_and_closes(self):
        j={'conversation_policy':cb.policy(self.w.store)}
        s={'messages':[{'role':'assistant','content':'最后的回答'}], 'context_usage':{'total_tokens':17000}}
        result=cb.finish(self.w.store,self.key,s,j)
        self.assertTrue(result['closed'])
        self.assertIn('最后的回答',result['handoff'])

    def test_client_reserves_output_in_actual_payload(self):
        c=DeepSeekClient('https://example.invalid','test','secret');c.chat_output_limit=4096
        response=json.dumps({'choices':[{'message':{'content':'ok'}}]})
        with patch('app.api_client.post_json',return_value=response) as transport:
            c.chat([{'role':'user','content':'test'}])
        self.assertEqual(transport.call_args.args[1]['max_tokens'],4096)

    def test_cancel_does_not_generate_handoff(self):
        stop=threading.Event();stop.set()
        with self.assertRaises(ValueError):cb.guard(self.w.store,self.key,'x'*30000,[{'content':'x'*30000}],cb.policy(self.w.store),stop)
        self.assertIsNone(self.w.store.get('sessions',self.key))

    def test_handoff_keeps_exact_evidence(self):
        s={'messages':[{'role':'assistant','content':'回答[S1]','evidence':[{'article_title':'来源','article_id':'a','version':'v','block':'b1','quote':'原文证据'}]}]}
        text=cb.handoff(s,'待解决的问题','到达预算')
        self.assertIn('原文证据',text);self.assertIn('版本 v',text);self.assertIn('待解决的问题',text)


if __name__=='__main__':unittest.main()
