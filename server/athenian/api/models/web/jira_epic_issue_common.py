from datetime import datetime, timedelta
from typing import Optional

from athenian.api.models.web.base_model_ import Model


class JIRAEpicIssueCommon(Model):
    """Common JIRA issue fields."""

    openapi_types = {
        "id": str,
        "title": str,
        "created": datetime,
        "updated": datetime,
        "work_began": Optional[datetime],
        "resolved": Optional[datetime],
        "lead_time": Optional[timedelta],
        "reporter": str,
        "assignee": Optional[str],
        "comments": int,
        "priority": str,
        "status": str,
    }

    attribute_map = {
        "id": "id",
        "title": "title",
        "created": "created",
        "updated": "updated",
        "work_began": "work_began",
        "resolved": "resolved",
        "lead_time": "lead_time",
        "reporter": "reporter",
        "assignee": "assignee",
        "comments": "comments",
        "priority": "priority",
        "status": "status",
    }

    __enable_slots__ = False

    def __init__(self,
                 id: Optional[str] = None,
                 title: Optional[str] = None,
                 created: Optional[datetime] = None,
                 updated: Optional[datetime] = None,
                 work_began: Optional[datetime] = None,
                 resolved: Optional[datetime] = None,
                 lead_time: Optional[timedelta] = None,
                 reporter: Optional[str] = None,
                 assignee: Optional[str] = None,
                 comments: Optional[int] = None,
                 priority: Optional[str] = None,
                 status: Optional[str] = None,
                 ):
        """JIRAEpicChild - a model defined in OpenAPI

        :param id: The id of this JIRAEpicIssueCommon.
        :param title: The title of this JIRAEpicIssueCommon.
        :param created: The created of this JIRAEpicIssueCommon.
        :param updated: The updated of this JIRAEpicIssueCommon.
        :param work_began: The work_began of this JIRAEpicIssueCommon.
        :param resolved: The resolved of this JIRAEpicIssueCommon.
        :param lead_time: The lead_time of this JIRAEpicIssueCommon.
        :param reporter: The reporter of this JIRAEpicIssueCommon.
        :param assignee: The assignee of this JIRAEpicIssueCommon.
        :param comments: The comments of this JIRAEpicIssueCommon.
        :param priority: The priority of this JIRAEpicIssueCommon.
        :param status: The status of this JIRAEpicIssueCommon.
        """
        self._id = id
        self._title = title
        self._created = created
        self._updated = updated
        self._work_began = work_began
        self._resolved = resolved
        self._lead_time = lead_time
        self._reporter = reporter
        self._assignee = assignee
        self._comments = comments
        self._priority = priority
        self._status = status

    def __lt__(self, other: "JIRAEpicIssueCommon") -> bool:
        """Support sorting."""
        return self._id < other._id

    @property
    def id(self) -> str:
        """Gets the id of this JIRAEpicIssueCommon.

        JIRA issue key `PROJECT-###`.

        :return: The id of this JIRAEpicIssueCommon.
        """
        return self._id

    @id.setter
    def id(self, id: str):
        """Sets the id of this JIRAEpicIssueCommon.

        JIRA issue key `PROJECT-###`.

        :param id: The id of this JIRAEpicIssueCommon.
        """
        if id is None:
            raise ValueError("Invalid value for `id`, must not be `None`")

        self._id = id

    @property
    def title(self) -> str:
        """Gets the title of this JIRAEpicIssueCommon.

        Title of this issue.

        :return: The title of this JIRAEpicIssueCommon.
        """
        return self._title

    @title.setter
    def title(self, title: str):
        """Sets the title of this JIRAEpicIssueCommon.

        Title of this issue.

        :param title: The title of this JIRAEpicIssueCommon.
        """
        if title is None:
            raise ValueError("Invalid value for `title`, must not be `None`")

        self._title = title

    @property
    def created(self) -> datetime:
        """Gets the created of this JIRAEpicIssueCommon.

        When this issue was created.

        :return: The created of this JIRAEpicIssueCommon.
        """
        return self._created

    @created.setter
    def created(self, created: datetime):
        """Sets the created of this JIRAEpicIssueCommon.

        When this issue was created.

        :param created: The created of this JIRAEpicIssueCommon.
        """
        if created is None:
            raise ValueError("Invalid value for `created`, must not be `None`")

        self._created = created

    @property
    def updated(self) -> datetime:
        """Gets the updated of this JIRAEpicIssueCommon.

        When this issue was updated.

        :return: The updated of this JIRAEpicIssueCommon.
        """
        return self._updated

    @updated.setter
    def updated(self, updated: datetime):
        """Sets the updated of this JIRAEpicIssueCommon.

        When this issue was updated.

        :param updated: The updated of this JIRAEpicIssueCommon.
        """
        if updated is None:
            raise ValueError("Invalid value for `updated`, must not be `None`")

        self._updated = updated

    @property
    def work_began(self) -> Optional[datetime]:
        """Gets the work_began of this JIRAEpicIssueCommon.

        When the issue entered the "In Progress" stage. This timestamp can be missing and is always
        less than or equal to `resolved`.

        :return: The work_began of this JIRAEpicIssueCommon.
        """
        return self._work_began

    @work_began.setter
    def work_began(self, work_began: Optional[datetime]):
        """Sets the work_began of this JIRAEpicIssueCommon.

        When the issue entered the "In Progress" stage. This timestamp can be missing and is always
        less than or equal to `resolved`.

        :param work_began: The work_began of this JIRAEpicIssueCommon.
        """
        self._work_began = work_began

    @property
    def resolved(self) -> Optional[datetime]:
        """Gets the resolved of this JIRAEpicIssueCommon.

        When the issue was marked as completed. This timestamp can be missing and is always greater
        than or equal to `work_began`.

        :return: The resolved of this JIRAEpicIssueCommon.
        """
        return self._resolved

    @resolved.setter
    def resolved(self, resolved: Optional[datetime]):
        """Sets the resolved of this JIRAEpicIssueCommon.

        When the issue was marked as completed. This timestamp can be missing and is always greater
        than or equal to `work_began`.

        :param resolved: The resolved of this JIRAEpicIssueCommon.
        """
        self._resolved = resolved

    @property
    def lead_time(self) -> Optional[timedelta]:
        """Gets the lead_time of this JIRAEpicIssueCommon.

        Issue's time spent between `work_began` and `resolved`.

        :return: The lead_time of this JIRAEpicIssueCommon.
        """
        return self._lead_time

    @lead_time.setter
    def lead_time(self, lead_time: Optional[timedelta]):
        """Sets the lead_time of this JIRAEpicIssueCommon.

        Issue's time spent between `work_began` and `resolved`.

        :param lead_time: The lead_time of this JIRAEpicIssueCommon.
        """
        self._lead_time = lead_time

    @property
    def reporter(self) -> str:
        """Gets the reporter of this JIRAEpicIssueCommon.

        Name of the person who reported the issue.

        :return: The reporter of this JIRAEpicIssueCommon.
        """
        return self._reporter

    @reporter.setter
    def reporter(self, reporter: str):
        """Sets the reporter of this JIRAEpicIssueCommon.

        Name of the person who reported the issue.

        :param reporter: The reporter of this JIRAEpicIssueCommon.
        """
        if reporter is None:
            raise ValueError("Invalid value for `reporter`, must not be `None`")

        self._reporter = reporter

    @property
    def assignee(self) -> Optional[str]:
        """Gets the assignee of this JIRAEpicIssueCommon.

        Name of the assigned person.

        :return: The assignee of this JIRAEpicIssueCommon.
        """
        return self._assignee

    @assignee.setter
    def assignee(self, assignee: Optional[str]):
        """Sets the assignee of this JIRAEpicIssueCommon.

        Name of the assigned person.

        :param assignee: The assignee of this JIRAEpicIssueCommon.
        """
        self._assignee = assignee

    @property
    def comments(self) -> int:
        """Gets the comments of this JIRAEpicIssueCommon.

        Number of comments in the issue excluding sub-tasks.

        :return: The comments of this JIRAEpicIssueCommon.
        """
        return self._comments

    @comments.setter
    def comments(self, comments: int):
        """Sets the comments of this JIRAEpicIssueCommon.

        Number of comments in the issue excluding sub-tasks.

        :param comments: The comments of this JIRAEpicIssueCommon.
        """
        if comments is None:
            raise ValueError("Invalid value for `comments`, must not be `None`")

        self._comments = comments

    @property
    def priority(self) -> str:
        """Gets the priority of this JIRAEpicIssueCommon.

        Name of the priority. The details are returned in `FilteredJIRAStuff.priorities`.

        :return: The priority of this JIRAEpicIssueCommon.
        """
        return self._priority

    @priority.setter
    def priority(self, priority: str):
        """Sets the priority of this JIRAEpicIssueCommon.

        Name of the priority. The details are returned in `FilteredJIRAStuff.priorities`.

        :param priority: The priority of this JIRAEpicIssueCommon.
        """
        if priority is None:
            raise ValueError("Invalid value for `priority`, must not be `None`")

        self._priority = priority

    @property
    def status(self) -> str:
        """Gets the status of this JIRAEpicIssueCommon.

        Name of the status. The details are returned in `FilteredJIRAStuff.statuses`.

        :return: The status of this JIRAEpicIssueCommon.
        """
        return self._status

    @status.setter
    def status(self, status: str):
        """Sets the status of this JIRAEpicIssueCommon.

        Name of the status. The details are returned in `FilteredJIRAStuff.statuses`.

        :param status: The status of this JIRAEpicIssueCommon.
        """
        if status is None:
            raise ValueError("Invalid value for `status`, must not be `None`")

        self._status = status
