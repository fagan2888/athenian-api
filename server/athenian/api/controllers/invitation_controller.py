import base64
import binascii
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
import logging
import marshal
import os
import pickle
from random import randint
from sqlite3 import IntegrityError, OperationalError
import struct
from typing import Callable, Coroutine, List, Optional, Tuple

from aiohttp import web
import aiomcache
import aiosqlite.core
from asyncpg import IntegrityConstraintViolationError
import databases.core
from slack_sdk.web.async_client import AsyncWebClient as SlackWebClient
from sqlalchemy import and_, delete, func, insert, select, update

from athenian.api import metadata
from athenian.api.auth import Auth0, disable_default_user
from athenian.api.cache import cached, max_exptime
from athenian.api.controllers.account import generate_jira_invitation_link, \
    get_metadata_account_ids, get_user_account_status, jira_url_template
from athenian.api.controllers.ffx import decrypt, encrypt
from athenian.api.controllers.reposet import load_account_reposets
from athenian.api.defer import defer
from athenian.api.models.metadata.github import Account as MetadataAccount, AccountRepository, \
    FetchProgress
from athenian.api.models.state.models import Account, Invitation, RepositorySet, UserAccount
from athenian.api.models.web import BadRequestError, ForbiddenError, GenericError, \
    NoSourceDataError, NotFoundError, User
from athenian.api.models.web.generic_error import DatabaseConflict, TooManyRequestsError
from athenian.api.models.web.installation_progress import InstallationProgress
from athenian.api.models.web.invitation_check_result import InvitationCheckResult
from athenian.api.models.web.invitation_link import InvitationLink
from athenian.api.models.web.invited_user import InvitedUser
from athenian.api.models.web.table_fetching_progress import TableFetchingProgress
from athenian.api.request import AthenianWebRequest
from athenian.api.response import model_response, ResponseError
from athenian.api.typing_utils import DatabaseLike


admin_backdoor = (1 << 24) - 1
url_prefix = os.getenv("ATHENIAN_INVITATION_URL_PREFIX")


def validate_env():
    """Check that the required global parameters are set."""
    if Auth0.KEY is None:
        raise EnvironmentError("ATHENIAN_INVITATION_KEY environment variable must be set")
    if url_prefix is None:
        raise EnvironmentError("ATHENIAN_INVITATION_URL_PREFIX environment variable must be set")
    if jira_url_template is None:
        raise EnvironmentError(
            "ATHENIAN_JIRA_INSTALLATION_URL_TEMPLATE environment variable must be set")


async def gen_invitation(request: AthenianWebRequest, id: int) -> web.Response:
    """Generate a new regular member invitation URL."""
    async with request.sdb.connection() as sdb_conn:
        await get_user_account_status(request.uid, id, sdb_conn, request.cache)
        existing = await sdb_conn.fetch_one(
            select([Invitation.id, Invitation.salt])
            .where(and_(Invitation.is_active, Invitation.account_id == id)))
        if existing is not None:
            invitation_id = existing[Invitation.id.key]
            salt = existing[Invitation.salt.key]
        else:
            # create a new invitation
            salt = randint(0, (1 << 16) - 1)  # 0:65535 - 2 bytes
            inv = Invitation(salt=salt, account_id=id, created_by=request.uid).create_defaults()
            invitation_id = await sdb_conn.execute(insert(Invitation).values(inv.explode()))
        slug = encode_slug(invitation_id, salt, request.app["auth"].key)
        model = InvitationLink(url=url_prefix + slug)
        return model_response(model)


async def _check_admin_access(uid: str, account: int, sdb_conn: databases.core.Connection):
    status = await sdb_conn.fetch_one(
        select([UserAccount.is_admin])
        .where(and_(UserAccount.user_id == uid, UserAccount.account_id == account)))
    if status is None:
        raise ResponseError(NotFoundError(
            detail="User %s is not in the account %d" % (uid, account)))
    if not status[UserAccount.is_admin.key]:
        raise ResponseError(ForbiddenError(
            detail="User %s is not an admin of the account %d" % (uid, account)))


