# Copyright 2022 IBM, Red Hat
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
The cluster sub-module contains the definition of the Cluster object, which represents
the resources requested by the user. It also contains functions for checking the
cluster setup queue, a list of all existing clusters, and the user's working namespace.
"""

from time import sleep
from typing import List, Optional, Tuple, Dict

import openshift as oc
from kubernetes import config
from ray.job_submission import JobSubmissionClient
import urllib3

from .auth import config_check, api_config_handler
from ..utils import pretty_print
from ..utils.generate_yaml import (
    generate_appwrapper,
)
from ..utils.kube_api_helpers import _kube_api_error_handling
from ..utils.openshift_oauth import (
    create_openshift_oauth_objects,
    delete_openshift_oauth_objects,
)
from .config import ClusterConfiguration
from .model import (
    AppWrapper,
    AppWrapperStatus,
    CodeFlareClusterStatus,
    RayCluster,
    RayClusterStatus,
)
from kubernetes import client, config
import yaml
import os
import requests

from kubernetes import config


class Cluster:
    """
    An object for requesting, bringing up, and taking down resources.
    Can also be used for seeing the resource cluster status and details.

    Note that currently, the underlying implementation is a Ray cluster.
    """

    torchx_scheduler = "ray"

    def __init__(self, config: ClusterConfiguration):
        """
        Create the resource cluster object by passing in a ClusterConfiguration
        (defined in the config sub-module). An AppWrapper will then be generated
        based off of the configured resources to represent the desired cluster
        request.
        """
        self.config = config
        self.app_wrapper_yaml = self.create_app_wrapper()
        self._job_submission_client = None
        self.app_wrapper_name = self.app_wrapper_yaml.replace(".yaml", "").split("/")[
            -1
        ]

    @property
    def _client_headers(self):
        k8_client = api_config_handler() or client.ApiClient()
        return {
            "Authorization": k8_client.configuration.get_api_key_with_prefix(
                "authorization"
            )
        }

    @property
    def _client_verify_tls(self):
        return not self.config.openshift_oauth

    @property
    def job_client(self):
        if self._job_submission_client:
            return self._job_submission_client
        if self.config.openshift_oauth:
            print(
                api_config_handler().configuration.get_api_key_with_prefix(
                    "authorization"
                )
            )
            self._job_submission_client = JobSubmissionClient(
                self.cluster_dashboard_uri(),
                headers=self._client_headers,
                verify=self._client_verify_tls,
            )
        else:
            self._job_submission_client = JobSubmissionClient(
                self.cluster_dashboard_uri()
            )
        return self._job_submission_client

    def evaluate_dispatch_priority(self):
        priority_class = self.config.dispatch_priority

        try:
            config_check()
            api_instance = client.CustomObjectsApi(api_config_handler())
            priority_classes = api_instance.list_cluster_custom_object(
                group="scheduling.k8s.io",
                version="v1",
                plural="priorityclasses",
            )
        except Exception as e:  # pragma: no cover
            return _kube_api_error_handling(e)

        for pc in priority_classes["items"]:
            if pc["metadata"]["name"] == priority_class:
                return pc["value"]
        print(f"Priority class {priority_class} is not available in the cluster")
        return None

    def validate_image_config(self):
        """
        Validates that the image configuration is not empty.

        :param image: The image string to validate
        :raises ValueError: If the image is not specified
        """
        if self.config.image == "" or self.config.image == None:
            raise ValueError("Image must be specified in the ClusterConfiguration")

    def create_app_wrapper(self):
        """
        Called upon cluster object creation, creates an AppWrapper yaml based on
        the specifications of the ClusterConfiguration.
        """

        if self.config.namespace is None:
            self.config.namespace = get_current_namespace()
            if self.config.namespace is None:
                print("Please specify with namespace=<your_current_namespace>")
            elif type(self.config.namespace) is not str:
                raise TypeError(
                    f"Namespace {self.config.namespace} is of type {type(self.config.namespace)}. Check your Kubernetes Authentication."
                )

        # Validate image configuration
        self.validate_image_config()

        # Before attempting to create the cluster AW, let's evaluate the ClusterConfig
        if self.config.dispatch_priority:
            if not self.config.mcad:
                raise ValueError(
                    "Invalid Cluster Configuration, cannot have dispatch priority without MCAD"
                )
            priority_val = self.evaluate_dispatch_priority()
            if priority_val == None:
                raise ValueError(
                    "Invalid Cluster Configuration, AppWrapper not generated"
                )
        else:
            priority_val = None

        name = self.config.name
        namespace = self.config.namespace
        head_cpus = self.config.head_cpus
        head_memory = self.config.head_memory
        head_gpus = self.config.head_gpus
        min_cpu = self.config.min_cpus
        max_cpu = self.config.max_cpus
        min_memory = self.config.min_memory
        max_memory = self.config.max_memory
        gpu = self.config.num_gpus
        workers = self.config.num_workers
        template = self.config.template
        image = self.config.image
        instascale = self.config.instascale
        mcad = self.config.mcad
        instance_types = self.config.machine_types
        env = self.config.envs
        local_interactive = self.config.local_interactive
        image_pull_secrets = self.config.image_pull_secrets
        dispatch_priority = self.config.dispatch_priority
        ingress_domain = self.config.ingress_domain
        ingress_options = self.config.ingress_options
        return generate_appwrapper(
            name=name,
            namespace=namespace,
            head_cpus=head_cpus,
            head_memory=head_memory,
            head_gpus=head_gpus,
            min_cpu=min_cpu,
            max_cpu=max_cpu,
            min_memory=min_memory,
            max_memory=max_memory,
            gpu=gpu,
            workers=workers,
            template=template,
            image=image,
            instascale=instascale,
            mcad=mcad,
            instance_types=instance_types,
            env=env,
            local_interactive=local_interactive,
            image_pull_secrets=image_pull_secrets,
            dispatch_priority=dispatch_priority,
            priority_val=priority_val,
            openshift_oauth=self.config.openshift_oauth,
            ingress_domain=ingress_domain,
            ingress_options=ingress_options,
        )

    # creates a new cluster with the provided or default spec
    def up(self):
        """
        Applies the AppWrapper yaml, pushing the resource request onto
        the MCAD queue.
        """
        namespace = self.config.namespace
        if self.config.openshift_oauth:
            create_openshift_oauth_objects(
                cluster_name=self.config.name, namespace=namespace
            )

        try:
            config_check()
            api_instance = client.CustomObjectsApi(api_config_handler())
            if self.config.mcad:
                with open(self.app_wrapper_yaml) as f:
                    aw = yaml.load(f, Loader=yaml.FullLoader)
                api_instance.create_namespaced_custom_object(
                    group="workload.codeflare.dev",
                    version="v1beta1",
                    namespace=namespace,
                    plural="appwrappers",
                    body=aw,
                )
            else:
                self._component_resources_up(namespace, api_instance)
        except Exception as e:  # pragma: no cover
            return _kube_api_error_handling(e)

    def down(self):
        """
        Deletes the AppWrapper yaml, scaling-down and deleting all resources
        associated with the cluster.
        """
        namespace = self.config.namespace
        try:
            config_check()
            api_instance = client.CustomObjectsApi(api_config_handler())
            if self.config.mcad:
                api_instance.delete_namespaced_custom_object(
                    group="workload.codeflare.dev",
                    version="v1beta1",
                    namespace=namespace,
                    plural="appwrappers",
                    name=self.app_wrapper_name,
                )
            else:
                self._component_resources_down(namespace, api_instance)
        except Exception as e:  # pragma: no cover
            return _kube_api_error_handling(e)

        if self.config.openshift_oauth:
            delete_openshift_oauth_objects(
                cluster_name=self.config.name, namespace=namespace
            )

    def status(
        self, print_to_console: bool = True
    ) -> Tuple[CodeFlareClusterStatus, bool]:
        """
        Returns the requested cluster's status, as well as whether or not
        it is ready for use.
        """
        ready = False
        status = CodeFlareClusterStatus.UNKNOWN
        if self.config.mcad:
            # check the app wrapper status
            appwrapper = _app_wrapper_status(self.config.name, self.config.namespace)
            if appwrapper:
                if appwrapper.status in [
                    AppWrapperStatus.RUNNING,
                    AppWrapperStatus.COMPLETED,
                    AppWrapperStatus.RUNNING_HOLD_COMPLETION,
                ]:
                    ready = False
                    status = CodeFlareClusterStatus.STARTING
                elif appwrapper.status in [
                    AppWrapperStatus.FAILED,
                    AppWrapperStatus.DELETED,
                ]:
                    ready = False
                    status = CodeFlareClusterStatus.FAILED  # should deleted be separate
                    return status, ready  # exit early, no need to check ray status
                elif appwrapper.status in [
                    AppWrapperStatus.PENDING,
                    AppWrapperStatus.QUEUEING,
                ]:
                    ready = False
                    if appwrapper.status == AppWrapperStatus.PENDING:
                        status = CodeFlareClusterStatus.QUEUED
                    else:
                        status = CodeFlareClusterStatus.QUEUEING
                    if print_to_console:
                        pretty_print.print_app_wrappers_status([appwrapper])
                    return (
                        status,
                        ready,
                    )  # no need to check the ray status since still in queue

        # check the ray cluster status
        cluster = _ray_cluster_status(self.config.name, self.config.namespace)
        if cluster:
            if cluster.status == RayClusterStatus.UNKNOWN:
                ready = False
                status = CodeFlareClusterStatus.STARTING
            if cluster.status == RayClusterStatus.READY:
                ready = True
                status = CodeFlareClusterStatus.READY
            elif cluster.status in [
                RayClusterStatus.UNHEALTHY,
                RayClusterStatus.FAILED,
            ]:
                ready = False
                status = CodeFlareClusterStatus.FAILED

            if print_to_console:
                # overriding the number of gpus with requested
                cluster.worker_gpu = self.config.num_gpus
                pretty_print.print_cluster_status(cluster)
        elif print_to_console:
            if status == CodeFlareClusterStatus.UNKNOWN:
                pretty_print.print_no_resources_found()
            else:
                pretty_print.print_app_wrappers_status([appwrapper], starting=True)

        return status, ready

    def is_dashboard_ready(self) -> bool:
        try:
            response = requests.get(
                self.cluster_dashboard_uri(),
                headers=self._client_headers,
                timeout=5,
                verify=self._client_verify_tls,
            )
        except requests.exceptions.SSLError:  # pragma no cover
            # SSL exception occurs when oauth ingress has been created but cluster is not up
            return False
        if response.status_code == 200:
            return True
        else:
            return False

    def wait_ready(self, timeout: Optional[int] = None, dashboard_check: bool = True):
        """
        Waits for requested cluster to be ready, up to an optional timeout (s).
        Checks every five seconds.
        """
        print("Waiting for requested resources to be set up...")
        ready = False
        dashboard_ready = False
        status = None
        time = 0
        while not ready:
            status, ready = self.status(print_to_console=False)
            if status == CodeFlareClusterStatus.UNKNOWN:
                print(
                    "WARNING: Current cluster status is unknown, have you run cluster.up yet?"
                )
            if not ready:
                if timeout and time >= timeout:
                    raise TimeoutError(
                        f"wait() timed out after waiting {timeout}s for cluster to be ready"
                    )
                sleep(5)
                time += 5
        print("Requested cluster is up and running!")

        while dashboard_check and not dashboard_ready:
            dashboard_ready = self.is_dashboard_ready()
            if not dashboard_ready:
                if timeout and time >= timeout:
                    raise TimeoutError(
                        f"wait() timed out after waiting {timeout}s for dashboard to be ready"
                    )
                sleep(5)
                time += 5
        if dashboard_ready:
            print("Dashboard is ready!")

    def details(self, print_to_console: bool = True) -> RayCluster:
        cluster = _copy_to_ray(self)
        if print_to_console:
            pretty_print.print_clusters([cluster])
        return cluster

    def cluster_uri(self) -> str:
        """
        Returns a string containing the cluster's URI.
        """
        return f"ray://{self.config.name}-head-svc.{self.config.namespace}.svc:10001"

    def cluster_dashboard_uri(self) -> str:
        """
        Returns a string containing the cluster's dashboard URI.
        """
        try:
            config_check()
            api_instance = client.NetworkingV1Api(api_config_handler())
            ingresses = api_instance.list_namespaced_ingress(self.config.namespace)
        except Exception as e:  # pragma no cover
            return _kube_api_error_handling(e)

        for ingress in ingresses.items:
            annotations = ingress.metadata.annotations
            protocol = "http"
            if (
                ingress.metadata.name == f"ray-dashboard-{self.config.name}"
                or ingress.metadata.name.startswith(f"{self.config.name}-ingress")
            ):
                if annotations == None:
                    protocol = "http"
                elif "route.openshift.io/termination" in annotations:
                    protocol = "https"
            return f"{protocol}://{ingress.spec.rules[0].host}"
        return "Dashboard ingress not available yet, have you run cluster.up()?"

    def list_jobs(self) -> List:
        """
        This method accesses the head ray node in your cluster and lists the running jobs.
        """
        return self.job_client.list_jobs()

    def job_status(self, job_id: str) -> str:
        """
        This method accesses the head ray node in your cluster and returns the job status for the provided job id.
        """
        return self.job_client.get_job_status(job_id)

    def job_logs(self, job_id: str) -> str:
        """
        This method accesses the head ray node in your cluster and returns the logs for the provided job id.
        """
        return self.job_client.get_job_logs(job_id)

    def torchx_config(
        self, working_dir: str = None, requirements: str = None
    ) -> Dict[str, str]:
        dashboard_address = urllib3.util.parse_url(self.cluster_dashboard_uri()).host
        to_return = {
            "cluster_name": self.config.name,
            "dashboard_address": dashboard_address,
        }
        if working_dir:
            to_return["working_dir"] = working_dir
        if requirements:
            to_return["requirements"] = requirements
        return to_return

    def from_k8_cluster_object(rc, mcad=True):
        machine_types = (
            rc["metadata"]["labels"]["orderedinstance"].split("_")
            if "orderedinstance" in rc["metadata"]["labels"]
            else []
        )
        local_interactive = (
            "volumeMounts"
            in rc["spec"]["workerGroupSpecs"][0]["template"]["spec"]["containers"][0]
        )
        cluster_config = ClusterConfiguration(
            name=rc["metadata"]["name"],
            namespace=rc["metadata"]["namespace"],
            machine_types=machine_types,
            num_workers=rc["spec"]["workerGroupSpecs"][0]["minReplicas"],
            min_cpus=rc["spec"]["workerGroupSpecs"][0]["template"]["spec"][
                "containers"
            ][0]["resources"]["requests"]["cpu"],
            max_cpus=rc["spec"]["workerGroupSpecs"][0]["template"]["spec"][
                "containers"
            ][0]["resources"]["limits"]["cpu"],
            min_memory=int(
                rc["spec"]["workerGroupSpecs"][0]["template"]["spec"]["containers"][0][
                    "resources"
                ]["requests"]["memory"][:-1]
            ),
            max_memory=int(
                rc["spec"]["workerGroupSpecs"][0]["template"]["spec"]["containers"][0][
                    "resources"
                ]["limits"]["memory"][:-1]
            ),
            num_gpus=rc["spec"]["workerGroupSpecs"][0]["template"]["spec"][
                "containers"
            ][0]["resources"]["limits"]["nvidia.com/gpu"],
            instascale=True if machine_types else False,
            image=rc["spec"]["workerGroupSpecs"][0]["template"]["spec"]["containers"][
                0
            ]["image"],
            local_interactive=local_interactive,
            mcad=mcad,
        )
        return Cluster(cluster_config)

    def local_client_url(self):
        if self.config.local_interactive == True:
            ingress_domain = _get_ingress_domain(self)
            return f"ray://{ingress_domain}"
        else:
            return "None"

    def _component_resources_up(
        self, namespace: str, api_instance: client.CustomObjectsApi
    ):
        with open(self.app_wrapper_yaml) as f:
            yamls = yaml.load_all(f, Loader=yaml.FullLoader)
            for resource in yamls:
                if resource["kind"] == "RayCluster":
                    api_instance.create_namespaced_custom_object(
                        group="ray.io",
                        version="v1alpha1",
                        namespace=namespace,
                        plural="rayclusters",
                        body=resource,
                    )
                elif resource["kind"] == "Route":
                    api_instance.create_namespaced_custom_object(
                        group="route.openshift.io",
                        version="v1",
                        namespace=namespace,
                        plural="routes",
                        body=resource,
                    )
                elif resource["kind"] == "Secret":
                    secret_instance = client.CoreV1Api(api_config_handler())
                    secret_instance.create_namespaced_secret(
                        namespace=namespace,
                        body=resource,
                    )

    def _component_resources_down(
        self, namespace: str, api_instance: client.CustomObjectsApi
    ):
        with open(self.app_wrapper_yaml) as f:
            yamls = yaml.load_all(f, Loader=yaml.FullLoader)
            for resource in yamls:
                if resource["kind"] == "RayCluster":
                    api_instance.delete_namespaced_custom_object(
                        group="ray.io",
                        version="v1alpha1",
                        namespace=namespace,
                        plural="rayclusters",
                        name=self.app_wrapper_name,
                    )
                elif resource["kind"] == "Route":
                    name = resource["metadata"]["name"]
                    api_instance.delete_namespaced_custom_object(
                        group="route.openshift.io",
                        version="v1",
                        namespace=namespace,
                        plural="routes",
                        name=name,
                    )
                elif resource["kind"] == "Secret":
                    name = resource["metadata"]["name"]
                    secret_instance = client.CoreV1Api(api_config_handler())
                    secret_instance.delete_namespaced_secret(
                        namespace=namespace,
                        name=name,
                    )


def list_all_clusters(namespace: str, print_to_console: bool = True):
    """
    Returns (and prints by default) a list of all clusters in a given namespace.
    """
    clusters = _get_ray_clusters(namespace)
    if print_to_console:
        pretty_print.print_clusters(clusters)
    return clusters


def list_all_queued(namespace: str, print_to_console: bool = True):
    """
    Returns (and prints by default) a list of all currently queued-up AppWrappers
    in a given namespace.
    """
    app_wrappers = _get_app_wrappers(
        namespace, filter=[AppWrapperStatus.RUNNING, AppWrapperStatus.PENDING]
    )
    if print_to_console:
        pretty_print.print_app_wrappers_status(app_wrappers)
    return app_wrappers


def get_current_namespace():  # pragma: no cover
    if api_config_handler() != None:
        if os.path.isfile("/var/run/secrets/kubernetes.io/serviceaccount/namespace"):
            try:
                file = open(
                    "/var/run/secrets/kubernetes.io/serviceaccount/namespace", "r"
                )
                active_context = file.readline().strip("\n")
                return active_context
            except Exception as e:
                print("Unable to find current namespace")
                return None
        else:
            print("Unable to find current namespace")
            return None
    else:
        try:
            _, active_context = config.list_kube_config_contexts(config_check())
        except Exception as e:
            return _kube_api_error_handling(e)
        try:
            return active_context["context"]["namespace"]
        except KeyError:
            return None


def get_cluster(cluster_name: str, namespace: str = "default"):
    try:
        config_check()
        api_instance = client.CustomObjectsApi(api_config_handler())
        rcs = api_instance.list_namespaced_custom_object(
            group="ray.io",
            version="v1alpha1",
            namespace=namespace,
            plural="rayclusters",
        )
    except Exception as e:
        return _kube_api_error_handling(e)

    for rc in rcs["items"]:
        if rc["metadata"]["name"] == cluster_name:
            mcad = _check_aw_exists(cluster_name, namespace)
            return Cluster.from_k8_cluster_object(rc, mcad=mcad)
    raise FileNotFoundError(
        f"Cluster {cluster_name} is not found in {namespace} namespace"
    )


# private methods
def _check_aw_exists(name: str, namespace: str) -> bool:
    try:
        config_check()
        api_instance = client.CustomObjectsApi(api_config_handler())
        aws = api_instance.list_namespaced_custom_object(
            group="workload.codeflare.dev",
            version="v1beta1",
            namespace=namespace,
            plural="appwrappers",
        )
    except Exception as e:  # pragma: no cover
        return _kube_api_error_handling(e, print_error=False)

    for aw in aws["items"]:
        if aw["metadata"]["name"] == name:
            return True
    return False


# Cant test this until get_current_namespace is fixed
def _get_ingress_domain(self):  # pragma: no cover
    try:
        config_check()
        api_client = client.NetworkingV1Api(api_config_handler())
        if self.config.namespace != None:
            namespace = self.config.namespace
        else:
            namespace = get_current_namespace()
        ingresses = api_client.list_namespaced_ingress(namespace)
    except Exception as e:  # pragma: no cover
        return _kube_api_error_handling(e)
    domain = None
    for ingress in ingresses.items:
        if ingress.spec.rules[0].http.paths[0].backend.service.port.number == 10001:
            domain = ingress.spec.rules[0].host
    return domain


def _app_wrapper_status(name, namespace="default") -> Optional[AppWrapper]:
    try:
        config_check()
        api_instance = client.CustomObjectsApi(api_config_handler())
        aws = api_instance.list_namespaced_custom_object(
            group="workload.codeflare.dev",
            version="v1beta1",
            namespace=namespace,
            plural="appwrappers",
        )
    except Exception as e:  # pragma: no cover
        return _kube_api_error_handling(e)

    for aw in aws["items"]:
        if aw["metadata"]["name"] == name:
            return _map_to_app_wrapper(aw)
    return None


def _ray_cluster_status(name, namespace="default") -> Optional[RayCluster]:
    try:
        config_check()
        api_instance = client.CustomObjectsApi(api_config_handler())
        rcs = api_instance.list_namespaced_custom_object(
            group="ray.io",
            version="v1alpha1",
            namespace=namespace,
            plural="rayclusters",
        )
    except Exception as e:  # pragma: no cover
        return _kube_api_error_handling(e)

    for rc in rcs["items"]:
        if rc["metadata"]["name"] == name:
            return _map_to_ray_cluster(rc)
    return None


def _get_ray_clusters(namespace="default") -> List[RayCluster]:
    list_of_clusters = []
    try:
        config_check()
        api_instance = client.CustomObjectsApi(api_config_handler())
        rcs = api_instance.list_namespaced_custom_object(
            group="ray.io",
            version="v1alpha1",
            namespace=namespace,
            plural="rayclusters",
        )
    except Exception as e:  # pragma: no cover
        return _kube_api_error_handling(e)

    for rc in rcs["items"]:
        list_of_clusters.append(_map_to_ray_cluster(rc))
    return list_of_clusters


def _get_app_wrappers(
    namespace="default", filter=List[AppWrapperStatus]
) -> List[AppWrapper]:
    list_of_app_wrappers = []

    try:
        config_check()
        api_instance = client.CustomObjectsApi(api_config_handler())
        aws = api_instance.list_namespaced_custom_object(
            group="workload.codeflare.dev",
            version="v1beta1",
            namespace=namespace,
            plural="appwrappers",
        )
    except Exception as e:  # pragma: no cover
        return _kube_api_error_handling(e)

    for item in aws["items"]:
        app_wrapper = _map_to_app_wrapper(item)
        if filter and app_wrapper.status in filter:
            list_of_app_wrappers.append(app_wrapper)
        else:
            # Unsure what the purpose of the filter is
            list_of_app_wrappers.append(app_wrapper)
    return list_of_app_wrappers


def _map_to_ray_cluster(rc) -> Optional[RayCluster]:
    if "state" in rc["status"]:
        status = RayClusterStatus(rc["status"]["state"].lower())
    else:
        status = RayClusterStatus.UNKNOWN
    try:
        config_check()
        api_instance = client.NetworkingV1Api(api_config_handler())
        ingresses = api_instance.list_namespaced_ingress(rc["metadata"]["namespace"])
    except Exception as e:  # pragma no cover
        return _kube_api_error_handling(e)
    ray_ingress = None
    for ingress in ingresses.items:
        annotations = ingress.metadata.annotations
        protocol = "http"
        if (
            ingress.metadata.name == f"ray-dashboard-{rc['metadata']['name']}"
            or ingress.metadata.name.startswith(f"{rc['metadata']['name']}-ingress")
        ):
            if annotations == None:
                protocol = "http"
            elif "route.openshift.io/termination" in annotations:
                protocol = "https"
        ray_ingress = f"{protocol}://{ingress.spec.rules[0].host}"

    return RayCluster(
        name=rc["metadata"]["name"],
        status=status,
        # for now we are not using autoscaling so same replicas is fine
        workers=rc["spec"]["workerGroupSpecs"][0]["replicas"],
        worker_mem_max=rc["spec"]["workerGroupSpecs"][0]["template"]["spec"][
            "containers"
        ][0]["resources"]["limits"]["memory"],
        worker_mem_min=rc["spec"]["workerGroupSpecs"][0]["template"]["spec"][
            "containers"
        ][0]["resources"]["requests"]["memory"],
        worker_cpu=rc["spec"]["workerGroupSpecs"][0]["template"]["spec"]["containers"][
            0
        ]["resources"]["limits"]["cpu"],
        worker_gpu=0,  # hard to detect currently how many gpus, can override it with what the user asked for
        namespace=rc["metadata"]["namespace"],
        head_cpus=rc["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0][
            "resources"
        ]["limits"]["cpu"],
        head_mem=rc["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0][
            "resources"
        ]["limits"]["memory"],
        head_gpu=rc["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0][
            "resources"
        ]["limits"]["nvidia.com/gpu"],
        dashboard=ray_ingress,
    )


def _map_to_app_wrapper(aw) -> AppWrapper:
    if "status" in aw and "canrun" in aw["status"]:
        return AppWrapper(
            name=aw["metadata"]["name"],
            status=AppWrapperStatus(aw["status"]["state"].lower()),
            can_run=aw["status"]["canrun"],
            job_state=aw["status"]["queuejobstate"],
        )
    return AppWrapper(
        name=aw["metadata"]["name"],
        status=AppWrapperStatus("queueing"),
        can_run=False,
        job_state="Still adding to queue",
    )


def _copy_to_ray(cluster: Cluster) -> RayCluster:
    ray = RayCluster(
        name=cluster.config.name,
        status=cluster.status(print_to_console=False)[0],
        workers=cluster.config.num_workers,
        worker_mem_min=cluster.config.min_memory,
        worker_mem_max=cluster.config.max_memory,
        worker_cpu=cluster.config.min_cpus,
        worker_gpu=cluster.config.num_gpus,
        namespace=cluster.config.namespace,
        dashboard=cluster.cluster_dashboard_uri(),
        head_cpus=cluster.config.head_cpus,
        head_mem=cluster.config.head_memory,
        head_gpu=cluster.config.head_gpus,
    )
    if ray.status == CodeFlareClusterStatus.READY:
        ray.status = RayClusterStatus.READY
    return ray
