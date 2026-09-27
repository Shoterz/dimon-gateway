import tempfile
import unittest
from pathlib import Path

from apps.dimon_live.database import Database
from apps.dimon_live.name_resolution import phonetic_key, rank_phonetic_candidates


TOKEN_TARGETS = {
    "Alicia": {"beneficiary_alicia_powell_01"},
    "Powell": {"beneficiary_alicia_powell_01"},
    "John": {"beneficiary_john_smith_01", "beneficiary_john_tan_01"},
    "Smith": {"beneficiary_john_smith_01"},
    "Tan": {"beneficiary_john_tan_01"},
}

FULL_NAME_TARGETS = {
    "Alicia Powell": {"beneficiary_alicia_powell_01"},
    "John Smith": {"beneficiary_john_smith_01"},
    "John Tan": {"beneficiary_john_tan_01"},
}


def single_edit_variants(token):
    """Generate typo probes independently of the production matcher."""

    lowered = token.casefold()
    variants = set()
    for index in range(len(lowered)):
        deleted = lowered[:index] + lowered[index + 1 :]
        if len(deleted) >= 3:
            variants.add(deleted)
        variants.add(lowered[:index] + "z" + lowered[index + 1 :])
    for index in range(len(lowered) + 1):
        variants.add(lowered[:index] + "z" + lowered[index:])
    for index in range(len(lowered) - 1):
        transposed = (
            lowered[:index]
            + lowered[index + 1]
            + lowered[index]
            + lowered[index + 2 :]
        )
        variants.add(transposed)
    variants.discard(lowered)
    return tuple(sorted(variants))


class RecipientResolutionEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "demo.sqlite3")
        self.database.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_saved_name_tokens_are_candidates_not_exact_authorizations(self):
        for token, expected_ids in TOKEN_TARGETS.items():
            with self.subTest(token=token):
                result = self.database.resolve_beneficiary(token)
                self.assertEqual(result.kind, "candidates")
                self.assertEqual(
                    {match["beneficiary_id"] for match in result.matches},
                    expected_ids,
                )

    def test_generated_single_edit_typos_retrieve_only_the_intended_people(self):
        exercised = 0
        for token, expected_ids in TOKEN_TARGETS.items():
            for variant in single_edit_variants(token):
                with self.subTest(token=token, variant=variant):
                    result = self.database.resolve_beneficiary(variant)
                    self.assertEqual(result.kind, "close")
                    self.assertEqual(
                        {match["beneficiary_id"] for match in result.matches},
                        expected_ids,
                    )
                    exercised += 1

        for full_name, expected_ids in FULL_NAME_TARGETS.items():
            tokens = full_name.casefold().split()
            for token_index, token in enumerate(tokens):
                for variant in single_edit_variants(token):
                    changed = list(tokens)
                    changed[token_index] = variant
                    mention = " ".join(changed)
                    with self.subTest(full_name=full_name, mention=mention):
                        result = self.database.resolve_beneficiary(mention)
                        self.assertEqual(result.kind, "close")
                        self.assertEqual(
                            {
                                match["beneficiary_id"]
                                for match in result.matches
                            },
                            expected_ids,
                        )
                        exercised += 1

        self.assertGreaterEqual(exercised, 100)
        self.assertLessEqual(exercised, 200)

    def test_unrelated_names_do_not_produce_candidates(self):
        for mention in ("Michael", "Samantha", "bank manager", "recipient"):
            with self.subTest(mention=mention):
                result = self.database.resolve_beneficiary(mention)
                self.assertEqual(result.kind, "missing")
                self.assertEqual(result.matches, ())

    def test_human_written_holdouts_are_resolved_without_runtime_aliases(self):
        cases = (
            ("Alecia", {"beneficiary_alicia_powell_01"}),
            ("Alisha", {"beneficiary_alicia_powell_01"}),
            ("Jon Smith", {"beneficiary_john_smith_01"}),
            ("Jhon Tan", {"beneficiary_john_tan_01"}),
        )
        for mention, expected_ids in cases:
            with self.subTest(mention=mention):
                result = self.database.resolve_beneficiary(mention)
                self.assertEqual(result.kind, "close")
                self.assertEqual(
                    {match["beneficiary_id"] for match in result.matches},
                    expected_ids,
                )

    def test_phonetic_holdout_suggests_but_never_exactly_resolves(self):
        result = self.database.resolve_beneficiary("Alesha")

        self.assertEqual(result.kind, "phonetic")
        self.assertEqual(
            [row["display_name"] for row in result.matches],
            ["Alicia Powell"],
        )

    def test_phonetic_collision_returns_every_candidate(self):
        candidates = (
            {"beneficiary_id": "one", "display_name": "Robert", "normalized_alias": "Robert"},
            {"beneficiary_id": "two", "display_name": "Rupert", "normalized_alias": "Rupert"},
        )

        result = rank_phonetic_candidates("Rubert", candidates)

        self.assertEqual(phonetic_key("Robert"), phonetic_key("Rupert"))
        self.assertEqual([row["beneficiary_id"] for row in result], ["one", "two"])

    def test_empty_and_non_latin_phonetic_keys_do_not_match(self):
        self.assertEqual(phonetic_key(""), "")
        self.assertEqual(phonetic_key("张伟"), "")


if __name__ == "__main__":
    unittest.main()
