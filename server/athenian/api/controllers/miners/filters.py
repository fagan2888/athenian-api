from dataclasses import dataclass
from typing import Iterable, Optional, Set

from athenian.api.models.web.jira_filter import JIRAFilter as WebJIRAFilter


@dataclass(frozen=True)
class LabelFilter:
    """Pull Request labels: must/must not contain."""

    include: Set[str]
    exclude: Set[str]

    @classmethod
    def from_iterables(cls,
                       include: Optional[Iterable[str]],
                       exclude: Optional[Iterable[str]],
                       ) -> "LabelFilter":
        """Initialize a new instance of LabelFilter from two iterables."""
        return cls(include=set(include or []), exclude=set(exclude or []))

    @classmethod
    def empty(cls) -> "LabelFilter":
        """Initialize an empty LabelFilter."""
        return cls(set(), set())

    def __bool__(self) -> bool:
        """Return value indicating whether there is at least one included or excluded label."""
        return bool(self.include) or bool(self.exclude)

    def __str__(self) -> str:
        """Implement str()."""
        return "[%s, %s]" % (sorted(self.include), sorted(self.exclude))

    def __repr__(self) -> str:
        """Implement repr()."""
        return "LabelFilter(%r, %r)" % (self.include, self.exclude)

    def compatible_with(self, other: "LabelFilter") -> bool:
        """Check whether the `other` filter can be applied to the items filtered by `self`."""
        return (
            ((not self.include) or (other.include and self.include.issuperset(other.include)))
            and
            ((not self.exclude) or (other.exclude and self.exclude.issubset(other.exclude)))
        )


@dataclass(frozen=True)
class JIRAFilter:
    """JIRA traits to select assigned PRs."""

    labels: LabelFilter
    epics: Set[str]
    issue_types: Set[str]

    @classmethod
    def empty(cls) -> "JIRAFilter":
        """Initialize an empty JIRAFilter."""
        return cls(LabelFilter.empty(), set(), set())

    def __bool__(self) -> bool:
        """Return value indicating whether this filter is not an identity."""
        return bool(self.labels) or bool(self.epics) or bool(self.issue_types)

    def __str__(self) -> str:
        """Implement str()."""
        return "[%s, %s, %s]" % (self.labels, sorted(self.epics), sorted(self.issue_types))

    def __repr__(self) -> str:
        """Implement repr()."""
        return "JIRAFilter(%r, %r, %r)" % (self.labels, self.epics, self.issue_types)

    def compatible_with(self, other: "JIRAFilter") -> bool:
        """Check whether the `other` filter can be applied to the items filtered by `self`."""
        if not self.labels.compatible_with(other.labels):
            return False
        if self.epics and (not other.epics or
                           not self.epics.issuperset(other.epics)):
            return False
        if self.issue_types and (not other.issue_types or
                                 not self.issue_types.issuperset(other.issue_types)):
            return False
        return True

    @classmethod
    def from_web(cls, model: Optional[WebJIRAFilter]) -> "JIRAFilter":
        """Initialize a new JIRAFilter from the corresponding web model."""
        if model is None:
            return cls.empty()
        labels = LabelFilter.from_iterables(model.labels_include, model.labels_exclude)
        return JIRAFilter(labels=labels,
                          epics=set(model.epics),
                          issue_types=set(model.issue_types))