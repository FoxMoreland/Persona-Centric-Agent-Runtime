import unittest

from persona_runtime.conversation_bubbles import frames
from persona_runtime.private_reply_output import limits, assess


class ConversationBubbles(unittest.TestCase):
    def pieces(self, body):
        chunks = [body[p['start']:p['end']] for p in frames(body)]
        self.assertEqual(''.join(chunks), body)
        return chunks

    def test_short_and_unpunctuated(self):
        for size in (1, 74, 75, 76, 120, 213, 250):
            chunks = self.pieces('甲'*size)
            self.assertTrue(all(len(c) <= 75 for c in chunks))
            self.assertEqual(len(chunks)==1,size<=75)

    def test_breaks_and_unicode_clusters(self):
        for cluster in ('👩‍💻','👍🏽','🇨🇳','e\u0301','\r\n'):
            chunks = self.pieces('甲'*74+cluster+'乙'*75)
            self.assertTrue(any(cluster in chunk for chunk in chunks))
            self.assertLessEqual(max(map(len,chunks)),75)
        self.assertEqual(self.pieces('甲。\n乙。'), ['甲。\n','乙。'])

    def test_no_stranded_final_punctuation_or_whitespace(self):
        for suffix in ('。','\n',' '):
            chunks=self.pieces('甲'*75+suffix)
            self.assertTrue(all('甲' in part for part in chunks))
            self.assertLessEqual(max(map(len,chunks)),75)

    def test_oversized_literal_isolated(self):
        for token in ('https://example.org/'+'a'*90,'`'+'a'*90+'`'):
            body='甲。'+token+'。乙'
            chunks=self.pieces(body)
            self.assertEqual([c for c in chunks if len(c)>75], [token])
            report=assess(body,None,limits('main','short_answer',conversation=True),advisory=True)
            self.assertEqual(report['observed_counts']['protected_overlength'],[chunks.index(token)])

    def test_explanation_in_chat_and_explicit_long_requests(self):
        import sqlite3
        from persona_runtime.private_lookup import explicit_long
        from persona_runtime.archive import fingerprint,encoded
        db=sqlite3.connect(':memory:')
        try:
            db.execute('CREATE TABLE knowledge_control(key TEXT PRIMARY KEY,value TEXT)')
            message={'id':'701','text':'详细讲讲'}
            self.assertFalse(explicit_long(db,[message],conversation=True))
            self.assertTrue(explicit_long(db,[message]))
            self.assertTrue(explicit_long(db,[message|{'text':'请给我长文'}],conversation=True))
            db.execute('INSERT INTO knowledge_control VALUES (?,?)',('private.lookup.long.701',encoded({'hash':fingerprint(message['text'])})))
            self.assertTrue(explicit_long(db,[message],conversation=True))
        finally:
            db.close()

    def test_partwise_reply_retains_sources_then_splits_complete_text(self):
        import json
        from persona_runtime.partwise_reply import prepare,assemble
        facts=[{'id':'731','text':'731'},{'id':'732','text':'732'}]
        items=[{'id':i,'question':str(i),'result':'partial','facts':[f['id']]} for i,f in enumerate(facts,1)]
        packet={'phase':'main','action':'respond','mode':'short_answer','evidence':[],
                'output_constraints':limits('main','short_answer',conversation=True),
                'research':{'facts':facts,'status':'local_hit','lookup_outcome':'studied',
                            'retrieval':{'tool':'acquired','items':items}}}
        prepared=prepare(packet)
        self.assertNotIn('max_bubble_chars',prepared.packet['output_constraints'])
        text='甲'*95
        raw={'items':[{'id':2,'body':'702','sources':[2]},{'id':1,'body':text,'sources':[1]}]}
        reply=assemble(json.dumps(raw),prepared)
        self.assertEqual(reply.body,text+'\n'+'702')
        self.assertEqual(reply.bindings,((1,('731',)),(2,('732',))))
        self.assertLessEqual(max(map(len,self.pieces(reply.body))),75)
        raw['items'][0]['sources']=[1]
        with self.assertRaisesRegex(ValueError,'partwise_reply_invalid_sources'):
            assemble(json.dumps(raw),prepared)

    def test_other_contracts(self):
        for phase, mode in [('search_notice','short_answer'),('main','long_detail')]:
            self.assertEqual(limits(phase,mode,conversation=True,compact_notice=True),
                             limits(phase,mode,compact_notice=True))
