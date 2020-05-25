"""Diversify PR participants

Revision ID: af98f4f19811
Revises: 49c42f5d53b5
Create Date: 2020-05-21 20:59:51.971583+00:00

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import HSTORE
from sqlalchemy.orm import Session

# revision identifiers, used by Alembic.

revision = "af98f4f19811"
down_revision = "49c42f5d53b5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("github_pull_request_times") as bop:
        bop.drop_column("developers")
        bop.add_column(sa.Column("author", sa.CHAR(100)))
        bop.add_column(sa.Column("merger", sa.CHAR(100)))
        bop.add_column(sa.Column("releaser", sa.CHAR(100)))
        bop.add_column(sa.Column("reviewers", HSTORE(), nullable=False, server_default=""))
        bop.add_column(sa.Column("commenters", HSTORE(), nullable=False, server_default=""))
        bop.add_column(sa.Column("commit_authors", HSTORE(), nullable=False, server_default=""))
        bop.add_column(sa.Column("commit_committers", HSTORE(), nullable=False, server_default=""))
        bop.alter_column("format_version", server_default="2")
        bop.alter_column("data", nullable=False)
    sql = """
    CREATE INDEX github_pull_request_times_author
    ON github_pull_request_times
    ("author");
    """
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        session = Session(bind=bind)
        session.execute(sql)


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        session = Session(bind=bind)
        session.execute("DROP INDEX github_pull_request_times_author;")
    with op.batch_alter_table("github_pull_request_times") as bop:
        bop.drop_column("author")
        bop.drop_column("merger")
        bop.drop_column("releaser")
        bop.drop_column("reviewers")
        bop.drop_column("commenters")
        bop.drop_column("commit_authors")
        bop.drop_column("commit_committers")
        bop.add_column(sa.Column("developers", HSTORE(), nullable=False, server_default=""))
        bop.alter_column("format_version", server_default="1")
        bop.alter_column("data", nullable=True)