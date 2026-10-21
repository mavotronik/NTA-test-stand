import unittest

from app.services.image_inventory import (
    ImageUsageRef,
    compute_in_use_now,
    is_stand_image_tag,
    merge_stand_image_rows,
    normalize_image_id,
)


class ImageInventoryTests(unittest.TestCase):
    def test_is_stand_image_tag(self):
        self.assertTrue(is_stand_image_tag("grapes-nta-test:abc123456789"))
        self.assertTrue(is_stand_image_tag("grapes-nta:latest"))
        self.assertFalse(is_stand_image_tag("nginx:latest"))

    def test_normalize_image_id(self):
        self.assertEqual(
            normalize_image_id("abcd" * 16),
            "sha256:" + "abcd" * 16,
        )

    def test_merge_usage_instance_and_container(self):
        iid = "sha256:" + "a" * 64
        rows = merge_stand_image_rows(
            docker_images=[
                {
                    "id": iid,
                    "tags": ["grapes-nta-test:deadbeef0001"],
                    "size": 1024,
                    "created": 1_700_000_000,
                }
            ],
            instances=[
                {
                    "id": "inst1",
                    "name": "Demo",
                    "slug": "demo",
                    "state": "running",
                    "image_tag": "grapes-nta-test:deadbeef0001",
                    "rollback_image_tag": "",
                }
            ],
            containers=[
                {
                    "name": "grapes-test-demo",
                    "id": "c1",
                    "status": "running",
                    "image": "grapes-nta-test:deadbeef0001",
                    "image_id": iid,
                }
            ],
            builds=[
                {
                    "id": "b1",
                    "status": "ready",
                    "image_tag": "grapes-nta-test:deadbeef0001",
                    "image_id": iid,
                    "git_commit": "deadbeef0001",
                }
            ],
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row.in_use)
        self.assertTrue(row.in_use_now)
        self.assertFalse(row.deletable)
        self.assertEqual(len(row.usages), 2)

    def test_rollback_not_in_use_now(self):
        refs = [
            ImageUsageRef(
                kind="instance",
                label="x",
                detail="",
                href=None,
                role="rollback",
                status="stopped",
            ),
        ]
        self.assertFalse(compute_in_use_now(refs))

    def test_deletable_when_free(self):
        iid = "sha256:" + "b" * 64
        rows = merge_stand_image_rows(
            docker_images=[{"id": iid, "tags": ["grapes-nta-test:free00000001"], "size": 0, "created": None}],
            instances=[],
            containers=[],
            builds=[],
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].deletable)


if __name__ == "__main__":
    unittest.main()
