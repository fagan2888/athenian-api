from typing import Optional

from athenian.api.models.web.base_model_ import Model


class CreatedIdentifier(Model):
    """NOTE: This class is auto generated by OpenAPI Generator (https://openapi-generator.tech).

    Do not edit the class manually.
    """

    def __init__(self, id: Optional[int] = None):
        """CreatedIdentifier - a model defined in OpenAPI

        :param id: The id of this CreatedIdentifier.
        """
        self.openapi_types = {
            "id": int,
        }
        self.attribute_map = {
            "id": "id",
        }
        self._id = id

    @property
    def id(self):
        """Gets the id of this CreatedIdentifier.

        Identifier of the created entity.

        :return: The id of this CreatedIdentifier.
        :rtype: int
        """
        return self._id

    @id.setter
    def id(self, id):
        """Sets the id of this CreatedIdentifier.

        Identifier of the created entity.

        :param id: The id of this CreatedIdentifier.
        :type id: int
        """
        if id is None:
            raise ValueError("Invalid value for `id`, must not be `None`")

        self._id = id
