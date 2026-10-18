import unittest
from unittest import mock

from docker.errors import NotFound

from app.services.docker_service import DockerService


class DockerStopRemoveTests(unittest.TestCase):
    def test_stop_remove_continues_after_stop_error(self):
        svc = DockerService()
        container = mock.Mock()
        container.stop.side_effect = RuntimeError("stop timeout")
        client = mock.Mock()
        client.containers.get.return_value = container
        with mock.patch.object(DockerService, "client", new_callable=mock.PropertyMock, return_value=client):
            svc.stop_remove("grapes-test-demo")

        container.stop.assert_called_once()
        container.remove.assert_called_once_with(force=True)

    def test_stop_remove_missing_container(self):
        svc = DockerService()
        client = mock.Mock()
        client.containers.get.side_effect = NotFound("missing")
        with mock.patch.object(DockerService, "client", new_callable=mock.PropertyMock, return_value=client):
            svc.stop_remove("grapes-test-missing")

        client.containers.get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
