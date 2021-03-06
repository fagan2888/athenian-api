import datetime
from itertools import chain
from lzma import LZMAFile
import os
from pathlib import Path

from sqlalchemy.cprocessors import str_to_date, str_to_datetime
import sqlalchemy.orm
from sqlalchemy.sql.type_api import Variant

from athenian.api.controllers import invitation_controller
from athenian.api.controllers.invitation_controller import _generate_account_secret
from athenian.api.models.metadata import __min_version__
from athenian.api.models.metadata.github import Base as GithubBase, NodePullRequest, PullRequest, \
    PushCommit, \
    SchemaMigration
from athenian.api.models.metadata.jira import Base as JiraBase
from athenian.api.models.state.models import Account, AccountFeature, AccountGitHubAccount, \
    AccountJiraInstallation, Feature, FeatureComponent, Invitation, RepositorySet, UserAccount


def fill_metadata_session(session: sqlalchemy.orm.Session):
    models = {}
    tables = {**GithubBase.metadata.tables, **JiraBase.metadata.tables}
    for cls in chain(GithubBase._decl_class_registry.values(),
                     JiraBase._decl_class_registry.values()):
        table = getattr(cls, "__table__", None)
        if table is not None:
            models[table.fullname] = cls
    data_file = os.getenv("DB_DATA")
    if data_file is None:
        data_file = Path(__file__).with_name("test_data.sql.xz")
    else:
        data_file = Path(data_file)
    if data_file.suffix == ".xz":
        opener = lambda: LZMAFile(data_file)  # noqa:E731
    else:
        opener = lambda: open(data_file, "rb")  # noqa:E731
    with opener() as fin:
        stdin = False
        for line in fin:
            if not stdin and line.startswith(b"COPY "):
                stdin = True
                parts = line[5:].split(b" ")
                table = parts[0].decode()
                if table.startswith("public."):
                    table = table[7:]
                model = models[table]
                columns = {}
                for c in tables[table].columns:
                    if isinstance(c.type, Variant):
                        pt = c.type.load_dialect_impl(session.bind.dialect).python_type
                    else:
                        pt = c.type.python_type
                    if pt is datetime.date:
                        ctor = str_to_date
                    elif pt is datetime.datetime:
                        ctor = str_to_datetime
                    elif pt is bool:
                        ctor = lambda x: x == "t" or x == "1"  # noqa:E731
                    elif issubclass(pt, (list, dict)):
                        ctor = lambda x: [s for s in x.strip("{}").split(",") if s]  # noqa
                    else:
                        ctor = lambda x: x  # noqa:E731
                    columns[c.name] = ctor
                keys = [p.strip(b'(),"').decode() for p in parts[1:-2]]
                continue
            if stdin:
                if line == b"\\.\n":
                    stdin = False
                    session.flush()
                    continue
                kwargs = {}
                vals = line[:-1].split(b"\t")
                for k, p in zip(keys, vals):
                    p = p.replace(b"\\t", b"\t").replace(b"\\n", b"\n").decode()
                    if p == r"\N":
                        kwargs[k] = None
                    else:
                        try:
                            kwargs[k] = columns[k](p)
                        except Exception as e:
                            print("%s.%s" % (table, k), p)
                            for k, p in zip(keys, vals):
                                print(k, '"%s"' % p.decode())
                            raise e from None
                session.add(model(**kwargs))
                if table == "github.api_pull_requests":
                    session.add(NodePullRequest(id=kwargs["node_id"], acc_id=kwargs["acc_id"]))
    session.add(SchemaMigration(version=__min_version__, dirty=False))
    session.flush()
    # append missed merge commit IDs to PRs
    commit_ids = {h: n for h, n in session.query(PushCommit.sha, PushCommit.node_id)}
    for pr in session.query(PullRequest).filter(PullRequest.merge_commit_sha.isnot(None)):
        pr.merge_commit_id = commit_ids[pr.merge_commit_sha]


def fill_state_session(session: sqlalchemy.orm.Session):
    salt, secret = _generate_account_secret(1, "secret")
    session.add(Account(secret_salt=salt, secret=secret))
    salt, secret = _generate_account_secret(2, "secret")
    session.add(Account(secret_salt=salt, secret=secret))
    salt, secret = _generate_account_secret(3, "secret")
    session.add(Account(secret_salt=salt, secret=secret))
    salt, secret = _generate_account_secret(invitation_controller.admin_backdoor, "secret")
    session.add(Account(id=invitation_controller.admin_backdoor, secret_salt=salt, secret=secret))
    session.flush()
    session.add(AccountGitHubAccount(id=6366825, account_id=1))
    session.add(UserAccount(
        user_id="auth0|5e1f6dfb57bc640ea390557b", account_id=1, is_admin=True))
    session.add(UserAccount(
        user_id="auth0|5e1f6dfb57bc640ea390557b", account_id=2, is_admin=False))
    session.add(UserAccount(
        user_id="auth0|5e1f6e2e8bfa520ea5290741", account_id=3, is_admin=True))
    session.add(UserAccount(
        user_id="auth0|5e1f6e2e8bfa520ea5290741", account_id=1, is_admin=False))
    session.add(RepositorySet(
        name="all",
        owner_id=1,
        precomputed=os.getenv("PRECOMPUTED", "1") == "1",
        items=["github.com/src-d/gitbase", "github.com/src-d/go-git"]))
    session.add(RepositorySet(
        name="all",
        owner_id=2,
        items=["github.com/src-d/hercules", "github.com/athenianco/athenian-api"]))
    session.add(RepositorySet(
        name="all",
        owner_id=3,
        items=["github.com/athenianco/athenian-webapp", "github.com/athenianco/athenian-api"]))
    session.add(Invitation(salt=777, account_id=3, created_by="auth0|5e1f6e2e8bfa520ea5290741"))
    session.add(Feature(id=1, name="jira", component=FeatureComponent.webapp, enabled=True,
                        default_parameters={"a": "b", "c": "d"}))
    session.flush()
    session.add(AccountFeature(account_id=1, feature_id=1, enabled=True,
                               parameters={"a": "x"}))
    session.add(AccountJiraInstallation(id=1, account_id=1))
