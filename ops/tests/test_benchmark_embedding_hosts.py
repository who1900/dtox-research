import unittest
from ops.corpus_quality.benchmark_embedding_hosts import compare_vectors,rank_documents


class EmbeddingHostBenchmarkTests(unittest.TestCase):
    def test_host_rank_loss_is_visible_and_invalid_vectors_rejected(self):
        topic=[1.0]+[0.0]*383;other=[0.0,1.0]+[0.0]*382
        records=[{'point_id':'target','paper_id':'relevant'}]+[{'point_id':'d'+str(i),'paper_id':'distractor'} for i in range(12)]
        baseline=[topic]+[other]*12;changed=[other]+[topic]*12
        self.assertTrue(compare_vectors(baseline,baseline)['bitwise_identical'])
        self.assertFalse(compare_vectors(baseline,changed)['bitwise_identical'])
        self.assertTrue(rank_documents(topic,baseline,records,'relevant')['hit_at_5'])
        self.assertFalse(rank_documents(topic,changed,records,'relevant')['hit_at_10'])
        fragment=rank_documents(topic,changed,records,'relevant','target')
        self.assertEqual(fragment['expected_point_rank'],13)
        self.assertFalse(fragment['expected_point_hit_at_10'])
        self.assertEqual(fragment['top_10_boundary_margin'],0)
        with self.assertRaises(ValueError):compare_vectors(baseline,[[float('nan')]*384]*13)


if __name__=='__main__':unittest.main()
