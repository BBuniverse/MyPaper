import datetime as dt
import unittest
from unittest.mock import patch

from scripts.collect_papers import Topic, arxiv_query_for_topic, collection_cutoff, extract_known_venue, merge_with_retained_papers, publication_status, trim_papers_for_storage


def paper(paper_id: str, level: str, published: str) -> dict:
    return {
        "id": paper_id,
        "title": paper_id,
        "published": published,
        "best_match": {
            "topic_id": "topic",
            "topic_name": "Topic",
            "score": {"high": 0.9, "medium": 0.5, "low": 0.2}[level],
            "level": level,
            "reason": "test",
        },
        "matches": [],
        "chinese_summary": {},
    }


class RetentionTest(unittest.TestCase):
    def test_arxiv_query_uses_more_than_eight_keywords_by_default(self) -> None:
        topic = Topic(
            id="topic",
            name="Topic",
            description="",
            keywords=[f"keyword {index}" for index in range(12)],
            arxiv_categories=["cs.CV"],
        )

        query = arxiv_query_for_topic(topic)

        self.assertIn('all:"keyword 11"', query)

    def test_arxiv_query_keyword_count_can_be_configured(self) -> None:
        topic = Topic(
            id="topic",
            name="Topic",
            description="",
            keywords=[f"keyword {index}" for index in range(12)],
            arxiv_categories=["cs.CV"],
        )

        with patch.dict("os.environ", {"ARXIV_QUERY_KEYWORDS": "3"}):
            query = arxiv_query_for_topic(topic)

        self.assertIn('all:"keyword 2"', query)
        self.assertNotIn('all:"keyword 3"', query)

    def test_arxiv_query_prefers_topic_keyword_count(self) -> None:
        topic = Topic(
            id="topic",
            name="Topic",
            description="",
            keywords=[f"keyword {index}" for index in range(12)],
            arxiv_categories=["cs.CV"],
            max_query_keywords=5,
        )

        with patch.dict("os.environ", {"ARXIV_QUERY_KEYWORDS": "3"}):
            query = arxiv_query_for_topic(topic)

        self.assertIn('all:"keyword 4"', query)
        self.assertNotIn('all:"keyword 5"', query)

    def test_extract_known_venue_from_comment(self) -> None:
        self.assertEqual(extract_known_venue("Accepted by CVPR 2026"), "CVPR 2026")
        self.assertEqual(extract_known_venue("To appear in IEEE/CVF Conference on Computer Vision and Pattern Recognition 2025"), "CVPR 2025")
        self.assertEqual(extract_known_venue("Accepted to NIPS '26"), "NeurIPS 2026")
        self.assertEqual(extract_known_venue("ICLR2025 poster"), "ICLR 2025")
        self.assertEqual(extract_known_venue("IEEE TIP 2024"), "TIP 2024")
        self.assertEqual(extract_known_venue("IEEE Transactions on Pattern Analysis and Machine Intelligence, 2024"), "TPAMI 2024")
        self.assertEqual(extract_known_venue("International Journal of Computer Vision (IJCV), 2023"), "IJCV 2023")
        self.assertEqual(extract_known_venue("This paper gives practical tips for restoration."), "")

    def test_publication_status_prefers_journal_ref(self) -> None:
        status = publication_status("Nature 620, 123-130 (2026)", "10.1234/example", "Accepted by Nature")

        self.assertEqual(status["status"], "published")
        self.assertEqual(status["venue"], "Nature 620, 123-130 (2026)")
        self.assertTrue(status["has_publication_info"])

    def test_publication_status_uses_doi_or_comment_when_needed(self) -> None:
        doi_status = publication_status("", "10.1234/example", "")
        venue_status = publication_status("", "", "CVPR 2026, Highlight paper")
        comment_status = publication_status("", "", "Accepted as an oral presentation.")
        unknown_status = publication_status("", "", "")

        self.assertEqual(doi_status["status"], "doi")
        self.assertEqual(venue_status["status"], "venue_note")
        self.assertEqual(venue_status["venue"], "CVPR 2026")
        self.assertEqual(comment_status["status"], "accepted_note")
        self.assertEqual(unknown_status["status"], "unknown")
        self.assertFalse(unknown_status["has_publication_info"])

    def test_merge_retains_previous_high_medium_and_recent_low(self) -> None:
        now = dt.datetime(2026, 5, 28, tzinfo=dt.timezone.utc)
        stale_low = paper("old-low", "low", "2026-03-01T00:00:00+00:00")
        stale_low["first_seen_at"] = "2026-03-02T00:00:00+00:00"
        existing = {
            "generated_at_iso": "2026-05-27T00:00:00+00:00",
            "papers": [
                paper("old-high", "high", "2026-05-26T00:00:00+00:00"),
                paper("old-medium", "medium", "2026-05-25T00:00:00+00:00"),
                paper("recent-low", "low", "2026-05-24T00:00:00+00:00"),
                stale_low,
            ],
        }

        merged, stats = merge_with_retained_papers(
            [paper("new-low", "low", "2026-05-28T00:00:00+00:00")],
            existing,
            now,
            recent_history_days=45,
        )

        self.assertEqual({item["id"] for item in merged}, {"new-low", "old-high", "old-medium", "recent-low"})
        self.assertEqual(stats["retained_paper_count"], 3)
        self.assertEqual(stats["retained_recent_low_count"], 1)
        self.assertEqual(stats["dropped_low_relevance_count"], 1)
        self.assertTrue(next(item for item in merged if item["id"] == "old-high")["retained_from_previous_run"])

    def test_collection_cutoff_uses_previous_run_for_incremental_mode(self) -> None:
        now = dt.datetime(2026, 5, 28, 22, tzinfo=dt.timezone.utc)
        cutoff, mode = collection_cutoff(
            {"generated_at_iso": "2026-05-27T22:00:00+00:00"},
            now,
            days=7,
            incremental_since_last_run=True,
        )

        self.assertEqual(mode, "incremental")
        self.assertEqual(cutoff, dt.datetime(2026, 5, 27, 22, tzinfo=dt.timezone.utc))

    def test_collection_cutoff_falls_back_to_lookback(self) -> None:
        now = dt.datetime(2026, 5, 28, 22, tzinfo=dt.timezone.utc)
        cutoff, mode = collection_cutoff({}, now, days=7, incremental_since_last_run=True)

        self.assertEqual(mode, "lookback")
        self.assertEqual(cutoff, dt.datetime(2026, 5, 21, 22, tzinfo=dt.timezone.utc))

    def test_storage_trim_removes_low_then_oldest(self) -> None:
        payload = {
            "generated_at_iso": "2026-05-28T00:00:00+00:00",
            "papers": [
                paper("newer-high", "high", "2026-05-28T00:00:00+00:00"),
                paper("older-high", "high", "2026-05-20T00:00:00+00:00"),
                paper("newer-low", "low", "2026-05-28T00:00:00+00:00"),
            ],
            "stats": {},
        }

        trimmed, stats = trim_papers_for_storage(payload, max_stored_papers=2, max_data_bytes=0)
        self.assertEqual({item["id"] for item in trimmed}, {"newer-high", "older-high"})
        self.assertEqual(stats["storage_trimmed_by_level"]["low"], 1)

        payload["papers"] = trimmed
        trimmed, stats = trim_papers_for_storage(payload, max_stored_papers=1, max_data_bytes=0)
        self.assertEqual([item["id"] for item in trimmed], ["newer-high"])
        self.assertEqual(stats["storage_trimmed_by_level"]["high"], 1)


if __name__ == "__main__":
    unittest.main()
