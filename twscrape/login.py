import imaplib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
import pyotp

from httpx import AsyncClient, HTTPStatusError, Response

from .account import Account
from .imap import imap_get_email_code, imap_login
from .logger import logger
from .utils import raise_for_status, utc

LOGIN_URL = "https://api.twitter.com/1.1/onboarding/task.json"


@dataclass
class LoginConfig:
    email_first: bool = False
    manual: bool = False


@dataclass
class TaskCtx:
    client: AsyncClient
    acc: Account
    cfg: LoginConfig
    prev: Any
    imap: None | imaplib.IMAP4_SSL


async def get_guest_token(client: AsyncClient):
    rep = await client.post("https://api.twitter.com/1.1/guest/activate.json")
    raise_for_status(rep, "guest_token (most likely ip ban)")
    return rep.json()["guest_token"]


async def login_initiate(client: AsyncClient) -> Response:
    payload = {
        "input_flow_data": {
            "flow_context": {"debug_overrides": {}, "start_location": {"location": "unknown"}}
        },
        "subtask_versions": {},
    }

    rep = await client.post(LOGIN_URL, params={"flow_name": "login"}, json=payload)
    raise_for_status(rep, "login_initiate")
    return rep


async def login_instrumentation(ctx: TaskCtx) -> Response:
    payload = {
        "flow_token": ctx.prev["flow_token"],
        "subtask_inputs": [
            {
                "subtask_id": "LoginJsInstrumentationSubtask",
                "js_instrumentation": {"response": "{\"rf\":{\"ee9d114bd114827ce8d4cca456f19b3374321d65f0c52660b185810fef13f85e\":-8,\"a1a6293fd3b347788d825c22dc3f5da69e314776cd4e10978f0508c1727c471e\":124,\"ad9f941bbb8b0a8bbc18aeebb8d5b2b3ee363c68fbde149b7f9ee9945abfe522\":-214,\"c32d5d4020fe0180a3afdee9e931d1c38a436cfa58e511be8f0be534934beba1\":203},\"s\":\"WCdLUtsnS3qdTPMQrei9PN3O7Ln86ojKdsZyfMmr5q0jEsdXE6KR7qrF1eOaKlf75eReaup2xTEuBSAXd55oPEDL79NZtoM5tr33sVgNhL2N2YVLPI7X3h-0Ah2NvS6WaQTbLXK0ShEiGS9z48qalQ-oM5smlxhZhLRL7rS-y9IB_euQooEmwC3Dyn-Ka8uXybagc8C6ENaKBk9cBDkw7CFHBKekunjnKElUr0VGCYuuWJtX2PL4AMkZgBtpD_2PVbl_RN8mZkw7cx5Qbr_dGvo8vNKSmHdCwYFwKz6q38TMXXEEgQw_3BnYpqnhC4P-xDwrR_b3W7S2zZ8rSp6wUgAAAYj7_CMQ\"}", "link": "next_link"},
            }
        ],
    }

    rep = await ctx.client.post(LOGIN_URL, json=payload)
    raise_for_status(rep, "login_instrumentation")
    return rep


async def login_enter_username(ctx: TaskCtx) -> Response:
    payload = {
        "flow_token": ctx.prev["flow_token"],
        "subtask_inputs": [
            {
                "subtask_id": "LoginEnterUserIdentifierSSO",
                "settings_list": {
                    "setting_responses": [
                        {
                            "key": "user_identifier",
                            "response_data": {"text_data": {"result": ctx.acc.username}},
                        }
                    ],
                    "link": "next_link",
                },
            }
        ],
    }

    rep = await ctx.client.post(LOGIN_URL, json=payload)
    raise_for_status(rep, "login_username")
    return rep


async def login_enter_password(ctx: TaskCtx) -> Response:
    payload = {
        "flow_token": ctx.prev["flow_token"],
        "subtask_inputs": [
            {
                "subtask_id": "LoginEnterPassword",
                "enter_password": {"password": ctx.acc.password, "link": "next_link"},
            }
        ],
    }

    rep = await ctx.client.post(LOGIN_URL, json=payload)
    raise_for_status(rep, "login_password")
    return rep


async def login_two_factor_auth_challenge(ctx: TaskCtx) -> Response:
    totp = pyotp.TOTP(ctx.acc.mfa_code)
    payload = {
        "flow_token": ctx.prev["flow_token"],
        "subtask_inputs": [
            {
                "subtask_id": "LoginTwoFactorAuthChallenge",
                "enter_text": {"text": totp.now(), "link": "next_link"},
            }
        ],
    }

    rep = await ctx.client.post(LOGIN_URL, json=payload)
    raise_for_status(rep, "login_two_factor_auth_challenge")
    return rep


