"""Streamlit chatbot for the Agent Access Pattern demo.

Auth flow used here is the **Resource Owner Password Credentials** grant
(``grant_type=password``) — the simplest possible OIDC flow, chosen so the
demo works out-of-the-box without iframes or redirect callbacks. In
production you'd swap this for the auth-code + PKCE flow (any OIDC-aware
UI framework will do). The interesting security part of the pattern — the
RFC 8693 token exchange and the MCP gateway's fine-grained authorization —
is downstream and stays exactly the same.
"""
from __future__ import annotations

import os
import time

import requests
import streamlit as st

st.set_page_config(page_title="Agent Access Pattern", page_icon="🔐", layout="centered")

KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "http://localhost:8080")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "agents")
KEYCLOAK_CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID", "ui-client")
AGENT_URL = os.getenv("AGENT_URL", "http://localhost:8000")

TOKEN_URL = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _login(username: str, password: str) -> tuple[bool, str]:
    try:
        r = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "password",
                "client_id": KEYCLOAK_CLIENT_ID,
                "username": username,
                "password": password,
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        return False, f"Cannot reach Keycloak: {exc}"
    if r.status_code != 200:
        return False, f"Login failed ({r.status_code}): {r.text}"
    tok = r.json()
    st.session_state.access_token = tok["access_token"]
    st.session_state.refresh_token = tok.get("refresh_token", "")
    st.session_state.expires_at = time.time() + int(tok.get("expires_in", 300))
    st.session_state.username = username
    return True, "ok"


def _logout() -> None:
    refresh_token = st.session_state.get("refresh_token")
    if refresh_token:
        try:
            requests.post(
                f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/logout",
                data={"client_id": KEYCLOAK_CLIENT_ID, "refresh_token": refresh_token},
                timeout=5,
            )
        except Exception:  # noqa: BLE001
            pass
    for k in ("access_token", "refresh_token", "expires_at", "username", "history"):
        st.session_state.pop(k, None)


def _is_authenticated() -> bool:
    exp = st.session_state.get("expires_at", 0)
    return bool(st.session_state.get("access_token")) and time.time() < exp


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("🔐 Agent Access Pattern")
st.caption(
    "Login with Keycloak → your token is exchanged (RFC 8693) → the agent "
    "calls MCP tools through a gateway that enforces per-agent permissions."
)

if not _is_authenticated():
    st.info("Sign in to start chatting. Default demo credentials: **demo / demo**.")
    with st.form("login_form"):
        col1, col2 = st.columns(2)
        with col1:
            username = st.text_input("Username", value="demo")
        with col2:
            password = st.text_input("Password", value="demo", type="password")
        submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)
    if submitted:
        ok, msg = _login(username, password)
        if ok:
            st.rerun()
        else:
            st.error(msg)
    st.stop()


# Authenticated area ---------------------------------------------------------
st.success(f"✅ Signed in as **{st.session_state.username}** — ready to chat.")

with st.sidebar:
    st.subheader("Session")
    st.write({"user": st.session_state.username, "client": KEYCLOAK_CLIENT_ID})
    if st.button("Log out", use_container_width=True):
        _logout()
        st.rerun()

if "history" not in st.session_state:
    st.session_state.history = []

for role, content in st.session_state.history:
    with st.chat_message(role):
        st.markdown(content)


def _ask(prompt: str) -> None:
    st.session_state.history.append(("user", prompt))
    try:
        r = requests.post(
            f"{AGENT_URL}/chat",
            json={"question": prompt, "user_token": st.session_state.access_token},
            timeout=60,
        )
        if r.status_code == 200:
            payload = r.json()
            answer = payload["answer"]
            answer += (
                f"\n\n_acting as `{payload['acting_as']}` on behalf of "
                f"`{payload.get('on_behalf_of')}`_"
            )
        else:
            answer = f"⚠️ Agent error {r.status_code}: {r.text}"
    except Exception as exc:  # noqa: BLE001
        answer = f"⚠️ {exc}"
    st.session_state.history.append(("assistant", answer))


prompt = st.chat_input("Ask 'what is the weather?' or 'list the employees of the company'")
if prompt:
    _ask(prompt)
    st.rerun()
