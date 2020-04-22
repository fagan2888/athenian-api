from datetime import timedelta, timezone

import faker
import pytest

from athenian.api.controllers.miners.github.pull_request import Fallback, PullRequestTimes


@pytest.fixture
def pr_samples():
    def generate(n):
        fake = faker.Faker()

        def random_pr():
            created_at = fake.date_time_between(
                start_date="-3y", end_date="-6M", tzinfo=timezone.utc)
            first_commit = fake.date_time_between(
                start_date="-3y1M", end_date=created_at, tzinfo=timezone.utc)
            last_commit_before_first_review = fake.date_time_between(
                start_date=created_at, end_date=created_at + timedelta(days=30),
                tzinfo=timezone.utc)
            first_comment_on_first_review = fake.date_time_between(
                start_date=last_commit_before_first_review, end_date=timedelta(days=2),
                tzinfo=timezone.utc)
            first_review_request = fake.date_time_between(
                start_date=last_commit_before_first_review, end_date=first_comment_on_first_review,
                tzinfo=timezone.utc)
            first_passed_checks = fake.date_time_between(
                start_date=created_at, end_date=first_review_request, tzinfo=timezone.utc)
            approved_at = fake.date_time_between(
                start_date=first_comment_on_first_review + timedelta(days=1),
                end_date=first_comment_on_first_review + timedelta(days=30),
                tzinfo=timezone.utc)
            last_commit = fake.date_time_between(
                start_date=first_comment_on_first_review + timedelta(days=1),
                end_date=approved_at,
                tzinfo=timezone.utc)
            last_passed_checks = fake.date_time_between(
                last_commit, last_commit + timedelta(days=1), tzinfo=timezone.utc)
            merged_at = fake.date_time_between(
                approved_at, approved_at + timedelta(days=2), tzinfo=timezone.utc)
            closed_at = merged_at
            last_review = fake.date_time_between(approved_at, closed_at, tzinfo=timezone.utc)
            released_at = fake.date_time_between(
                merged_at, merged_at + timedelta(days=30), tzinfo=timezone.utc)
            return PullRequestTimes(
                created=Fallback(created_at, None),
                first_commit=Fallback(first_commit, created_at),
                last_commit_before_first_review=Fallback(last_commit_before_first_review, None),
                last_commit=Fallback(last_commit, None),
                merged=Fallback(merged_at, None),
                first_comment_on_first_review=Fallback(first_comment_on_first_review, None),
                first_review_request=Fallback(first_review_request, None),
                last_review=Fallback(last_review, None),
                approved=Fallback(approved_at, None),
                first_passed_checks=Fallback(first_passed_checks, None),
                last_passed_checks=Fallback(last_passed_checks, None),
                released=Fallback(released_at, None),
                closed=Fallback(closed_at, None),
            )

        return [random_pr() for _ in range(n)]
    return generate