"""全文导图的基准、拒绝错误、存档与画图/导出回归。模拟模型仅验证流程，不代表模型准确率。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

from app import article_map as am, map_export
from app.api_client import ApiError
from app.config import Config
from app.map_service import MAP_VALIDATION_VERSION, MapGraph, MapNode, MapService
from app.ui import concept_map as cm
from tests import support

FIXTURES = json.loads((Path(__file__).parent/"fixtures/article-maps.json").read_text(encoding="utf-8"))["fixtures"]


def reading_for(fixture):
    return {"claims":[{"subject":r["src"],"predicate":r["label"],"object":r["dst"],
                       "quote":r["quote"],"relation_required":True} for r in fixture["draft"]["relations"]],
            "selections":[{"selection_index":b["selection_index"],
                           "quote":next(n["quote"] for n in fixture["draft"]["nodes"] if n["id"]==b["node_ids"][0]),
                           "note":"按基准原句定位"} for b in fixture["draft"]["bindings"]]}


def supported_review(draft, reading):
    return {"node_verdicts":[{"id":n["id"],"verdict":"supported","note":"基准原句支持"} for n in draft["nodes"]],
            "relation_verdicts":[{"id":r["id"],"verdict":"supported","note":"基准方向和条件支持"} for r in draft["relations"]],
            "binding_verdicts":[{"selection_index":b["selection_index"],"verdict":"supported","note":"基准词义支持"} for b in draft["bindings"]],
            "claim_coverage":[{"claim_index":i,"node_ids":[],"relation_ids":[draft["relations"][i]["id"]]}
                              for i in range(len(reading["claims"]))],
            "coverage_ok":True,"issues":[],"reader_issues":[]}


class FixtureClient:
    def __init__(self, fixture, fail_rounds=0, fail_always=False):
        self.fixture=fixture;self.calls=[];self.rounds=0
        self.fail_rounds=fail_rounds;self.fail_always=fail_always

    def full_article_map(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        stage=kwargs["stage"]
        if stage=="read":
            return reading_for(self.fixture)
        if stage=="generate":
            self.rounds+=1
            data=copy.deepcopy(self.fixture["draft"])
            if self.rounds<=self.fail_rounds:
                r=data["relations"][0];r["src"],r["dst"]=r["dst"],r["src"]
            return data
        if stage=="repair-quotes":
            return {'quotes': [], 'selections': []}
        result=supported_review(kwargs["draft"],kwargs["reading"])
        if self.rounds<=self.fail_rounds or self.fail_always:
            result["relation_verdicts"][0].update(verdict="contradicted",note="把水温条件与香气结果的方向讲反了")
            result["issues"]=["回原文修正方向，不能由香气结果反推水温条件"]
        return result


def saved_nodes(fixture):
    return [MapNode(i+10,word) for i,word in enumerate(fixture["selected_words"])]


class TestArticleMapContract(unittest.TestCase):
    def test_generation_gets_complete_validated_outline_and_reader_never_sees_draft(self):
        f=FIXTURES[0];client=FixtureClient(f)
        am.run(client,text=f['text'],selected_words=f['selected_words'])
        self.assertNotIn('draft',client.calls[0])
        self.assertNotIn('reading',client.calls[0])
        outline=client.calls[1]['reading']
        self.assertEqual(reading_for(f),outline)
        request=am.payload('test',stage='generate',text=f['text'],reading=outline)
        material=json.loads(request['messages'][1]['content'])
        self.assertEqual(outline,material['coverage_outline'])
        self.assertEqual(f['text'],''.join(line['text'] for line in material['source_lines']))

    def test_interrupted_generation_reuses_independent_reading(self):
        f=FIXTURES[0]
        saved=[]
        class Interrupted(FixtureClient):
            def full_article_map(self, **kwargs):
                if kwargs['stage']=='generate':
                    raise ApiError('network','模拟网络中断')
                return super().full_article_map(**kwargs)
        first=Interrupted(f)
        with self.assertRaises(ApiError):
            am.run(first,text=f['text'],selected_words=f['selected_words'],checkpoint=saved.append)
        resumed=FixtureClient(f)
        graph,quality=am.run(resumed,text=f['text'],selected_words=f['selected_words'],resume=saved[-1])
        self.assertEqual([call['stage'] for call in resumed.calls],['generate','review'])
        self.assertTrue(quality['approved'])
        self.assertEqual(graph['root'],f['draft']['root'])
        for invalid in (dict(saved[-1], source_sha256='another article'),
                        dict(saved[-1], selected_words=[]),
                        dict(saved[-1], reading={'claims':[],'selections':[]})):
            fresh=FixtureClient(f)
            am.run(fresh,text=f['text'],selected_words=f['selected_words'],resume=invalid)
            self.assertEqual('read',fresh.calls[0]['stage'])

    def test_pending_draft_still_requires_semantic_review_and_valid_source(self):
        f=FIXTURES[0]
        saved=[]
        class Interrupted(FixtureClient):
            def full_article_map(self, **kwargs):
                if kwargs['stage']=='review':
                    raise ApiError('network','模拟核对中断')
                return super().full_article_map(**kwargs)
        with self.assertRaises(ApiError):
            am.run(Interrupted(f),text=f['text'],selected_words=f['selected_words'],checkpoint=saved.append)
        resumed=FixtureClient(f,fail_always=True)
        with self.assertRaises(ApiError):
            am.run(resumed,text=f['text'],selected_words=f['selected_words'],resume=saved[-1])
        self.assertEqual('review',resumed.calls[0]['stage'])
        self.assertEqual(3,len([call for call in resumed.calls if call['stage']=='review']))
        bad=copy.deepcopy(saved[-1]);bad['pending_draft']['nodes'][0]['quote']='原文未出现的引用'
        regenerated=FixtureClient(f)
        am.run(regenerated,text=f['text'],selected_words=f['selected_words'],resume=bad)
        self.assertEqual('read',regenerated.calls[0]['stage'])

    def test_model_line_citations_become_original_text_before_review_and_storage(self):
        fixture = FIXTURES[0]
        class LinesClient(FixtureClient):
            def full_article_map(self, **kwargs):
                result = super().full_article_map(**kwargs)
                if kwargs['stage'] == 'read':
                    for item in result['claims'] + result['selections']:
                        item['quote'] = {'line_start': 1, 'line_end': len(fixture['text'].splitlines())}
                if kwargs['stage'] == 'generate':
                    for item in result['nodes'] + result['relations']:
                        item['quote'] = {'line_start': 1, 'line_end': len(fixture['text'].splitlines())}
                return result
        client = LinesClient(fixture)
        graph, quality = am.run(client, text=fixture['text'], selected_words=fixture['selected_words'])
        self.assertTrue(quality['approved'])
        for item in graph['nodes'] + graph['relations']:
            self.assertIsInstance(item['quote'], str)
            self.assertIn(item['quote'], fixture['text'])
        for value in ({'line_start': 0, 'line_end': 1}, {'line_start': 1, 'line_end': 1000},
                      {'line_start': True, 'line_end': 1}, {'line_start': 2, 'line_end': 1}):
            with self.subTest(value=value), self.assertRaises(am.ArticleMapError):
                am._quote({'quote': value}, fixture['text'])

    def test_quote_repair_keeps_independent_claims_and_receives_no_map_draft(self):
        fixture = FIXTURES[0]
        class QuoteClient(FixtureClient):
            def full_article_map(self, **kwargs):
                if kwargs['stage'] == 'repair-quotes':
                    self.calls.append(copy.deepcopy(kwargs))
                    self.assert_independent(kwargs)
                    return {'quotes': [{'claim_index': 0, 'quote': fixture['draft']['relations'][0]['quote'],
                                        'subject': '不能把这个字段写入清单'}], 'selections': []}
                result = super().full_article_map(**kwargs)
                if kwargs['stage'] == 'read':
                    result['claims'][0]['quote'] = '模型错误地合并了跨页句子'
                return result

            def assert_independent(self, kwargs):
                if 'draft' in kwargs:
                    raise AssertionError('引用定位不得接收导图草稿')
        client = QuoteClient(fixture)
        _, quality = am.run(client, text=fixture['text'], selected_words=fixture['selected_words'])
        self.assertEqual([c['stage'] for c in client.calls], ['read', 'repair-quotes', 'generate', 'review'])
        self.assertEqual(quality['independent_reading']['claims'][0], reading_for(fixture)['claims'][0])
        request = am.payload('test', stage='repair-quotes', text=fixture['text'],
                             reading=client.calls[1]['reading'], draft={'secret_draft': 'excluded'})
        self.assertNotIn('secret_draft', json.dumps(request))

    def test_quote_repair_preserves_intervening_page_material_and_rejects_fabrication(self):
        source = 'Read on.\n\n2\n\nContinue carefully.'
        reading = {'claims': [{'subject': 'Continue carefully', 'predicate': '记录信息', 'object': '',
                              'relation_required': False, 'quote': 'Read on. Continue carefully.'}], 'selections': []}
        client = mock.Mock()
        client.full_article_map.return_value = {'quotes': [{'claim_index': 0, 'quote': source}], 'selections': []}
        corrected = am.repair_reading_quotes(client, reading, source)
        self.assertEqual(corrected['claims'][0]['quote'], source)
        self.assertEqual(reading['claims'][0]['quote'], 'Read on. Continue carefully.')
        for edits in ([{'claim_index': 0, 'quote': 'Read on. Continue carelessly.'}],
                      [], [{'claim_index': 0, 'quote': source}, {'claim_index': 0, 'quote': source}]):
            client.full_article_map.return_value = {'quotes': edits, 'selections': []}
            with self.subTest(edits=edits), self.assertRaises(am.ArticleMapError):
                am.repair_reading_quotes(client, reading, source)

    def test_quote_line_ranges_restore_exact_unicode_and_can_compact_only_invalid_fields(self):
        source = 'Relation labels:\nΩt = holding/touching, on/supporting, inside, attached_to.\n2\nEnd.'
        reading = {'claims': [{'subject': '关系范围', 'predicate': '仅评估' + 'holding/touching, on/supporting, inside, attached_to',
                              'object': '统一关系范围', 'relation_required': True, 'quote': 'Relation labels:'}], 'selections': []}
        client = mock.Mock()
        client.full_article_map.return_value = {'quotes': [{'claim_index': 0, 'line_start': 2, 'line_end': 3,
            'subject': '关系范围', 'predicate': '仅评估', 'object': 'holding/touching, on/supporting, inside, attached_to',
            'relation_required': False}], 'selections': []}
        corrected = am.repair_reading_quotes(client, reading, source)
        self.assertEqual(corrected['claims'][0]['quote'], source.split('\n', 1)[1].rsplit('\n', 1)[0])
        self.assertTrue(corrected['claims'][0]['relation_required'])
        self.assertEqual(corrected['claims'][0]['predicate'], '仅评估')
        payload = am.payload('test', stage='repair-quotes', text=source, reading={})
        material = json.loads(payload['messages'][1]['content'])
        self.assertEqual(''.join(line['text'] for line in material['source_lines']), source)
        for start, end in ((0, 2), (2, 100), (3, 2), (True, 2)):
            client.full_article_map.return_value['quotes'][0].update(line_start=start, line_end=end)
            with self.subTest(start=start, end=end), self.assertRaises(am.ArticleMapError):
                am.repair_reading_quotes(client, reading, source)

    def test_pdf_quote_whitespace_restores_exact_source_span(self):
        source = ('A foundation-model\n    pipeline identifies manipulation roles, '
                  'grounds persistent object masks, infers\nrelationships.')
        quoted = ('A foundation-model pipeline identifies manipulation roles, '
                  'grounds persistent object masks, infers')
        restored = am._quote({'quote': quoted}, source)
        self.assertEqual(restored, source[:source.index('\nrelationships.')])
        self.assertIn(restored, source)
        self.assertEqual(am._quote({'quote': 'visual\n and temporal quality'},
                                   'visual and temporal quality'), 'visual and temporal quality')
        self.assertEqual(am._quote({'quote': 'manipulated-object motion'},
                                   'manipulated-\nobject motion'), 'manipulated-\nobject motion')

    def test_pdf_quote_cannot_change_words_punctuation_numbers_or_join_excerpts(self):
        source = 'Benchmarks assess\nvisual quality, but do not prove safety. Score: 41.'
        for quote in ('Benchmarks assess quality', 'visual quality but do not prove safety',
                      'visual quality, and do not prove safety', 'Score: 42.',
                      'Benchmarks assess…Score: 41.', 'benchmarks assess', 'visualquality'):
            with self.subTest(quote=quote), self.assertRaises(am.ArticleMapError):
                am._quote({'quote': quote}, source)
        for quote in ('manipulated object', 'manipulatedobject'):
            with self.subTest(quote=quote), self.assertRaises(am.ArticleMapError):
                am._quote({'quote': quote}, 'manipulated-\nobject')

    def test_reader_selection_keeps_original_pdf_whitespace(self):
        source = 'Inspect\n  visual quality before release.'
        reading = {'claims': [{'subject': 'Inspect visual quality', 'predicate': '记录信息',
                              'object': '', 'quote': 'Inspect visual quality', 'relation_required': False}],
                   'selections': [{'selection_index': 0, 'quote': 'Inspect visual quality', 'note': ''}]}
        result = am.validate_reading(reading, source, ['visual'])
        self.assertEqual(result['claims'][0]['quote'], 'Inspect\n  visual quality')
        self.assertEqual(result['selections'][0]['quote'], 'Inspect\n  visual quality')

    def test_progress_reports_each_request_and_repair_round(self):
        fixture = FIXTURES[0]
        messages = []
        am.run(FixtureClient(fixture, fail_rounds=1), text=fixture['text'],
               selected_words=fixture['selected_words'], progress=messages.append)
        self.assertEqual(len(messages), 5)
        self.assertIn('阅读文章', messages[0])
        self.assertIn('整理导图', messages[1])
        self.assertIn('核对原文', messages[2])
        self.assertIn('完善导图', messages[3])
        self.assertIn('核对原文', messages[4])


    def test_three_approved_previews_preserve_full_structure_and_all_relations(self):
        for fixture in FIXTURES:
            with self.subTest(file=fixture["file"]):
                model=FixtureClient(fixture)
                data,quality=am.run(model,text=fixture["text"],selected_words=fixture["selected_words"])
                nodes,rels,ids=am.projection(data,saved_nodes(fixture))
                self.assertEqual(len(nodes),len(fixture["draft"]["nodes"]))
                self.assertEqual(len(rels),len(fixture["draft"]["relations"]))
                self.assertTrue(any(n.entry_id<0 for n in nodes),"新增概念必须保留")
                self.assertEqual([c["stage"] for c in model.calls],["read","generate","review"])
                self.assertNotIn("draft",model.calls[0])
                self.assertTrue(quality["approved"])
                self.assertEqual(quality["rounds"],1)
                for r in rels:
                    self.assertIn(r.evidence,fixture["text"])

    def test_actual_call_material_keeps_reader_independent_and_excludes_old_explanations(self):
        f=FIXTURES[0]
        p=am.payload("configured",stage="read",text=f["text"],selected_words=f["selected_words"],draft={"secret_draft":"do not read"})
        material=json.loads(p["messages"][1]["content"])
        self.assertEqual(set(material),{"source_lines","selected_words"})
        self.assertEqual(''.join(line['text'] for line in material['source_lines']),f['text'])
        self.assertEqual([line['line'] for line in material['source_lines']], list(range(1, len(material['source_lines'])+1)))
        self.assertNotIn("secret_draft",json.dumps(p))

    def test_no_silent_full_article_truncation(self):
        model=FixtureClient(FIXTURES[0])
        with self.assertRaises(ApiError):
            am.run(model,text="文"*(am.MAX_TEXT_CHARS+1))
        self.assertEqual(model.calls,[])

    def test_reader_retries_receive_corrections_without_generation_draft(self):
        f=FIXTURES[0]
        correction={"claim_index":0,"issue":"完整动作不要机械拆为句法关系"}
        class ReaderClient(FixtureClient):
            def full_article_map(self,**kwargs):
                if kwargs["stage"]=="read" and self.rounds:
                    self_reader_feedback=kwargs.get("feedback")
                    if self_reader_feedback != [correction]:
                        raise AssertionError("重读没有收到独立清单纠错")
                    if "draft" in kwargs or "reading" in kwargs:
                        raise AssertionError("重读不能收到生成草稿")
                result=super().full_article_map(**kwargs)
                if kwargs["stage"]=="review" and self.rounds==1:
                    result["reader_issues"]=[correction]
                return result
        model=ReaderClient(f)
        _,quality=am.run(model,text=f["text"],selected_words=f["selected_words"])
        self.assertEqual(quality["rounds"],2)
        p=am.payload("configured",stage="read",text=f["text"],feedback=[correction],
                     draft={"secret_draft":"do not read"})
        material=json.loads(p["messages"][1]["content"])
        self.assertEqual(material["reading_corrections"],[correction])
        self.assertNotIn("secret_draft",json.dumps(p))

    def test_invalid_reader_quote_retries_with_specific_feedback(self):
        f=FIXTURES[0]
        class InvalidReader(FixtureClient):
            def full_article_map(self,**kwargs):
                result=super().full_article_map(**kwargs)
                if kwargs["stage"]=="read" and not kwargs.get("feedback"):
                    result["claims"][0]["quote"]="原文没有这段话"
                return result
        model=InvalidReader(f)
        _,quality=am.run(model,text=f["text"],selected_words=f["selected_words"])
        self.assertEqual(quality["rounds"],2)
        reads=[call for call in model.calls if call["stage"]=="read"]
        self.assertIn("逐字",reads[-1]["feedback"][0])

    def test_short_source_concept_is_valid_and_meaningful_selection_is_not_a_fragment(self):
        f=FIXTURES[2];d=copy.deepcopy(f["draft"])
        am._quote({"quote":"阅读"},f["text"])
        target=d["bindings"][0]["node_ids"][0]
        next(n for n in d["nodes"] if n["id"]==target)["kind"]="selection"
        with self.assertRaises(am.ArticleMapError):
            am.validate_draft(d,f["text"],f["selected_words"])

    def test_backpack_completion_and_user_marker_are_retained(self):
        f=FIXTURES[1];d=am.validate_draft(f["draft"],f["text"],f["selected_words"])
        nodes,rels,ids=am.projection(d,saved_nodes(f))
        self.assertEqual(next(n.term for n in nodes if n.entry_id==10),"整理背包")
        self.assertEqual(next(n.term for n in nodes if n.entry_id==13),"首先")
        self.assertTrue(d["bindings"][3]["suggest_exclude"])
        self.assertTrue(any(r.src_entry_id<0 or r.dst_entry_id<0 for r in rels))

    def test_bad_quotes_cycles_missing_words_and_classification_labels_are_rejected(self):
        f=FIXTURES[0]
        mutations=[lambda d:d["nodes"][1].update(quote="网上说所有水温都可以"),
                   lambda d:d["nodes"][1].update(parent=d["nodes"][1]["id"]),
                   lambda d:d.update(bindings=[]),
                   lambda d:d["relations"][0].update(label="因果"),
                   lambda d:d["relations"][0].update(src={}),
                   lambda d:d["bindings"][0].update(node_ids=[{}])]
        for mutate in mutations:
            data=copy.deepcopy(f["draft"]);mutate(data)
            with self.assertRaises(am.ArticleMapError):
                am.validate_draft(data,f["text"],f["selected_words"])

    def test_partial_review_and_unexpressed_real_relations_do_not_pass(self):
        f=FIXTURES[0];d=am.validate_draft(f["draft"],f["text"],f["selected_words"]);r=reading_for(f)
        review=supported_review(d,r)
        review["node_verdicts"].pop()
        review["claim_coverage"][0]={"claim_index":0,"node_ids":["high"],"relation_ids":[]}
        issues=am.review_issues(d,r,review)
        self.assertTrue(any("逐项复核" in x for x in issues))
        self.assertTrue(any("真实关系未画出" in x for x in issues))

    def test_reversed_direction_is_repaired_with_feedback_and_maximum_three_rounds(self):
        f=FIXTURES[0];model=FixtureClient(f,fail_rounds=2);attempts=[]
        data,q=am.run(model,text=f["text"],selected_words=f["selected_words"],on_attempt=lambda *args:attempts.append(args))
        self.assertEqual(q["rounds"],3)
        self.assertEqual(len(attempts),3)
        self.assertTrue(attempts[0][2]);self.assertFalse(attempts[-1][2])
        self.assertTrue(model.calls[3]["feedback"])
        self.assertEqual(data["relations"][0]["src"],"high")
        model=FixtureClient(f,fail_always=True)
        with self.assertRaises(ApiError):
            am.run(model,text=f["text"],selected_words=f["selected_words"])
        self.assertEqual(model.rounds,3)


class TestArticleMapIntegration(unittest.TestCase):
    def prepare(self,db,fixture):
        topic=db.create_batch("全文验证")
        nodes=[MapNode(db.add_entry(batch_id=topic,term=w,doc_key="fixture"),w) for w in fixture["selected_words"]]
        config=Config(db);service=MapService(db,config)
        article={"doc_key":"fixture","title":fixture["file"],"status":"ok","text":fixture["text"]}
        fp=service.fingerprint(topic,nodes,article=article)
        service.make_client=lambda **kwargs:FixtureClient(fixture)
        return topic,nodes,service,article,fp

    def test_cache_roundtrip_does_not_truncate_tree_or_drop_extra_concepts(self):
        with support.temp_db() as db:
            f=FIXTURES[0];topic,nodes,service,article,fp=self.prepare(db,f)
            results=[];service.set_result_sink(lambda *args:results.append(args))
            service._worker_article(1,topic,fp,nodes,(article,f["text"]))
            self.assertEqual(results[-1][3],"ok")
            graph=service.cached_graph(topic,nodes,article=article)
            self.assertEqual(len(graph.nodes),len(f["draft"]["nodes"]))
            self.assertEqual(len(graph.relations),len(f["draft"]["relations"]))
            row=db.get_map_graph(topic,fp)
            self.assertGreater(len(row["raw"]),4000)
            self.assertEqual(json.loads(row["raw"])["article_map"],graph.article_map)
            self.assertEqual(row["validation_version"],MAP_VALIDATION_VERSION)

    def test_failed_new_drafts_never_overwrite_accepted_cache(self):
        with support.temp_db() as db:
            f=FIXTURES[0];topic,nodes,service,article,fp=self.prepare(db,f)
            results=[];service.set_result_sink(lambda *args:results.append(args))
            service._worker_article(1,topic,fp,nodes,(article,f["text"]))
            previous=db.get_map_graph(topic,fp)["raw"]
            service.make_client=lambda **kwargs:FixtureClient(f,fail_always=True)
            service._worker_article(2,topic,fp,nodes,(article,f["text"]))
            self.assertEqual(results[-1][3],"error")
            self.assertEqual(db.get_map_graph(topic,fp)["raw"],previous)
            self.assertTrue(json.loads(db.latest_article_map_draft(topic)["issues"]))

    def test_same_length_source_changes_invalidate_cache(self):
        with support.temp_db() as db:
            f=FIXTURES[0];topic,nodes,s,a,fp=self.prepare(db,f)
            changed=dict(a,text=a["text"].replace("热水","冷水"))
            self.assertEqual(len(a["text"]),len(changed["text"]))
            self.assertNotEqual(fp,s.fingerprint(topic,nodes,article=changed))

    def test_thinking_configuration_invalidates_article_cache(self):
        with support.temp_db() as db:
            topic,nodes,s,a,fp=self.prepare(db,FIXTURES[0])
            s.config.set("api.map_thinking","never")
            self.assertNotEqual(fp,s.fingerprint(topic,nodes,article=a))

    def test_article_policy_revision_invalidates_only_article_fingerprint(self):
        with support.temp_db() as db:
            topic,nodes,s,a,fp=self.prepare(db,FIXTURES[0])
            word_fp=s.fingerprint(topic,nodes)
            with mock.patch.object(am,"POLICY_VERSION",am.POLICY_VERSION+1):
                self.assertNotEqual(fp,s.fingerprint(topic,nodes,article=a))
                self.assertEqual(word_fp,s.fingerprint(topic,nodes))

    def test_extra_concept_positions_and_block_edits_have_no_entry_foreign_key(self):
        with support.temp_db() as db:
            f=FIXTURES[0];topic,nodes,s,a,fp=self.prepare(db,f)
            db.set_article_map_edit(topic,fp,"pin","-3",[100,200])
            db.set_article_map_edit(topic,fp,"block","[-5, -3]",{"label":"会"})
            self.assertEqual(len(db.article_map_edits(topic,fp,"pin")),1)
            self.assertEqual(db.clear_article_map_edits(topic,fp,"pin"),1)
            self.assertEqual(len(db.article_map_edits(topic,fp,"block")),1)

    def test_full_article_keeps_collections_beyond_legacy_sample_limit(self):
        from app.article_source import Article
        with support.temp_db() as db:
            f=FIXTURES[0];topic,nodes,s,a,fp=self.prepare(db,f)
            db.put_article("fixture",Article(ok=True,text=f["text"],status="ok",method="test"))
            for i in range(30):
                db.add_entry(batch_id=topic,term=f"收藏{i}",doc_key="fixture")
            self.assertEqual(len(s.nodes_for_topic(topic)),34)

    def test_depth_is_only_a_view_and_relationship_paths_remain_visible(self):
        for f in FIXTURES:
            data=am.validate_draft(f["draft"],f["text"],f["selected_words"])
            saved=saved_nodes(f);nodes,relations,ids=am.projection(data,saved)
            graph=MapGraph(1,"fp",nodes=nodes,relations=relations,article_map=data)
            before=copy.deepcopy(data)
            for r in relations:
                layout=cm.layout_article_graph(graph,saved,width=900,height=700,depth=1,focus_entries=[saved[0].entry_id],relations=[r])
                shown={n.entry_id for n in layout.nodes}
                self.assertIn(r.src_entry_id,shown);self.assertIn(r.dst_entry_id,shown)
                self.assertEqual(len(layout.edges),1)
                self.assertEqual(layout.edges[0].label,r.reason)
                self.assertIsNotNone(layout.edges[0].label_pos)
            self.assertEqual(data,before)

    def test_full_export_retains_tree_all_natural_labels_quotes_and_highlights(self):
        for f in FIXTURES:
            data=am.validate_draft(f["draft"],f["text"],f["selected_words"])
            saved=saved_nodes(f);nodes,relations,ids=am.projection(data,saved)
            graph=MapGraph(1,"fp",nodes=nodes,relations=relations,article_map=data)
            layout=cm.layout_article_graph(graph,saved,width=900,height=700,depth=8,relations=relations)
            self.assertEqual(len(layout.nodes),len(nodes))
            self.assertEqual(len(layout.edges),len(relations))
            self.assertEqual(len(layout.topic_links),len(nodes)-1)
            for edge in layout.edges:
                obstacles=[cm.node_box(n,0) for n in layout.nodes
                           if n.entry_id not in (edge.rel.src_entry_id,edge.rel.dst_entry_id)]
                self.assertTrue(cm.route_is_clear(edge.points,obstacles),edge.label)
                label_box=cm.label_box(edge.label_pos,edge.label,cm.metrics_for())
                self.assertFalse(any(cm.rects_intersect(label_box,cm.node_box(n,0)) for n in layout.nodes),edge.label)
            svg=map_export.svg_document(layout=layout,meta={"article_map":data})
            xml=ET.fromstring(svg);ns={"s":map_export.SVG_NAMESPACE}
            self.assertEqual(json.loads(xml.find("s:metadata",ns).text),data)
            labels={t.text for t in xml.findall(".//s:tspan",ns)}
            self.assertTrue({r.reason for r in relations}.issubset(labels))
            self.assertTrue(any(n.highlighted for n in layout.nodes))

    def test_article_window_controls_highlight_evidence_and_export_restore(self):
        from app.article_source import Article
        with support.temp_db() as db, support._FakeTkEnv():
            f=FIXTURES[0];topic,nodes,s,a,fp=self.prepare(db,f)
            db.put_article("fixture",Article(ok=True,text=f["text"],title=f["file"],status="ok",method="test"))
            s._worker_article(1,topic,fp,nodes,(a,f["text"]))
            win=cm.ConceptMapWindow(None,SimpleNamespace(db=db,config=s.config,map_service=s))
            win._draw(force=True)
            self.assertIsNotNone(win._full_article_graph())
            self.assertEqual(len(win._layout.edges),1)
            win._pick_article_relation(2)
            self.assertIn(win._graph.relations[2].evidence,win.evidence.cget("text"))
            graph=win._graph
            win._selected_entry_ids=lambda:[nodes[0].entry_id]
            with mock.patch.object(s,"generate",side_effect=AssertionError("高光不应调用模型")):
                win._on_subset_generate()
                win._change_article_depth("主干")
            self.assertIs(win._graph,graph)
            self.assertTrue(any(n.highlighted for n in win._layout.nodes))
            self.assertNotIn("孤立词",win._diag_summary())
            def export(**kwargs):
                self.assertEqual(len(kwargs["layout"].nodes),len(graph.nodes))
                self.assertEqual(len(kwargs["layout"].edges),len(graph.relations))
                raise RuntimeError("模拟导出失败")
            with mock.patch.object(map_export,"export_map",side_effect=export):
                win.export_map_files()
            self.assertFalse(win._article_export_all)
            self.assertEqual(win._article_depth(),1)
            self.assertEqual(len(win._layout.edges),1)
            win.close()

    def test_window_preserves_previous_graph_on_failed_regeneration(self):
        # 无真实窗口：验证产品窗口的结果接收行为，而非另写一套状态机。
        f=FIXTURES[0];d=am.validate_draft(f["draft"],f["text"],f["selected_words"])
        graph=MapGraph(1,"fp",article_map=d)
        win=cm.ConceptMapWindow.__new__(cm.ConceptMapWindow)
        win._topic_id=1;win._fingerprint="fp";win._pending_token=2;win._graph=graph
        win.alive=lambda:True;win._current_fingerprint=lambda:"fp"
        messages=[];win._set_feedback=messages.append
        win._update_evidence=lambda:None;win._sync_button=lambda:None;win._draw=lambda:None
        self.assertTrue(win.on_map_result((2,1,"fp","error",None,ApiError("schema","复核未通过"))))
        self.assertIs(win._graph,graph)
        self.assertIn("继续显示上一张合格图",messages[-1])


if __name__=="__main__":
    unittest.main()
