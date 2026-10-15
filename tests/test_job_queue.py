import unittest

from app.services.job_queue import pick_next_queued_job


class JobQueuePickTests(unittest.TestCase):
    def test_lifecycle_before_build_at_limit(self):
        queued = [
            {"id": "1", "job_type": "build", "created_at": "a"},
            {"id": "2", "job_type": "instance_stop", "created_at": "b"},
        ]
        picked = pick_next_queued_job(
            queued,
            building=1,
            deploying=0,
            build_max=1,
            deploy_max=1,
        )
        self.assertEqual(picked["job_type"], "instance_stop")

    def test_build_respects_parallel_limit(self):
        queued = [{"id": "1", "job_type": "build", "created_at": "a"}]
        self.assertIsNone(
            pick_next_queued_job(
                queued,
                building=1,
                deploying=0,
                build_max=1,
                deploy_max=1,
            )
        )

    def test_build_run_needs_both_slots(self):
        queued = [{"id": "1", "job_type": "build_run", "created_at": "a"}]
        self.assertIsNone(
            pick_next_queued_job(
                queued,
                building=0,
                deploying=1,
                build_max=2,
                deploy_max=1,
            )
        )
        picked = pick_next_queued_job(
            queued,
            building=0,
            deploying=0,
            build_max=1,
            deploy_max=1,
        )
        self.assertEqual(picked["job_type"], "build_run")


if __name__ == "__main__":
    unittest.main()