def encode_slug(iid: int, salt: int, key: str) -> str:
    """Encode an invitation ID and some extra data to 8 chars."""
    part1 = struct.pack("!H", salt)  # 2 bytes
    part2 = struct.pack("!I", iid)[1:]  # 3 bytes
    binseq = part1 + part2  # 5 bytes, 10 hex chars
    encseq = encrypt(binseq, key.encode())  # encrypted 5 bytes, 10 hex chars
    finseq = base64.b32encode(bytes.fromhex(encseq)).lower().decode()  # 8 base32 chars
    finseq = finseq.replace("o", "8").replace("l", "9")
    return finseq


def decode_slug(slug: str, key: str) -> (int, int):
    """Decode an invitation ID and some extra data from 8 chars."""
    assert len(slug) == 8
    assert isinstance(slug, str)
    b32 = slug.replace("8", "o").replace("9", "l").upper().encode()
    x = base64.b32decode(b32).hex()
    x = decrypt(x, key.encode())
    salt = struct.unpack("!H", x[:2])[0]
    iid = struct.unpack("!I", b"\x00" + x[2:])[0]
    return iid, salt


@disable_default_user
async def accept_invitation(request: AthenianWebRequest, body: dict) -> web.Response:
    """Accept the membership invitation."""
    if getattr(request, "god_id", request.uid) != request.uid:
        raise ResponseError(ForbiddenError(
            detail="You must not be an active god to accept an invitation."))

    def bad_req():
        raise ResponseError(BadRequestError(detail="Invalid invitation URL")) from None

    sdb = request.sdb
    url = InvitationLink.from_dict(body).url
    if not url.startswith(url_prefix):
        bad_req()
    x = url[len(url_prefix):].strip("/")
    if len(x) != 8:
        bad_req()
    try:
        iid, salt = decode_slug(x, request.app["auth"].key)
    except binascii.Error:
        bad_req()
    async with sdb.connection() as conn:
        try:
            async with conn.transaction():
                acc_id, user = await _accept_invitation(iid, salt, request, conn)
        except (IntegrityConstraintViolationError, IntegrityError, OperationalError) as e:
            return ResponseError(DatabaseConflict(detail=str(e))).response
    return model_response(InvitedUser(account=acc_id, user=user))


async def _accept_invitation(iid: int,
                             salt: int,
                             request: AthenianWebRequest,
                             conn: databases.core.Connection,
                             ) -> Tuple[int, User]:
    log = logging.getLogger(metadata.__package__)
    inv = await conn.fetch_one(
        select([Invitation.account_id, Invitation.accepted, Invitation.is_active])
        .where(and_(Invitation.id == iid, Invitation.salt == salt)))
    if inv is None:
        raise ResponseError(NotFoundError(detail="Invitation was not found."))
    if not inv[Invitation.is_active.key]:
        raise ResponseError(ForbiddenError(detail="This invitation is disabled."))
    acc_id = inv[Invitation.account_id.key]
    is_admin = acc_id == admin_backdoor
    slack = request.app["slack"]  # type: SlackWebClient
    if is_admin:
        other_accounts = await conn.fetch_all(select([UserAccount.account_id])
                                              .where(and_(UserAccount.user_id == request.uid,
                                                          UserAccount.is_admin)))
        if other_accounts:
            other_accounts = {row[0] for row in other_accounts}
            installed_accounts = await conn.fetch_all(
                select([RepositorySet.owner_id])
                .where(and_(RepositorySet.owner_id.in_(other_accounts),
                            RepositorySet.name == RepositorySet.ALL,
                            RepositorySet.precomputed)))
            installed = {row[0] for row in installed_accounts}
            if other_accounts - installed:
                raise ResponseError(TooManyRequestsError(
                    type="/errors/DuplicateAccountRegistrationError",
                    detail="You cannot accept new admin invitations until your account's "
                           "installation finishes."))
        # create a new account for the admin user
        acc_id = await create_new_account(conn, request.app["auth"].key)
        if acc_id >= admin_backdoor:
            await conn.execute(delete(Account).where(Account.id == acc_id))
            raise ResponseError(GenericError(
                type="/errors/LockedError",
                title=HTTPStatus.LOCKED.phrase,
                status=HTTPStatus.LOCKED,
                detail="Invitation was not found."))
        log.info("Created new account %d", acc_id)
        if slack is not None:
            async def report_new_account_to_slack():
                await slack.post("new_account.jinja2", user=await request.user(), account=acc_id)

            await defer(report_new_account_to_slack(), "report_new_account_to_slack")
        status = None
    else:
        status = await conn.fetch_one(select([UserAccount.is_admin])
                                      .where(and_(UserAccount.user_id == request.uid,
                                                  UserAccount.account_id == acc_id)))
    if status is None:
        # create the user<>account record
        user = UserAccount(
            user_id=request.uid,
            account_id=acc_id,
            is_admin=is_admin,
        ).create_defaults()
        await conn.execute(insert(UserAccount).values(user.explode(with_primary_keys=True)))
        log.info("Assigned user %s to account %d (admin: %s)", request.uid, acc_id, is_admin)
        if slack is not None:
            async def report_new_user_to_slack():
                await slack.post("new_user.jinja2", user=await request.user(), account=acc_id)

            await defer(report_new_user_to_slack(), "report_new_user_to_slack")
        values = {Invitation.accepted.key: inv[Invitation.accepted.key] + 1}
        await conn.execute(update(Invitation).where(Invitation.id == iid).values(values))
    user = await (await request.user()).load_accounts(conn)
    return acc_id, user


