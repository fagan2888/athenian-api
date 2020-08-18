from typing import List, Optional

from athenian.api.models.web.base_model_ import Model
from athenian.api.models.web.calculated_linear_metric_values import CalculatedLinearMetricValues
from athenian.api.models.web.release_metric_id import ReleaseMetricID


class CalculatedReleaseMetric(Model):
    """Response from `/metrics/releases`."""

    openapi_types = {
        "for_": List[str],
        "matches": List[str],
        "metrics": List[str],
        "granularity": str,
        "values": List[CalculatedLinearMetricValues],
    }

    attribute_map = {
        "for_": "for",
        "matches": "matches",
        "metrics": "metrics",
        "granularity": "granularity",
        "values": "values",
    }

    __slots__ = ["_" + k for k in openapi_types]

    def __init__(
        self,
        for_: List[str] = None,
        matches: Optional[List[str]] = None,
        metrics: List[str] = None,
        granularity: str = None,
        values: List[CalculatedLinearMetricValues] = None,
    ):
        """CalculatedReleaseMetric - a model defined in OpenAPI

        :param for_: The for_ of this CalculatedReleaseMetric.
        :param matches: The matches of this CalculatedReleaseMetric.
        :param metrics: The metrics of this CalculatedReleaseMetric.
        :param granularity: The granularity of this CalculatedReleaseMetric.
        :param values: The values of this CalculatedReleaseMetric.
        """
        self._for_ = for_
        self._matches = matches
        self._metrics = metrics
        self._granularity = granularity
        self._values = values

    @property
    def for_(self) -> List[str]:
        """Gets the for_ of this CalculatedReleaseMetric.

        :return: The for_ of this CalculatedReleaseMetric.
        """
        return self._for_

    @for_.setter
    def for_(self, for_: List[str]):
        """Sets the for_ of this CalculatedReleaseMetric.

        :param for_: The for_ of this CalculatedReleaseMetric.
        """
        if for_ is None:
            raise ValueError("Invalid value for `for_`, must not be `None`")

        self._for_ = for_

    @property
    def matches(self) -> List[str]:
        """Gets the matches of this CalculatedReleaseMetric.

        :return: The matches of this CalculatedReleaseMetric.
        """
        return self._matches

    @matches.setter
    def matches(self, matches: List[str]):
        """Sets the matches of this CalculatedReleaseMetric.

        :param matches: The matches of this CalculatedReleaseMetric.
        """
        if matches is None:
            raise ValueError("Invalid value for `matches`, must not be `None`")

        self._matches = matches

    @property
    def metrics(self) -> List[str]:
        """Gets the metrics of this CalculatedReleaseMetric.

        :return: The metrics of this CalculatedReleaseMetric.
        """
        return self._metrics

    @metrics.setter
    def metrics(self, metrics: List[str]):
        """Sets the metrics of this CalculatedReleaseMetric.

        :param metrics: The metrics of this CalculatedReleaseMetric.
        """
        if metrics is None:
            raise ValueError("Invalid value for `metrics`, must not be `None`")
        if metrics not in ReleaseMetricID:
            raise ValueError("`metrics` must consist only of %s" % list(ReleaseMetricID))

        self._metrics = metrics

    @property
    def granularity(self) -> str:
        """Gets the granularity of this CalculatedReleaseMetric.

        How often the metrics are reported. The value must satisfy the following regular
        expression: /^(([1-9]\\d* )?(day|week|month|year)|all)$/. \"all\" produces a single
        interval [`date_from`, `date_to`].

        :return: The granularity of this CalculatedReleaseMetric.
        """
        return self._granularity

    @granularity.setter
    def granularity(self, granularity: str):
        """Sets the granularity of this CalculatedReleaseMetric.

        How often the metrics are reported. The value must satisfy the following regular
        expression: /^(([1-9]\\d* )?(day|week|month|year)|all)$/. \"all\" produces a single
        interval [`date_from`, `date_to`].

        :param granularity: The granularity of this CalculatedReleaseMetric.
        :type granularity: str
        """
        if granularity is None:
            raise ValueError("Invalid value for `granularity`, must not be `None`")

        self._granularity = granularity

    @property
    def values(self) -> List[CalculatedLinearMetricValues]:
        """Gets the values of this CalculatedReleaseMetric.

        :return: The values of this CalculatedReleaseMetric.
        """
        return self._values

    @values.setter
    def values(self, values: List[CalculatedLinearMetricValues]):
        """Sets the values of this CalculatedReleaseMetric.

        :param values: The values of this CalculatedReleaseMetric.
        """
        if values is None:
            raise ValueError("Invalid value for `values`, must not be `None`")

        self._values = values
