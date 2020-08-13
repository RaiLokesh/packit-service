# MIT License
#
# Copyright (c) 2018-2019 Red Hat, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import logging
from typing import Optional, Tuple, Set, List

from copr.v3 import CoprRequestException

from ogr.abstract import GitProject, CommitStatus
from packit.config import JobType, JobConfig
from packit.config.aliases import get_build_targets
from packit.config.package_config import PackageConfig
from packit.exceptions import PackitCoprException, PackitCoprSettingsException
from packit_service import sentry_integration
from packit_service.celerizer import celery_app
from packit_service.config import ServiceConfig, Deployment
from packit_service.constants import MSG_RETRIGGER
from packit_service.models import (
    CoprBuildModel,
    AbstractTriggerDbType,
)
from packit_service.service.events import EventData
from packit_service.service.urls import (
    get_srpm_log_url_from_flask,
    get_copr_build_info_url_from_flask,
)
from packit_service.worker.build.build_helper import BaseBuildJobHelper
from packit_service.worker.result import TaskResults

logger = logging.getLogger(__name__)


class CoprBuildJobHelper(BaseBuildJobHelper):
    job_type_build = JobType.copr_build
    job_type_test = JobType.tests
    status_name_build: str = "rpm-build"
    status_name_test: str = "testing-farm"

    def __init__(
        self,
        service_config: ServiceConfig,
        package_config: PackageConfig,
        project: GitProject,
        metadata: EventData,
        db_trigger: AbstractTriggerDbType,
        job_config: JobConfig,
    ):
        super().__init__(
            service_config=service_config,
            package_config=package_config,
            project=project,
            metadata=metadata,
            db_trigger=db_trigger,
            job_config=job_config,
        )

        self.msg_retrigger: str = MSG_RETRIGGER.format(
            build="copr-build" if self.job_build else "build"
        )

    @property
    def default_project_name(self) -> str:
        """
        Project name for copr -- add `-stg` suffix for the stg app.
        """
        stg = "-stg" if self.service_config.deployment == Deployment.stg else ""
        return f"{self.project.namespace}-{self.project.repo}-{self.metadata.identifier}{stg}"

    @property
    def job_project(self) -> Optional[str]:
        """
        The job definition from the config file.
        """
        if self.job_build and self.job_build.metadata.project:
            return self.job_build.metadata.project

        return self.default_project_name

    @property
    def job_owner(self) -> Optional[str]:
        """
        Owner used for the copr build -- search the config or use the copr's config.
        """
        if self.job_build and self.job_build.metadata.owner:
            return self.job_build.metadata.owner

        return self.api.copr_helper.copr_client.config.get("username")

    @property
    def preserve_project(self) -> Optional[bool]:
        """
        If the project will be preserved or can be removed after 60 days.
        """
        return self.job_build.metadata.preserve_project if self.job_build else None

    @property
    def list_on_homepage(self) -> Optional[bool]:
        """
        If the project will be shown on the copr home page.
        """
        return self.job_build.metadata.list_on_homepage if self.job_build else None

    @property
    def additional_repos(self) -> Optional[List[str]]:
        """
        Additional repos that will be enable for copr build.
        """
        return self.job_build.metadata.additional_repos if self.job_build else None

    @property
    def build_targets(self) -> Set[str]:
        """
        Return the chroots to build.

        (Used when submitting the copr build and as a part of the commit status name.)

        1. If the job is not defined, use the test chroots.
        2. If the job is defined without targets, use "fedora-stable".
        """
        return get_build_targets(*self.configured_build_targets, default=None)

    @property
    def tests_targets(self) -> Set[str]:
        """
        Return the list of chroots used in testing farm.
        Has to be a sub-set of the `build_targets`.

        (Used when submitting the copr build and as a part of the commit status name.)

        Return an empty list if there is no job configured.

        If not defined:
        1. use the build_targets if the job si configured
        2. use "fedora-stable" alias otherwise
        """
        return get_build_targets(*self.configured_tests_targets, default=None)

    def run_copr_build(self) -> TaskResults:

        if not (self.job_build or self.job_tests):
            msg = "No copr_build or tests job defined."
            # we can't report it to end-user at this stage
            return TaskResults(success=False, details={"msg": msg})

        self.report_status_to_all(
            description="Building SRPM ...",
            state=CommitStatus.pending,
            # pagure requires "valid url"
            url="",
        )
        self.create_srpm_if_needed()

        if not self.srpm_model.success:
            msg = "SRPM build failed, check the logs for details."
            self.report_status_to_all(
                state=CommitStatus.failure,
                description=msg,
                url=get_srpm_log_url_from_flask(self.srpm_model.id),
            )
            return TaskResults(success=False, details={"msg": msg})

        try:
            build_id, web_url = self.run_build()
        except Exception as ex:
            sentry_integration.send_to_sentry(ex)
            # TODO: Where can we show more info about failure?
            # TODO: Retry
            self.report_status_to_all(
                state=CommitStatus.error,
                description=f"Submit of the build failed: {ex}",
            )
            return TaskResults(
                success=False,
                details={"msg": "Submit of the Copr build failed.", "error": str(ex)},
            )

        for chroot in self.build_targets:
            copr_build = CoprBuildModel.get_or_create(
                build_id=str(build_id),
                commit_sha=self.metadata.commit_sha,
                project_name=self.job_project,
                owner=self.job_owner,
                web_url=web_url,
                target=chroot,
                status="pending",
                srpm_build=self.srpm_model,
                trigger_model=self.db_trigger,
            )
            url = get_copr_build_info_url_from_flask(id_=copr_build.id)
            self.report_status_to_all_for_chroot(
                state=CommitStatus.pending,
                description="Starting RPM build...",
                url=url,
                chroot=chroot,
            )

        # release the hounds!
        celery_app.send_task(
            "task.babysit_copr_build",
            args=(build_id,),
            countdown=120,  # do the first check in 120s
        )

        return TaskResults(success=True, details={})

    def run_build(
        self, target: Optional[str] = None
    ) -> Tuple[Optional[int], Optional[str]]:
        """
        Trigger the build and return id and web_url
        :param target: str, run for all if not set
        :return: task_id, task_url
        """

        owner = self.job_owner or self.api.copr_helper.configured_owner
        if not owner:
            raise PackitCoprException(
                "Copr owner not set. Use Copr config file or `--owner` when calling packit CLI."
            )

        try:
            overwrite_booleans = owner == "packit"
            self.api.copr_helper.create_copr_project_if_not_exists(
                project=self.job_project,
                chroots=list(self.build_targets),
                owner=owner,
                description=None,
                instructions=None,
                list_on_homepage=self.list_on_homepage if overwrite_booleans else None,
                preserve_project=self.preserve_project if overwrite_booleans else None,
                additional_repos=self.additional_repos,
                request_admin_if_needed=True,
            )
        except PackitCoprSettingsException as ex:
            if self.metadata.pr_id:

                table = (
                    "| field | old value | new value |\n"
                    "| ----- | --------- | --------- |\n"
                )
                for field, (old, new) in ex.fields_to_change.items():
                    table += f"| {field} | {old} | {new} |\n"

                msg = (
                    "Based on your Packit configuration the settings "
                    f"of the {owner}/{self.job_project} "
                    "Copr project would need to be updated as follows:\n"
                    "\n"
                    f"{table}"
                    "\n"
                    "Packit was unable to update the settings above as it is missing `admin` "
                    f"permissions on the {owner}/{self.job_project} Copr project.\n"
                    "\n"
                    "To fix this you can do one of the following:\n"
                    "\n"
                    f"- Grant Packit `admin` permissions on the {owner}/{self.job_project} "
                    "Copr project.\n"
                    "- Change the above Copr project settings manually "
                    "to match the Packit configuration.\n"
                    "- Update the Packit configuration to match the Copr project settings.\n"
                    "\n"
                    "Please re-trigger the build, once the issue above is fixed.\n"
                )
                self.project.pr_comment(pr_id=self.metadata.pr_id, body=msg)
            raise ex

        logger.debug(
            f"owner={owner}, project={self.job_project}, path={self.srpm_path}"
        )

        try:
            build = self.api.copr_helper.copr_client.build_proxy.create_from_file(
                ownername=owner, projectname=self.job_project, path=self.srpm_path
            )
        except CoprRequestException as ex:
            if "You don't have permissions to build in this copr." in str(ex):
                self.api.copr_helper.copr_client.project_proxy.request_permissions(
                    ownername=owner,
                    projectname=self.job_project,
                    permissions={"builder": True},
                )
                if self.metadata.pr_id:
                    permissions_url = self.api.copr_helper.get_copr_settings_url(
                        owner, self.job_project, section="permissions"
                    )
                    self.project.pr_comment(
                        pr_id=self.metadata.pr_id,
                        body="We have requested the `builder` permissions "
                        f"for the {owner}/{self.job_project} Copr project.\n"
                        "\n"
                        "Please confirm the request on the "
                        f"[{owner}/{self.job_project} Copr project permissions page]"
                        f"({permissions_url})"
                        " and re-trigger the build.",
                    )
            raise ex

        return build.id, self.api.copr_helper.copr_web_build_url(build)
