# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""Functional tests for :module:`flocker.node.gear`."""

import os
import json
import subprocess
from unittest import skipIf

from twisted.python.filepath import FilePath
from twisted.internet.defer import succeed
from twisted.internet.error import ConnectionRefusedError
from twisted.internet.utils import getProcessOutput
from twisted.web.client import ResponseNeverReceived

from treq import request, content

from ...testtools import (
    loop_until, find_free_port, DockerImageBuilder, assertContainsAll)

from ..test.test_gear import random_name
from ..gear import PortMap, GearEnvironment
from ..testtools import wait_for_unit_state

_if_root = skipIf(os.getuid() != 0, "Must run as root.")


class DockerClientTestsMixin(object):
    """
    Implementation-specific tests mixin for ``DockerClient`` and similar
    classes (in particular, ``DockerClient``).
    """
    # Override with Exception subclass used by the client
    clientException = None

    def make_client(self):
        """
        Create a client.

        :return: A ``IDockerClient`` provider.
        """
        raise NotImplementedError("Implement in subclasses")

    def start_container(self, unit_name,
                        image_name=u"openshift/busybox-http-app",
                        ports=None, links=None, expected_states=(u'active',),
                        environment=None):
        """
        Start a unit and wait until it reaches the `active` state or the
        supplied `expected_state`.

        :param unicode unit_name: See ``IDockerClient.add``.
        :param unicode image_name: See ``IDockerClient.add``.
        :param list ports: See ``IDockerClient.add``.
        :param list links: See ``IDockerClient.add``.
        :param Unit expected_states: A list of activation states to wait for.

        :return: ``Deferred`` that fires with the ``DockerClient`` when
            the unit reaches the expected state.
        """
        client = self.make_client()
        d = client.add(
            unit_name=unit_name,
            image_name=image_name,
            ports=ports,
            links=links,
            environment=environment,
        )
        self.addCleanup(client.remove, unit_name)

        d.addCallback(lambda _: wait_for_unit_state(client, unit_name,
                                                    expected_states))
        d.addCallback(lambda _: client)

        return d

    def test_add_starts_container(self):
        """``DockerClient.add`` starts the container."""
        name = random_name()
        return self.start_container(name)

    @_if_root
    def test_correct_image_used(self):
        """
        ``DockerClient.add`` creates a container with the specified image.
        """
        name = random_name()
        d = self.start_container(name)

        def started(_):
            data = subprocess.check_output(
                [b"docker", b"inspect", name.encode("ascii")])
            self.assertEqual(json.loads(data)[0][u"Config"][u"Image"],
                             u"openshift/busybox-http-app")
        d.addCallback(started)
        return d

    def test_add_error(self):
        """``DockerClient.add`` returns ``Deferred`` that errbacks with
        ``GearError`` if response code is not a success response code.
        """
        client = self.make_client()
        # add() calls exists(), and we don't want exists() to be the one
        # failing since that's not the code path we're testing, so bypass
        # it:
        client.exists = lambda _: succeed(False)
        # Illegal container name should make gear complain when we try to
        # install the container:
        d = client.add(u"!!!###!!!", u"busybox")
        return self.assertFailure(d, self.clientException)

    def test_dead_is_listed(self):
        """
        ``DockerClient.list()`` includes dead units.

        We use a `busybox` image here, because it will exit immediately and
        reach an `inactive` substate of `dead`.

        There are no assertions in this test, because it will fail with a
        timeout if the unit with that expected state is never listed or if that
        unit never reaches that state.
        """
        name = random_name()
        d = self.start_container(unit_name=name, image_name="busybox",
                                 expected_states=(u'inactive',))
        return d

    def request_until_response(self, port):
        """
        Resend a test HTTP request until a response is received.

        The container may have started, but the webserver inside may take a
        little while to start serving requests.

        :param int port: The localhost port to which an HTTP request will be
            sent.

        :return: A ``Deferred`` which fires with the result of the first
            successful HTTP request.
        """
        def send_request():
            """
            Send an HTTP request in a loop until the request is answered.
            """
            response = request(
                b"GET", b"http://127.0.0.1:%d" % (port,),
                persistent=False)

            def check_error(failure):
                """
                Catch ConnectionRefused errors and response timeouts and return
                False so that loop_until repeats the request.

                Other error conditions will be passed down the errback chain.
                """
                failure.trap(ConnectionRefusedError, ResponseNeverReceived)
                return False
            response.addErrback(check_error)
            return response

        return loop_until(send_request)

    def test_add_with_port(self):
        """
        DockerClient.add accepts a ports argument which is passed to gear to
        expose those ports on the unit.

        Assert that the busybox-http-app returns the expected "Hello world!"
        response.

        XXX: We should use a stable internal container instead. See
        https://github.com/hybridlogic/flocker/issues/120

        XXX: The busybox-http-app returns headers in the body of its response,
        hence this over complicated custom assertion. See
        https://github.com/openshift/geard/issues/213
        """
        expected_response = b'Hello world!\n'
        external_port = find_free_port()[1]
        name = random_name()
        d = self.start_container(
            name, ports=[PortMap(internal_port=8080,
                                 external_port=external_port)])

        d.addCallback(
            lambda ignored: self.request_until_response(external_port))

        def started(response):
            d = content(response)
            d.addCallback(lambda body: self.assertIn(expected_response, body))
            return d
        d.addCallback(started)
        return d

    def build_slow_shutdown_image(self):
        """
        Create a Docker image that takes a while to shut down.

        This should really use Python instead of shell:
        https://github.com/ClusterHQ/flocker/issues/719

        :return: The name of created Docker image.
        """
        path = FilePath(self.mktemp())
        path.makedirs()
        path.child(b"Dockerfile.in").setContent("""\
FROM busybox
CMD sh -c "trap \"\" 2; sleep 3"
""")
        image = DockerImageBuilder(test=self, source_dir=path)
        return image.build()

    @_if_root
    def test_add_with_environment(self):
        """
        ``DockerClient.add`` accepts an environment object whose ID and
        variables are used when starting a docker image.
        """
        docker_dir = FilePath(self.mktemp())
        docker_dir.makedirs()
        docker_dir.child(b"Dockerfile").setContent(
            b'FROM busybox\n'
            b'CMD ["/bin/sh",  "-c", "while true; do env && sleep 1; done"]'
        )
        image = DockerImageBuilder(test=self, source_dir=docker_dir)
        image_name = image.build()
        unit_name = random_name()
        expected_environment_id = random_name()
        expected_variables = frozenset({
            'key1': 'value1',
            'key2': 'value2',
        }.items())
        d = self.start_container(
            unit_name=unit_name,
            image_name=image_name,
            environment=GearEnvironment(
                id=expected_environment_id, variables=expected_variables),
        )
        d.addCallback(
            lambda ignored: getProcessOutput(b'docker', [b'logs', unit_name],
                                             env=os.environ,
                                             # Capturing stderr makes
                                             # debugging easier:
                                             errortoo=True)
        )
        d.addCallback(
            assertContainsAll,
            test_case=self,
            needles=['{}={}\n'.format(k, v) for k, v in expected_variables],
        )
        return d