async def login_duplication_check(ctx: TaskCtx) -> Response:
    payload = {
        "flow_token": ctx.prev["flow_token"],
        "subtask_inputs": [
            {
                "subtask_id": "AccountDuplicationCheck",
                "check_logged_in_account": {"link": "AccountDuplicationCheck_false"},
            }
        ],
    }

    rep = await ctx.client.post(LOGIN_URL, json=payload)
    raise_for_status(rep, "login_duplication_check")
    return rep


async def login_confirm_email(ctx: TaskCtx) -> Response:
    payload = {
        "flow_token": ctx.prev["flow_token"],
        "subtask_inputs": [
            {
                "subtask_id": "LoginAcid",
                "enter_text": {"text": ctx.acc.email, "link": "next_link"},
            }
        ],
    }

    rep = await ctx.client.post(LOGIN_URL, json=payload)
    raise_for_status(rep, "login_confirm_email")
    return rep


async def login_confirm_email_code(ctx: TaskCtx):
    if ctx.cfg.manual:
        print(f"Enter email code for {ctx.acc.username} / {ctx.acc.email}")
        value = input("Code: ")
        value = value.strip()
    else:
        if not ctx.imap:
            ctx.imap = await imap_login(ctx.acc.email, ctx.acc.email_password)

        now_time = utc.now() - timedelta(seconds=30)
        value = await imap_get_email_code(ctx.imap, ctx.acc.email, now_time)

    payload = {
        "flow_token": ctx.prev["flow_token"],
        "subtask_inputs": [
            {
                "subtask_id": "LoginAcid",
                "enter_text": {"text": value, "link": "next_link"},
            }
        ],
    }

    rep = await ctx.client.post(LOGIN_URL, json=payload)
    raise_for_status(rep, "login_confirm_email")
    return rep


async def login_success(ctx: TaskCtx) -> Response:
    payload = {
        "flow_token": ctx.prev["flow_token"],
        "subtask_inputs": [],
    }

    rep = await ctx.client.post(LOGIN_URL, json=payload)
    raise_for_status(rep, "login_success")
    return rep


async def next_login_task(ctx: TaskCtx, rep: Response):
    ct0 = ctx.client.cookies.get("ct0", None)
    if ct0:
        ctx.client.headers["x-csrf-token"] = ct0
        ctx.client.headers["x-twitter-auth-type"] = "OAuth2Session"

    ctx.prev = rep.json()
    assert "flow_token" in ctx.prev, f"flow_token not in {rep.text}"

    for x in ctx.prev["subtasks"]:
        print('login task', x)
        task_id = x["subtask_id"]

        try:
            if task_id == "LoginSuccessSubtask":
                return await login_success(ctx)
            if task_id == "LoginAcid":
                is_code = x["enter_text"]["hint_text"].lower() == "confirmation code"
                fn = login_confirm_email_code if is_code else login_confirm_email
                return await fn(ctx)
            if task_id == "AccountDuplicationCheck":
                return await login_duplication_check(ctx)
            if task_id == "LoginEnterPassword":
                return await login_enter_password(ctx)
            if task_id == "LoginTwoFactorAuthChallenge":
                return await login_two_factor_auth_challenge(ctx)
            if task_id == "LoginEnterUserIdentifierSSO":
                return await login_enter_username(ctx)
            if task_id == "LoginJsInstrumentationSubtask":
                return await login_instrumentation(ctx)
        except Exception as e:
            ctx.acc.error_msg = f"login_step={task_id} err={e}"
            raise e

    return None


async def login(acc: Account, cfg: LoginConfig | None = None) -> Account:
    log_id = f"{acc.username} - {acc.email}"
    if acc.active:
        logger.info(f"account already active {log_id}")
        return acc

    cfg, imap = cfg or LoginConfig(), None
    if cfg.email_first and not cfg.manual:
        imap = await imap_login(acc.email, acc.email_password)

    async with acc.make_client() as client:
        guest_token = await get_guest_token(client)
        client.headers["x-guest-token"] = guest_token

        rep = await login_initiate(client)
        ctx = TaskCtx(client, acc, cfg, None, imap)
        while True:
            if not rep:
                break

            try:
                rep = await next_login_task(ctx, rep)
            except HTTPStatusError as e:
                if e.response.status_code == 403:
                    logger.error(f"403 error {log_id}")
                    return acc

        client.headers["x-csrf-token"] = client.cookies["ct0"]
        client.headers["x-twitter-auth-type"] = "OAuth2Session"

        acc.active = True
        acc.headers = {k: v for k, v in client.headers.items()}
        acc.cookies = {k: v for k, v in client.cookies.items()}
        return acc