async def create_new_account(conn: DatabaseLike, secret: str) -> int:
    """Create a new account."""
    if isinstance(conn, databases.Database):
        slow = conn.url.dialect == "sqlite"
    else:
        slow = isinstance(conn.raw_connection, aiosqlite.core.Connection)
    if slow:
        return await _create_new_account_slow(conn, secret)
    return await _create_new_account_fast(conn, secret)


async def _create_new_account_fast(conn: DatabaseLike, secret: str) -> int:
    """Create a new account.

    Should be used for PostgreSQL.
    """
    account_id = await conn.execute(
        insert(Account).values(Account(secret_salt=0, secret=Account.missing_secret)
                               .create_defaults().explode()))
    salt, secret = _generate_account_secret(account_id, secret)
    await conn.execute(update(Account).where(Account.id == account_id).values({
        Account.secret_salt: salt,
        Account.secret: secret,
    }))
    return account_id


async def _create_new_account_slow(conn: DatabaseLike, secret: str) -> int:
    """Create a new account without relying on autoincrement.

    SQLite does not allow resetting the primary key sequence, so we have to increment the ID
    by hand.
    """
    acc = Account(secret_salt=0, secret=Account.missing_secret).create_defaults()
    max_id = (await conn.fetch_one(select([func.max(Account.id)])
                                   .where(Account.id < admin_backdoor)))[0] or 0
    acc.id = max_id + 1
    acc_id = await conn.execute(insert(Account).values(acc.explode(with_primary_keys=True)))
    salt, secret = _generate_account_secret(acc_id, secret)
    await conn.execute(update(Account).where(Account.id == acc_id).values({
        Account.secret_salt: salt,
        Account.secret: secret,
    }))
    return acc_id


async def check_invitation(request: AthenianWebRequest, body: dict) -> web.Response:
    """Given an invitation URL, get its type (admin or regular account member) and find whether \
    it is enabled or disabled."""
    url = InvitationLink.from_dict(body).url
    result = InvitationCheckResult(valid=False)
    if not url.startswith(url_prefix):
        return model_response(result)
    x = url[len(url_prefix):].strip("/")
    if len(x) != 8:
        return model_response(result)
    try:
        iid, salt = decode_slug(x, request.app["auth"].key)
    except binascii.Error:
        return model_response(result)
    inv = await request.sdb.fetch_one(
        select([Invitation.account_id, Invitation.is_active])
        .where(and_(Invitation.id == iid, Invitation.salt == salt)))
    if inv is None:
        return model_response(result)
    result.valid = True
    result.active = inv[Invitation.is_active.key]
    types = [InvitationCheckResult.INVITATION_TYPE_REGULAR,
             InvitationCheckResult.INVITATION_TYPE_ADMIN]
    result.type = types[inv[Invitation.account_id.key] == admin_backdoor]
    return model_response(result)


