# Architecture

The stack has 5 pieces and 3 phases. Each phase is one round-trip.

## Phase 1 — Login

The user proves who they are to Keycloak and gets a short-lived **user
token**. Nothing else in the system knows about the user yet.

```
                    ┌────────────────────────────────┐
                    │        Keycloak (realm)        │
                    │  users: demo / demo            │
                    │  client: ui-client (public)    │
                    └───────────────▲────────────────┘
                                    │  ①  password grant
                                    │      username + password
                                    │
                                    │  ②  200 OK
                                    │      { access_token, refresh_token, … }
                                    │
   ┌──────────┐    login form      ┌──────────────────┐
   │   User   │ ─────────────────► │  Streamlit UI    │
   │ (browser)│ ◄───────────────── │  stores tokens   │
   └──────────┘                    │  in session_state│
                                   └──────────────────┘
```

Result at this point : the UI holds an `access_token` whose payload looks like

```json
{
  "sub": "a439baae-…",           // the user
  "preferred_username": "demo",
  "azp": "ui-client",            // ui-client emitted this token
  "aud": ["agent-1"],            // only agent-1 is allowed to use it
  "iss": "http://keycloak:8080/realms/agents",
  "exp": 1719854400
}
```

The `aud=[agent-1]` is critical — it's what unlocks Phase 2. If it were
missing, Keycloak would reject the token exchange with *"Client is not
within the token audience"*.

---

## Phase 2 — Token exchange (RFC 8693)

The UI sends the user's question **plus the user token** to the agent. The
agent doesn't call the MCP gateway with the user token directly — it first
exchanges it for a narrower token that identifies **both** the user (as
subject) and the agent (as actor).

```
   ┌──────────────┐                ┌─────────────┐
   │ Streamlit UI │ ─── POST ────► │    Agent    │
   │              │  /chat         │  (Strands)  │
   │              │  { question,   │             │
   │              │    user_token }│             │
   └──────────────┘                └──────┬──────┘
                                          │
                                          │  ①  grant_type = urn:ietf:...:token-exchange
                                          │      subject_token = user_token
                                          │      client_id = agent-1
                                          │      audience = mcp-gateway
                                          ▼
                                   ┌────────────────┐
                                   │    Keycloak    │
                                   │  verifies:     │
                                   │  • user_token  │
                                   │    is valid    │
                                   │  • aud has     │
                                   │    agent-1     │
                                   │  • agent-1     │
                                   │    is allowed  │
                                   │    to exchange │
                                   └───────┬────────┘
                                           │  ②  200 OK
                                           │      { access_token: <exchanged> }
                                           ▼
                                   ┌─────────────┐
                                   │    Agent    │
                                   │  now holds  │
                                   │  a token    │
                                   │  bound to   │
                                   │  mcp-gateway│
                                   └─────────────┘
```

The **exchanged token** payload :

```json
{
  "sub": "a439baae-…",         // still the user
  "azp": "agent-1",            // agent-1 initiated the exchange
  "act": { "sub": "agent-1" }, // RFC 8693 actor claim
  "aud": ["mcp-gateway"],      // now targeted at the gateway
  "iss": "http://keycloak:8080/realms/agents",
  "exp": 1719854700
}
```

Key point : the user token from Phase 1 **cannot** be used to talk to the
gateway (wrong audience). Only the exchanged token can. That's how we
prevent a compromised agent from re-using the user's credentials
elsewhere.

---

## Phase 3 — Tool call through the gateway

The agent (driven by the LLM) picks a tool and calls it. The MCP Gateway
is the choke-point : it validates the token and enforces per-agent
authorization before letting the request reach the MCP server.

```
   ┌─────────────┐  Bearer <exchanged_token>   ┌──────────────────┐
   │    Agent    │ ─── POST /mcp ────────────► │   MCP Gateway    │
   │  (Strands)  │  { method:"tools/call",     │                  │
   │             │    params:{name:"get_wea..."│                  │
   └─────────────┘                             │                  │
                                               │  ①  verify JWT   │
                                               │      via Keycloak│
                                               │      JWKS        │
                                               │                  │
                                               │  ②  read acting  │
                                               │      agent from  │
                                               │      act.sub     │
                                               │                  │
                                               │  ③  check        │
                                               │      permissions │
                                               │      .yaml       │
                                               └────┬──────┬──────┘
                                                    │      │
                             ✅ allowed (get_weather)│      │❌ denied (list_employees)
                                                    │      │
                                                    ▼      ▼
                                       ┌──────────────┐   returns JSON-RPC
                                       │  MCP Server  │   { result:{ isError:true,
                                       │  runs the    │       content:[…"Agent
                                       │  tool        │       agent-1 does not
                                       └──────┬───────┘       have permission…"]}}
                                              │
                                              │  { result:"sunny" }
                                              ▼
                                       response bubbles up
                                       Gateway → Agent → UI → User
```

---

## Recap : 3 tokens, 3 audiences

| Phase | Token | `aud` | Purpose |
|---|---|---|---|
| 1 | user token | `agent-1` | proves who the user is; usable only by agent-1 |
| 2 | exchanged token | `mcp-gateway` | proves the agent acts on behalf of the user |
| 3 | — | — | tool call, gateway enforces per-agent ACL |

Each hop **narrows** the token to its next destination. That's least
privilege in action.

## Why a gateway (not tool-side auth) ?

- **Separation of concerns.** Tool servers only implement business logic.
  The gateway holds all policy. Adding a new tool never requires touching
  auth code.
- **Uniform enforcement.** All MCP traffic goes through one place — one
  JWT verifier, one policy file. Auditable and simple.
- **Extensible.** Today `permissions.yaml` is a stand-in ; tomorrow you
  swap it for OPA, Cedar, or Keycloak Authorization Services without
  changing agents or servers.

## Why token exchange (not forwarding the user token) ?

The user token is only usable by `agent-1` (that's the `aud` from Phase 1).
Downstream services must **not** accept it — otherwise a compromised agent
could re-use it to impersonate the user against other systems. By
exchanging, we get a **narrower** token :

- bound to `mcp-gateway` audience (can't be reused elsewhere),
- carrying both identities (`sub`=user, `act.sub`=agent-1),
- traceable in audit logs (who did what, on whose behalf).

That's the least-privilege story, backed by a real audit trail.
