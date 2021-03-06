# Copyright 2017 the lostromos Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Module used to perform integration testing on lostromos, by attempting multiple create/update/delete commands via
kubectl.
"""

import os
import re
import time
import requests
import signal
import subprocess

from unittest import TestCase

_LOSTROMOS_EXE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lostromos")
_TEST_DATA_DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
_LOSTROMOS_CONFIGURATION_FILE = os.path.join(_TEST_DATA_DIRECTORY, "config.yaml")
_CUSTOM_RESOURCE_DEFINITION_FILE = os.path.join(_TEST_DATA_DIRECTORY, "crd.yml")
_THINGS_CUSTOM_RESOURCE_FILE = os.path.join(_TEST_DATA_DIRECTORY, "cr_things.yml")
_THINGS_FILTERED_CUSTOM_RESOURCE_FILE = os.path.join(_TEST_DATA_DIRECTORY, "cr_things_filter.yml")
_THINGS_FILTERED_UPDATE_CUSTOM_RESOURCE_FILE = os.path.join(_TEST_DATA_DIRECTORY, "cr_things_filter_update.yml")
_NEMO_CUSTOM_RESOURCE_FILE = os.path.join(_TEST_DATA_DIRECTORY, "cr_nemo.yml")
_NEMO_UPDATE_CUSTOM_RESOURCE_FILE = os.path.join(_TEST_DATA_DIRECTORY, "cr_nemo_update.yml")
_REMOTE_REPO_CUSTOM_RESOURCE_FILE = os.path.join(_TEST_DATA_DIRECTORY, "cr_remote_repo.yml")


class Kubectl(object):
    """
    Class used to interact with kubectl and return data.
    """

    def __run_command(self, command, filepath, raise_error):
        """
        Run a command via kubectl.  Will raise an error if the command fails.
        :param command: The command to run.
        :param filepath: The path to the file to use for the command.
        :param raise_error: Whether to raise the error should one occur. If False any error is swallowed.
        """
        try:
            subprocess.check_call([
                "kubectl",
                command,
                "-f",
                filepath
            ])
        except subprocess.CalledProcessError as error:
            if raise_error:
                raise error

    def apply(self, filepath):
        """
        Run an apply with the given filepath. Will raise an error if the command fails.
        :param filepath: The file to be used for the given apply.
        """
        self.__run_command("apply", filepath, True)

    def delete(self, filepath, raise_error=False):
        """
        Run a delete with the given filepath. Will raise an error if the command fails.
        :param filepath: The file to be used for the given delete.
        :param raise_error: Whether or not to smother the error.
        """
        self.__run_command("delete", filepath, raise_error)


class Helm(object):
    """
    Class used to interact with helm and return data
    """

    def delete(self, release_name):
        """
        Delete a named helm release
        """
        subprocess.call(
            [
                "helm",
                "delete",
                "--purge",
                release_name
            ]
        )

    def init(self):
        """
        Run a helm init to ensure there is a working tiller
        :return:
        """
        subprocess.check_call(
            [
                "helm",
                "init"
            ]
        )

    def status(self, release_name):
        """
        Return the output of helm status
        :param release_name: The name of the release to get
        :return: The output of command as bytes
        """
        try:
            output = subprocess.check_output(
                [
                    "helm",
                    "status",
                    release_name
                ]
            )
            return output
        except subprocess.CalledProcessError as error:
            return error.output


class HelmIntegrationTest(TestCase):
    """
    Class used to perform Lostromos integration testing with helm against a minikube environment. Uses kubectl to
    manipulate the kubernetes system and helm to interact with helm.
    """

    def setUp(self):
        """
        Ensure the custom resource definition exists, and set up Helm.
        """

        self.__kubectl = Kubectl()
        # Set up the tiller and expose it via a nodeport service
        self.__helm = Helm()
        self.__helm.init()
        self.__kubectl.apply(_TEST_DATA_DIRECTORY + "/tiller_nodeport_service.yml")
        self.__kubectl.apply(_CUSTOM_RESOURCE_DEFINITION_FILE)
        self.__kubectl.delete(_NEMO_CUSTOM_RESOURCE_FILE)
        self.__minikube_ip = subprocess.check_output(["minikube", "ip"]).strip().decode('utf-8')

    def runTest(self):
        """
        Run test using Lostromos with a helm controller.
        """
        print("Starting Lostromos with helm controller")
        self.__lostromos_process = subprocess.Popen(
            [
                _LOSTROMOS_EXE,
                "start",
                "--config",
                _TEST_DATA_DIRECTORY + "/helm/wait-config.yaml",
                "--helm-tiller",
                self.__minikube_ip + ":32664",
            ],
        )
        print("Started Lostromos with PID: {}".format(self.__lostromos_process.pid))
        # Sleep for a bit until the tiller is available
        time.sleep(15)
        self.__kubectl.apply(_NEMO_CUSTOM_RESOURCE_FILE)
        self.__wait_for_helm_to_fail(15)

    def tearDown(self):
        """
        Kill the lostromos process if it was created.
        """
        self.__kubectl.delete(_CUSTOM_RESOURCE_DEFINITION_FILE)
        self.__helm.delete("lostromos-nemo")
        if self.__lostromos_process:
            self.__lostromos_process.send_signal(signal.SIGINT)

    def __wait_for_helm_to_fail(self, timeout):
        """
        Wait for the helm timeout to be reached and verify the release is marked as failed
        :return:
        """
        seconds_to_sleep = 1
        # Check that the release wasn't immediately marked as failed or successful

        output = self.__helm.status("lostromos-nemo")
        self.assertNotIn(
            "STATUS: FAILED",
            output.decode("utf-8"),
            "Helm release is FAILED but did not wait for the timeout"
        )
        self.assertNotIn("STATUS: DEPLOYED", output.decode("utf-8"), "Helm release is DEPLOYED but should be FAILED")

        while timeout > 0:
            try:
                output = self.__helm.status("lostromos-nemo")
                self.assertIn("STATUS: FAILED", output.decode("utf-8"))
                return
            except AssertionError:
                time.sleep(1)
                timeout -= seconds_to_sleep
        raise AssertionError("Helm release not marked as failed")


class TemplateIntegrationTestWithFiltering(TestCase):
    """
    Class used to perform Lostromos integration testing against a minikube environment. Uses kubectl to manipulate the
    kubernetes system.
    """

    def setUp(self):
        """
        Ensure the custom resource definition exists, and set up the status and metrics url.
        """
        self.__kubectl = Kubectl()

        # Ensure the CRD is there and there are no characters, for a clean starting point
        self.__kubectl.apply(_CUSTOM_RESOURCE_DEFINITION_FILE)
        self.__kubectl.delete(_THINGS_CUSTOM_RESOURCE_FILE)
        self.__kubectl.delete(_THINGS_FILTERED_CUSTOM_RESOURCE_FILE)
        self.__kubectl.delete(_THINGS_FILTERED_UPDATE_CUSTOM_RESOURCE_FILE)
        self.__kubectl.delete(_NEMO_CUSTOM_RESOURCE_FILE)
        self.__kubectl.delete(_NEMO_UPDATE_CUSTOM_RESOURCE_FILE)
        self.__lostromos_process = None
        self.__status_url = "http://localhost:8080/status"
        self.__metrics_url = "http://localhost:8080/metrics"

    def runTest(self):
        """
        Ensure Lostromos is functioning as expected. Does the following steps.

        1. Ensures we see thing1 and thing2 as existing on the system.
        2. Add the nemo custom resource and see that Lostromos sees it as created.
        3. Modify the nemo custom resource and see that Lostromos sees it as updated.
        4. Delete both sets of custom resources and see that Lostromos picks them up as deleted.
        """
        self.__lostromos_process = subprocess.Popen(
            [
                _LOSTROMOS_EXE,
                "start",
                "--nop",
                "--config",
                _LOSTROMOS_CONFIGURATION_FILE,
            ]
        )
        print("Started Lostromos with PID: {}".format(self.__lostromos_process.pid))

        self.__wait_for_lostromos_start()

        current_ts = time.time()
        self.__kubectl.apply(_THINGS_CUSTOM_RESOURCE_FILE)
        self.__check_metrics(2, 2, 2, 0, 0)
        self.__check_timestamp("releases_last_create_timestamp_utc_seconds", current_ts, 2)

        current_ts = time.time()
        self.__kubectl.apply(_NEMO_CUSTOM_RESOURCE_FILE)
        self.__check_metrics(3, 3, 3, 0, 0)
        self.__check_timestamp("releases_last_create_timestamp_utc_seconds", current_ts, 3)

        current_ts = time.time()
        self.__kubectl.apply(_NEMO_UPDATE_CUSTOM_RESOURCE_FILE)
        self.__check_metrics(4, 3, 3, 0, 1)
        self.__check_timestamp("releases_last_update_timestamp_utc_seconds", current_ts, 4)

        current_ts = time.time()
        self.__kubectl.delete(_THINGS_CUSTOM_RESOURCE_FILE, True)
        self.__check_metrics(6, 1, 3, 2, 1)
        self.__check_timestamp("releases_last_delete_timestamp_utc_seconds", current_ts, 6)

        current_ts = time.time()
        self.__kubectl.delete(_NEMO_CUSTOM_RESOURCE_FILE, True)
        self.__check_metrics(7, 0, 3, 3, 1)
        self.__check_timestamp("releases_last_delete_timestamp_utc_seconds", current_ts, 7)

        # initialize helm remote repo and set HELM_HOME env
        # helm repo add incubator https://kubernetes-charts-incubator.storage.googleapis.com/
        subprocess.call(
            [
                "helm",
                "repo",
                "add",
                "incubator",
                "https://kubernetes-charts-incubator.storage.googleapis.com/"
            ]
        )

        # export HELM_HOME=$(helm home)
        helm_home = subprocess.check_output(
            [
                "helm",
                "home"
            ]
        )
        os.environ['HELM_HOME'] = str(helm_home).strip()

        current_ts = time.time()
        self.__kubectl.apply(_REMOTE_REPO_CUSTOM_RESOURCE_FILE)
        self.__check_metrics(8, 1, 4, 3, 1)
        self.__check_timestamp("releases_last_create_timestamp_utc_seconds", current_ts, 8)

        current_ts = time.time()
        self.__kubectl.delete(_REMOTE_REPO_CUSTOM_RESOURCE_FILE, True)
        self.__check_metrics(9, 0, 4, 4, 1)
        self.__check_timestamp("releases_last_delete_timestamp_utc_seconds", current_ts, 9)

        self.__lostromos_process.kill()
        self.__lostromos_process = subprocess.Popen(
            [
                _LOSTROMOS_EXE,
                "start",
                "--nop",
                "--config",
                _LOSTROMOS_CONFIGURATION_FILE,
                "--crd-filter",
                "io.nicolerenee.lostromosApplied",
            ]
        )
        print("Started Lostromos with PID: {}".format(self.__lostromos_process.pid))

        self.__wait_for_lostromos_start()
        self.__kubectl.apply(_THINGS_FILTERED_CUSTOM_RESOURCE_FILE)
        self.__check_metrics(2, 2, 2, 0, 0)
        self.__kubectl.apply(_THINGS_FILTERED_UPDATE_CUSTOM_RESOURCE_FILE)
        self.__check_metrics(5, 2, 3, 1, 1)
        self.__kubectl.delete(_THINGS_FILTERED_UPDATE_CUSTOM_RESOURCE_FILE)
        self.__check_metrics(7, 0, 3, 3, 1)

    def tearDown(self):
        """
        Kill the lostromos process if it was created.
        """
        self.__kubectl.delete(_CUSTOM_RESOURCE_DEFINITION_FILE)
        if self.__lostromos_process:
            self.__lostromos_process.send_signal(signal.SIGINT)

    def __check_metrics(self, events, managed, created, deleted, updated):
        """
        Check the metrics output to ensure that what we are expecting has occurred. Will wait up to 10 seconds looking
        for the expected amount of events to have occurred. If the events haven't occurred, then an assertionError will
        be raised. If the events occurred, we will check the stats for the managed/created/deleted/updated resources.
        :param events: Number of events we are expecting to have happened.
        :param managed: Number of resources we expect Lostromos to be managing.
        :param created: Number of resources we expect Lostromos to have created.
        :param deleted: Number of resources we expect Lostromos to have deleted.
        :param updated: Number of resources we expect Lostromos to have updated.
        """
        metrics = []
        attempts = 10
        while attempts > 0:
            metrics_response = requests.get(self.__metrics_url)
            metrics_response.raise_for_status()
            metrics = metrics_response.text.split("\n")
            if "releases_events_total {}".format(events) not in metrics:
                time.sleep(1)
                attempts -= 1
            else:
                self.assertIn("releases_total {}".format(managed), metrics)
                self.assertIn("releases_create_total {}".format(created), metrics)
                self.assertIn("releases_delete_total {}".format(deleted), metrics)
                self.assertIn("releases_update_total {}".format(updated), metrics)
                return

        raise AssertionError("Failed to see the expected number of events. {}".format(metrics))

    def __check_timestamp(self, metric, timestamp, num_events):
        """
        Assert timestamp for metric is greater than timestamp passed in
        :param metric: name of metric to check
        :param timestamp: timestamp to compare metric
        :param num_events: expected number of events
        """
        attempts = 10
        while attempts > 0:
            metrics_response = requests.get(self.__metrics_url)
            metrics_response.raise_for_status()
            metrics = metrics_response.text.split("\n")
            if "releases_events_total {}".format(num_events) not in metrics:
                time.sleep(1)
                attempts -= 1
            else:
                for line in metrics:
                    if re.match(metric, line):
                        metric_ts = line.split(" ")[1]
                        self.assertTrue(float(metric_ts) > timestamp)
                        return
                raise AssertionError("Failed to find metric {}".format(metric))
        raise AssertionError("Failed to see the expected number of events. {}".format(metrics))

    def __wait_for_lostromos_start(self):
        """
        Wait for Lostromos to start up, then return.
        """
        # 15 seconds is probably more than we need, but the main use of these tests will be to run in TravisCI, and
        # since we don't control that infrastructure it makes sense to inflate the value a bit. An extra 10 seconds
        # should cause no harm, but help out in cases where the Travis servers are overwhelmed.
        seconds_to_wait = 15
        seconds_to_sleep = 1
        while seconds_to_wait > 0:
            try:
                status_response = requests.get(self.__status_url)
                status_response.raise_for_status()
                self.assertTrue(status_response.json()["success"])
            except requests.exceptions.ConnectionError:
                time.sleep(seconds_to_sleep)
                seconds_to_wait -= seconds_to_sleep
            return
        raise AssertionError("Failed to start Lostromos.")