@cached(
    exptime=24 * 3600,  # 1 day
    serialize=lambda t: marshal.dumps(t),
    deserialize=lambda buf: marshal.loads(buf),
    key=lambda account, **_: (account,),
)
async def get_installation_event_ids(account: int,
                                     sdb: DatabaseLike,
                                     mdb: DatabaseLike,
                                     cache: Optional[aiomcache.Client],
                                     ) -> List[Tuple[int, str]]:
    """Load the GitHub account and delivery event IDs for the given sdb account."""
    meta_ids = await get_metadata_account_ids(account, sdb, cache)
    rows = await mdb.fetch_all(
        select([AccountRepository.acc_id, AccountRepository.event_id])
        .where(AccountRepository.acc_id.in_(meta_ids))
        .distinct())
    if diff := set(meta_ids) - {r[0] for r in rows}:
        raise ResponseError(NoSourceDataError(detail="Some installation%s missing: %s." %
                                                     ("s are" if len(diff) > 1 else " is", diff)))
    return [(r[0], r[1]) for r in rows]


@cached(
    exptime=max_exptime,
    serialize=lambda s: s.encode(),
    deserialize=lambda b: b.decode(),
    key=lambda metadata_account_id, **_: (metadata_account_id,),
    refresh_on_access=True,
)
async def get_installation_owner(metadata_account_id: int,
                                 mdb_conn: databases.core.Connection,
                                 cache: Optional[aiomcache.Client],
                                 ) -> str:
    """Load the native user ID who installed the app."""
    user_login = await mdb_conn.fetch_val(
        select([MetadataAccount.owner_login])
        .where(MetadataAccount.id == metadata_account_id))
    if user_login is None:
        raise ResponseError(NoSourceDataError(detail="The installation has not started yet."))
    return user_login


@cached(exptime=5,  # matches the webapp poll interval
        serialize=pickle.dumps,
        deserialize=pickle.loads,
        key=lambda account, **_: (account,))
async def fetch_github_installation_progress(account: int,
                                             sdb: DatabaseLike,
                                             mdb: databases.Database,
                                             cache: Optional[aiomcache.Client],
                                             ) -> InstallationProgress:
    """Load the GitHub installation progress for the specified account."""
    log = logging.getLogger("%s.fetch_github_installation_progress" % metadata.__package__)
    mdb_sqlite = mdb.url.dialect == "sqlite"
    idle_threshold = timedelta(hours=3)
    async with mdb.connection() as mdb_conn:
        event_ids = await get_installation_event_ids(account, sdb, mdb_conn, cache)
        owner = await get_installation_owner(event_ids[0][0], mdb_conn, cache)
        # we don't cache this because the number of repos can dynamically change
        models = []
        for metadata_account_id, event_id in event_ids:
            repositories = await mdb_conn.fetch_val(
                select([func.count(AccountRepository.repo_node_id)])
                .where(AccountRepository.acc_id == metadata_account_id))
            rows = await mdb_conn.fetch_all(
                select([FetchProgress])
                .where(and_(FetchProgress.event_id == event_id,
                            FetchProgress.acc_id == metadata_account_id)))
            if not rows:
                continue
            tables = [TableFetchingProgress(fetched=r[FetchProgress.nodes_processed.key],
                                            name=r[FetchProgress.node_type.key],
                                            total=r[FetchProgress.nodes_total.key])
                      for r in rows]
            started_date = min(r[FetchProgress.created_at.key] for r in rows)
            if mdb_sqlite:
                started_date = started_date.replace(tzinfo=timezone.utc)
            finished_date = max(r[FetchProgress.updated_at.key] for r in rows)
            if mdb_sqlite:
                finished_date = finished_date.replace(tzinfo=timezone.utc)
            pending = sum(t.fetched < t.total for t in tables)
            if datetime.now(tz=timezone.utc) - finished_date > idle_threshold:
                for table in tables:
                    table.total = table.fetched
                if pending:
                    log.info("Overriding the installation progress by the idle time threshold; "
                             "there are %d pending tables, last update on %s",
                             pending, finished_date)
                    finished_date += idle_threshold  # don't fool the user
            elif pending:
                finished_date = None
            model = InstallationProgress(started_date=started_date,
                                         finished_date=finished_date,
                                         owner=owner,
                                         repositories=repositories,
                                         tables=tables)
            models.append(model)
        if not models:
            raise ResponseError(NoSourceDataError(
                detail="No installation progress exists for account %d." % account))
        tables = {}
        finished_date = datetime(2020, 1, 1, tzinfo=timezone.utc)
        for m in models:
            for t in m.tables:
                table = tables.setdefault(
                    t.name, TableFetchingProgress(name=t.name, fetched=0, total=0))
                table.fetched += t.fetched
                table.total += t.total
            if model.finished_date is None:
                finished_date = None
            elif finished_date is not None:
                finished_date = max(finished_date, model.finished_date)
        model = InstallationProgress(started_date=min(m.started_date for m in models),
                                     finished_date=finished_date,
                                     owner=owner,
                                     repositories=sum(m.repositories for m in models),
                                     tables=sorted(tables.values()))
        return model


