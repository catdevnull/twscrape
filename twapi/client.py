import email as emaillib
import imaplib
import json
import os
import time
from datetime import datetime, timezone

from fake_useragent import UserAgent
from httpx import AsyncClient, Client, HTTPStatusError, Response
from loguru import logger

TOKEN = "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
TASK_URL = "https://api.twitter.com/1.1/onboarding/task.json"


class RateLimitExceeded(Exception):
    def __init__(self, reset: int, cursor: str | None = None):
        self.reset = reset
        self.cursor = cursor


def search_email_with_confirmation_code(imap: imaplib.IMAP4_SSL, msg_count: int) -> str | None:
    for i in range(msg_count, 0, -1):
        _, rep = imap.fetch(str(i), "(RFC822)")
        for x in rep:
            if isinstance(x, tuple):
                msg = emaillib.message_from_bytes(x[1])
                if "info@twitter.com" in msg.get("From", ""):
                    # eg. Your Twitter confirmation code is XXX
                    subject = str(msg.get("Subject", ""))
                    return subject.split(" ")[-1].strip()

    return None


def get_verification_code(email: str, password: str, imap_domain: None | str = None) -> str | None:
    imap_domain = f"imap.{email.split('@')[1]}" if imap_domain is None else imap_domain

    with imaplib.IMAP4_SSL(imap_domain) as imap:
        imap.login(email, password)

        before_count = 0
        while True:
            _, rep = imap.select("INBOX")
            msg_count = int(rep[0].decode("utf-8")) if len(rep) > 0 and rep[0] is not None else 0
            if msg_count > before_count:
                code = search_email_with_confirmation_code(imap, msg_count)
                if code is not None:
                    return code

            logger.debug(f"Waiting for confirmation email... {msg_count}")
            time.sleep(1)


def raise_for_status(rep: Response, label: str):
    try:
        rep.raise_for_status()
    except HTTPStatusError:
        raise Exception(f"{label} - {rep.status_code} - {rep.text}")


def login_get_guest_token(client: Client) -> str:
    rep = client.post("https://api.twitter.com/1.1/guest/activate.json")
    raise_for_status(rep, "guest_token")
    return rep.json()["guest_token"]


def login_initiate(client: Client) -> Response:
    payload = {
        "input_flow_data": {
            "flow_context": {"debug_overrides": {}, "start_location": {"location": "unknown"}}
        },
        "subtask_versions": {},
    }

    rep = client.post(TASK_URL, params={"flow_name": "login"}, json=payload)
    raise_for_status(rep, "login_initiate")
    return rep


def login_instrumentation(client, flow_token: str) -> Response:
    payload = {
        "flow_token": flow_token,
        "subtask_inputs": [
            {
                "subtask_id": "LoginJsInstrumentationSubtask",
                "js_instrumentation": {"response": "{}", "link": "next_link"},
            }
        ],
    }

    rep = client.post(TASK_URL, json=payload)
    raise_for_status(rep, "login_instrumentation")
    return rep


def login_enter_username(client: Client, flow_token: str, username: str) -> Response:
    payload = {
        "flow_token": flow_token,
        "subtask_inputs": [
            {
                "subtask_id": "LoginEnterUserIdentifierSSO",
                "settings_list": {
                    "setting_responses": [
                        {
                            "key": "user_identifier",
                            "response_data": {"text_data": {"result": username}},
                        }
                    ],
                    "link": "next_link",
                },
            }
        ],
    }

    rep = client.post(TASK_URL, json=payload)
    raise_for_status(rep, "login_username")
    return rep


def login_enter_password(client: Client, flow_token: str, password: str) -> Response:
    payload = {
        "flow_token": flow_token,
        "subtask_inputs": [
            {
                "subtask_id": "LoginEnterPassword",
                "enter_password": {"password": password, "link": "next_link"},
            }
        ],
    }

    rep = client.post(TASK_URL, json=payload)
    raise_for_status(rep, "login_password")
    return rep


def login_duplication_check(client: Client, flow_token: str) -> Response:
    payload = {
        "flow_token": flow_token,
        "subtask_inputs": [
            {
                "subtask_id": "AccountDuplicationCheck",
                "check_logged_in_account": {"link": "AccountDuplicationCheck_false"},
            }
        ],
    }

    rep = client.post(TASK_URL, json=payload)
    raise_for_status(rep, "login_duplication_check")
    return rep


def get_confirm_email_code(task: dict, email: str, email_password: str) -> str:
    logger.debug(f"task: {json.dumps(task)}")
    is_code = task["enter_text"]["hint_text"].lower() == "confirmation code"
    value = get_verification_code(email, email_password) if is_code else email
    assert value is not None, "Could not get verification code"
    return value


