"""Regression test for a real pipeline failure found live on
job-f884adc96492 (after the vision-model-endpoint regression it hit
earlier was independently fixed and confirmed resolved).

_plan_scenes_in_bounded_batches plans a long deck in page batches and
merges them; each batch's prompt gives the model a prior_scene_catalog of
already-defined scene identities and explicitly instructs it to reuse the
exact scene_key for a canonical_scene_id it recognizes from an earlier
batch. The real local model correctly reused canonical_scene_id
("factory_worker_production_line") across two independently-run batches —
confirming it genuinely recognized the same concrete scene — but, with no
memory of the exact string it used before, re-derived the scene_key slug
from scratch and permuted its word order: "worker_factory_production" in
the earlier batch vs "factory_worker_production" in a later one. This
previously hard-failed the whole deck with SCENE.CANONICAL_SCENE_CONFLICT
and zero repair chance, unlike nearly every other repairable scene-
planning failure mode elsewhere in this file.

_scene_keys_are_word_order_permutation recognizes this one narrow,
provably-safe case (same token set, different order) and lets the merge
proceed by normalizing to the catalog's original scene_key, without ever
risking merging two genuinely different scenes — a real, different scene
would not share every one of its underscore-separated tokens with the
earlier one.
"""
import unittest

from wengao_ppt.scene_model import _scene_keys_are_word_order_permutation


class TestSceneKeyWordOrderPermutation(unittest.TestCase):

    def test_real_word_order_permutation_from_job_f884adc96492(self):
        self.assertTrue(_scene_keys_are_word_order_permutation(
            "factory_worker_production", "worker_factory_production"))

    def test_identical_keys_trivially_match(self):
        self.assertTrue(_scene_keys_are_word_order_permutation(
            "worker_factory_production", "worker_factory_production"))

    def test_genuinely_different_scene_is_not_a_permutation(self):
        # A real different scene: shares two tokens but not all three, or
        # has an extra/missing token entirely — must never be treated as
        # the same identity just because it overlaps partially.
        self.assertFalse(_scene_keys_are_word_order_permutation(
            "worker_factory_production", "worker_factory_inspection"))
        self.assertFalse(_scene_keys_are_word_order_permutation(
            "worker_factory_production", "worker_factory"))
        self.assertFalse(_scene_keys_are_word_order_permutation(
            "worker_factory_production", "worker_factory_production_line"))

    def test_empty_keys_never_match(self):
        self.assertFalse(_scene_keys_are_word_order_permutation("", "worker_factory"))
        self.assertFalse(_scene_keys_are_word_order_permutation("worker_factory", ""))
        self.assertFalse(_scene_keys_are_word_order_permutation("", ""))


if __name__ == "__main__":
    unittest.main()
