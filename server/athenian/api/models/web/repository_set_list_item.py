from datetime import datetime
from typing import Optional

from athenian.api.models.web.base_model_ import Model


class RepositorySetListItem(Model):
    """NOTE: This class is auto generated by OpenAPI Generator (https://openapi-generator.tech).

    Do not edit the class manually.
    """

    openapi_types = {
        "id": int,
        "name": str,
        "created": datetime,
        "updated": datetime,
        "items_count": int,
    }

    attribute_map = {
        "id": "id",
        "name": "name",
        "created": "created",
        "updated": "updated",
        "items_count": "items_count",
    }

    __slots__ = ["_" + k for k in openapi_types]

    def __init__(
        self,
        id: Optional[int] = None,
        name: Optional[str] = None,
        created: Optional[datetime] = None,
        updated: Optional[datetime] = None,
        items_count: Optional[int] = None,
    ):
        """RepositorySetListItem - a model defined in OpenAPI

        :param id: The id of this RepositorySetListItem.
        :param name: The name of this RepositorySetListItem.
        :param created: The created of this RepositorySetListItem.
        :param updated: The updated of this RepositorySetListItem.
        :param items_count: The items_count of this RepositorySetListItem.
        """
        self._id = id
        self._name = name
        self._created = created
        self._updated = updated
        self._items_count = items_count

    @property
    def id(self) -> int:
        """Gets the epository set identifier of this RepositorySetListItem.

        :return: The id of this RepositorySetListItem.
        """
        return self._id

    @id.setter
    def id(self, id: int):
        """Sets the repository set identifier of this RepositorySetListItem.

        :param id: The id of this RepositorySetListItem.
        """
        self._id = id

    @property
    def name(self) -> str:
        """Gets the epository set identifier of this RepositorySetListItem.

        :return: The name of this RepositorySetListItem.
        """
        return self._name

    @name.setter
    def name(self, name: str):
        """Sets the repository set identifier of this RepositorySetListItem.

        :param name: The name of this RepositorySetListItem.
        """
        self._name = name

    @property
    def created(self) -> datetime:
        """Gets the created of this RepositorySetListItem.

        Date and time of creation of the repository set.

        :return: The created of this RepositorySetListItem.
        """
        return self._created

    @created.setter
    def created(self, created: datetime):
        """Sets the created of this RepositorySetListItem.

        Date and time of creation of the repository set.

        :param created: The created of this RepositorySetListItem.
        """
        self._created = created

    @property
    def updated(self) -> datetime:
        """Gets the updated of this RepositorySetListItem.

        Date and time of the last change of the repository set.

        :return: The updated of this RepositorySetListItem.
        """
        return self._updated

    @updated.setter
    def updated(self, updated: datetime):
        """Sets the updated of this RepositorySetListItem.

        Date and time of the last change of the repository set.

        :param updated: The updated of this RepositorySetListItem.
        """
        self._updated = updated

    @property
    def items_count(self) -> int:
        """Gets the items_count of this RepositorySetListItem.

        Number of repositories in the set.

        :return: The items_count of this RepositorySetListItem.
        """
        return self._items_count

    @items_count.setter
    def items_count(self, items_count: int):
        """Sets the items_count of this RepositorySetListItem.

        Number of repositories in the set.

        :param items_count: The items_count of this RepositorySetListItem.
        """
        self._items_count = items_count
