import json
import unittest
from unittest.mock import patch
from app.context_usage import estimate, reported
from app.api_client import DeepSeekClient

class ContextUsageTests(unittest.TestCase):
    def test_image_bytes_are_not_counted_as_text_tokens(self):
        result = estimate([{'role':'user','content':[{'type':'text','text':'测试正文'}, {'type':'image_url','image_url':{'url':'data:image/png;base64,'+'x'*100000}}]}])
        self.assertEqual(result['text_chars'],4)
        self.assertEqual(result['images'],1)
        self.assertLess(result['input_tokens'],100)
        self.assertIsNone(result['total_tokens'])

    def test_reported_tokens_override_estimate_and_missing_usage_remains_unknown(self):
        value=estimate([{'role':'user','content':'hello'}])
        self.assertEqual(reported(value,{}),value)
        actual=reported(value,{'usage':{'prompt_tokens':1000,'completion_tokens':200}})
        self.assertEqual(actual['total_tokens'],1200)
        self.assertEqual(actual['kind'],'reported')
        self.assertEqual(reported(value,{'usage':{'prompt_tokens':-1}}),value)

    def test_transport_notifies_estimate_then_actual_without_payload(self):
        client=DeepSeekClient('https://example.invalid','example','secret')
        seen=[];client.on_context=seen.append
        response=json.dumps({'usage':{'prompt_tokens':12,'completion_tokens':3},'choices':[{'message':{'content':'answer'}}]})
        with patch('app.api_client.post_json',return_value=response):
            self.assertEqual(client.chat([{'role':'user','content':'private question'}]),'answer')
        self.assertEqual([r['kind'] for r in seen],['estimate','reported'])
        self.assertEqual(seen[-1]['total_tokens'],15)
        self.assertNotIn('private question',json.dumps(seen))
        self.assertNotIn('secret',json.dumps(seen))
