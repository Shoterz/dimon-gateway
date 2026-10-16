import unittest

from apps.dimon_live.transcript_arbitration import arbitrate_transcripts


class TranscriptArbitrationTests(unittest.TestCase):
    def test_material_bill_vs_stock_disagreement_requires_confirmation(self):
        decision = arbitrate_transcripts("pay my water bill", "PMI Worth Review.")

        self.assertEqual(decision.status, "CONFIRM_LIVE")
        self.assertEqual(decision.confirmed_text, None)

    def test_incomplete_live_caption_can_agree_with_more_complete_final(self):
        decision = arbitrate_transcripts("pay my water", "pay my water bill")

        self.assertEqual(decision.status, "USE_FINAL")
        self.assertEqual(decision.confirmed_text, "pay my water bill")

    def test_minor_transcription_difference_uses_authoritative_final(self):
        decision = arbitrate_transcripts(
            "buy one share of nvidia",
            "Buy 1 share of NVIDIA.",
        )

        self.assertEqual(decision.status, "USE_FINAL")

    def test_unrelated_conversation_is_not_reclassified_as_a_financial_error(self):
        decision = arbitrate_transcripts("how was your day", "How is your day?")

        self.assertEqual(decision.status, "USE_FINAL")

    def test_conversation_vs_financial_hallucination_requires_confirmation(self):
        decision = arbitrate_transcripts("how was your day", "buy NVIDIA stock")

        self.assertEqual(decision.status, "CONFIRM_LIVE")


if __name__ == "__main__":
    unittest.main()