async def _append_precomputed_progress(model: InstallationProgress,
                                       account: int,
                                       uid: str,
                                       login: Callable[[], Coroutine[None, None, str]],
                                       sdb: DatabaseLike,
                                       mdb: databases.Database,
                                       cache: Optional[aiomcache.Client],
                                       slack: Optional[SlackWebClient]) -> None:
    reposets = await load_account_reposets(
        account, login,
        [RepositorySet.name, RepositorySet.precomputed, RepositorySet.created_at],
        sdb, mdb, cache, slack)
    precomputed = False
    created = None
    for reposet in reposets:
        if reposet[RepositorySet.name.key] == RepositorySet.ALL:
            precomputed = reposet[RepositorySet.precomputed.key]
            created = reposet[RepositorySet.created_at.key].replace(tzinfo=timezone.utc)
            break
    if slack is not None and not precomputed and model.finished_date is not None \
            and datetime.now(timezone.utc) - model.finished_date > timedelta(hours=2) \
            and datetime.now(timezone.utc) - created > timedelta(hours=2):
        await _notify_precomputed_failure(slack, uid, account, model, created, cache)
    model.tables.append(TableFetchingProgress(
        name="precomputed", fetched=int(precomputed), total=1))
    if not precomputed:
        model.finished_date = None


@cached(
    exptime=2 * 3600,
    serialize=marshal.dumps,
    deserialize=marshal.loads,
    key=lambda account, **_: (account,),
    refresh_on_access=True,
)
async def _notify_precomputed_failure(slack: Optional[SlackWebClient],
                                      uid: str,
                                      account: int,
                                      model: InstallationProgress,
                                      created: datetime,
                                      cache: Optional[aiomcache.Client]) -> None:
    await slack.post(
        "precomputed_failure.jinja2", uid=uid, account=account, model=model, created=created)


async def eval_invitation_progress(request: AthenianWebRequest, id: int) -> web.Response:
    """Return the current Athenian GitHub app installation progress."""
    await get_user_account_status(request.uid, id, request.sdb, request.cache)
    model = await fetch_github_installation_progress(id, request.sdb, request.mdb, request.cache)

    async def login_loader() -> str:
        return (await request.user()).login

    await _append_precomputed_progress(
        model, id, request.uid, login_loader, request.sdb, request.mdb,
        request.cache, request.app["slack"])
    return model_response(model)


def _generate_account_secret(account_id: int, key: str) -> Tuple[int, str]:
    salt = randint(0, (1 << 16) - 1)  # 0:65535 - 2 bytes
    secret = encode_slug(account_id, salt, key)
    return salt, secret


async def gen_jira_link(request: AthenianWebRequest, id: int) -> web.Response:
    """Generate JIRA integration installation link."""
    account_id = id
    async with request.sdb.connection() as sdb_conn:
        await _check_admin_access(request.uid, account_id, sdb_conn)
        url = await generate_jira_invitation_link(account_id, sdb_conn)
        model = InvitationLink(url=url)
        return model_response(model)