def login_confirm_email(client: Client, flow_token: str, value: str) -> Response:
    payload = {
        "flow_token": flow_token,
        "subtask_inputs": [
            {
                "subtask_id": "LoginAcid",
                "enter_text": {"text": value, "link": "next_link"},
            }
        ],
    }

    rep = client.post(TASK_URL, json=payload)
    raise_for_status(rep, "login_confirm_email")
    return rep


def login_success(client: Client, flow_token: str) -> Response:
    payload = {
        "flow_token": flow_token,
        "subtask_inputs": [],
    }

    rep = client.post(TASK_URL, json=payload)
    raise_for_status(rep, "login_success")
    return rep


class UserClient:
    def __init__(self, username: str, password: str, email: str, email_password: str):
        self.username = username
        self.password = password
        self.email = email
        self.email_password = email_password
        self.client = Client()
        self.session_path = f"sessions/{self.username}.session.json"

        dirname = os.path.dirname(self.session_path)
        os.makedirs(dirname, exist_ok=True)

        self.limits: dict[str, datetime] = {}
        self.locked: dict[str, bool] = {}

    def save(self):
        cookies = dict(self.client.cookies)
        headers = dict(self.client.headers)
        limits = dict(self.limits)

        with open(self.session_path, "w") as fp:
            json.dump({"cookies": cookies, "headers": headers, "limits": limits}, fp, indent=2)

    def restore(self):
        try:
            with open(self.session_path) as fp:
                data = json.load(fp)
                self.client.cookies.update(data.get("cookies", {}))
                self.client.headers.update(data.get("headers", {}))
                self.limits.update(
                    {k: datetime.fromisoformat(v) for k, v in data.get("limits", {}).items()}
                )
                return True
        except (FileNotFoundError, json.JSONDecodeError):
            return False

    def make_client(self) -> AsyncClient:
        client = AsyncClient()
        client.headers.update(self.client.headers)
        client.cookies.update(self.client.cookies)
        return client

    def can_use(self, queue: str):
        if self.locked.get(queue, False):
            return False

        limit = self.limits.get(queue)
        return not limit or limit <= datetime.now(timezone.utc)

    def lock(self, queue: str):
        self.locked[queue] = True

    def unlock(self, queue: str):
        self.locked[queue] = False

    def update_limit(self, queue: str, rep: Response):
        reset = rep.headers.get("x-rate-limit-reset", 0)
        reset = datetime.fromtimestamp(int(reset), tz=timezone.utc)
        self.limits[queue] = reset
        self.save()

    def print_session(self):
        for x in self.client.headers.items():
            print(x)

        print()
        for x in self.client.cookies.items():
            print(x)

    def _next_login_task(self, rep: Response):
        client = self.client

        ct0 = client.cookies.get("ct0", None)
        if ct0:
            client.headers["x-csrf-token"] = ct0
            client.headers["x-twitter-auth-type"] = "OAuth2Session"

        data = rep.json()
        logger.debug(f"login tasks: {[x['subtask_id'] for x in data['subtasks']]}")

        flow_token = data["flow_token"]
        for x in data["subtasks"]:
            task_id = x["subtask_id"]

            if task_id == "LoginSuccessSubtask":
                return login_success(client, flow_token)
            if task_id == "LoginAcid":
                value = get_confirm_email_code(x, self.email, self.email_password)
                return login_confirm_email(client, flow_token, value)
            if task_id == "AccountDuplicationCheck":
                return login_duplication_check(client, flow_token)
            if task_id == "LoginEnterPassword":
                return login_enter_password(client, flow_token, self.password)
            if task_id == "LoginEnterUserIdentifierSSO":
                return login_enter_username(client, flow_token, self.username)
            if task_id == "LoginJsInstrumentationSubtask":
                return login_instrumentation(client, flow_token)

        return None

    def login(self):
        if self.restore():
            logger.debug(f"session restored for {self.username}")
            return

        self.client.headers["user-agent"] = UserAgent().safari
        self.client.headers["content-type"] = "application/json"
        self.client.headers["authorization"] = TOKEN
        self.client.headers["x-twitter-active-user"] = "yes"
        self.client.headers["x-twitter-client-language"] = "en"

        guest_token = login_get_guest_token(self.client)
        self.client.headers["x-guest-token"] = guest_token

        rep = login_initiate(self.client)
        while True:
            rep = self._next_login_task(rep)
            if rep is None:
                break

        self.client.headers["x-csrf-token"] = self.client.cookies["ct0"]
        self.client.headers["x-twitter-auth-type"] = "OAuth2Session"
        self.save()

        logger.debug(f"login success for {self.username}")
