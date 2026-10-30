# cockpit-tools — Credential Storage / Write-Switch / OAuth / Quota Spec

Repo: `jlcodes99/cockpit-tools` (branch `main`)
Scope: all 16 AI IDE platforms. Every literal below is quoted from source.

## Global conventions

- Cockpit's own data dir constant: `DATA_DIR = ".cockpit_tools"` (legacy `.antigravity_cockpit`), under `$HOME` on mac/Linux and `%APPDATA%` on Windows.
- Per-platform cockpit account stores live at `<data_dir>/<platform>_accounts.json` (index) + `<data_dir>/<platform>_accounts/` (one JSON per account). Instances root: `~/.cockpit_tools/instances/<platform>`.
- Generic VS Code SQLite secret read/write lives in `crates/cockpit-core/src/modules/vscode_inject.rs`. Secret key format is `secret://{"extensionId":"<ext>","key":"<key>"}` (line 119).
- VS Code data roots (`crates/cockpit-core/src/modules/vscode_paths.rs`):
  - mac: `~/Library/Application Support/Code`, `~/Library/Application Support/Code - Insiders`
  - win: `%APPDATA%\Code`, `%APPDATA%\Code - Insiders`
  - linux: `$XDG_CONFIG_HOME`/`~/.config` + `/Code`, `/Code - Insiders`
  - state db = `<root>/User/globalStorage/state.vscdb`
- Three storage classes appear:
  1. **SQLite `state.vscdb`** — GitHub Copilot, Windsurf, Cursor, Qoder, CodeBuddy, CodeBuddy CN, Antigravity (<2.0.0 legacy).
  2. **Plain JSON/TOML auth files** — Codex, Kiro (auth token + profile), Trae (all 4), Grok CLI, ZCode.
  3. **OS keychain** — Antigravity (≥2.0.0), Zed, Codex (macOS keychain mirror).

---

## 1. Antigravity IDE

- **id / name**: `antigravity` / "Antigravity IDE"
- **Cockpit store**: legacy single-account `accounts/` under data dir; model `Account` + `TokenData` (`crates/cockpit-core/src/models/account.rs`, `models/token.rs`).
- **Credential paths**
  - Data dir: mac `~/Library/Application Support/Antigravity IDE` (legacy `.../Antigravity`); win `%APPDATA%\Antigravity IDE` then `...\Antigravity`; linux `~/.config/Antigravity IDE` (`src-tauri/src/modules/antigravity_paths.rs`).
  - state db: `<dir>/User/globalStorage/state.vscdb`; also `storage.json`, `machineid`.
  - Fallback file: `~/.gemini/jetski-standalone-oauth-token`.
  - OS credential (≥2.0.0, `antigravity_credential.rs`):
    - macOS Keychain generic-password `service=gemini`, `account=antigravity`, value `go-keyring-base64:<base64(payload JSON)>`.
    - Windows Credential Manager target `gemini:antigravity`, user `antigravity`.
    - Linux `secret-tool store --label=gemini service gemini username antigravity`.
- **Credential JSON shape** (`build_antigravity_credential_payload`):
  `{"token":{"access_token":..,"token_type":"Bearer","refresh_token":..,"expiry":<RFC3339 micros>},"auth_method":"consumer","id_token":..}`
  Model fields: `TokenData{access_token, refresh_token, expires_in, expiry_timestamp, token_type, email, project_id, is_gcp_tos, oauth_client_key, id_token, session_id}`.
- **Write / switch**
  - v2+ (`SystemCredential`): writes Keychain/Credential Manager/secret-tool **and** the `~/.gemini/jetski-standalone-oauth-token` fallback with identical payload (`write_antigravity_credential_payload`).
  - <2.0.0 (`LegacyStateDb`, `src-tauri/src/modules/db.rs`): `INSERT OR REPLACE INTO ItemTable` keys:
    - `antigravityUnifiedStateSync.oauthToken` (base64 protobuf `oauthTokenInfoSentinelKey`)
    - `antigravityUnifiedStateSync.userStatus` (only if absent)
    - `antigravityUnifiedStateSync.enterprisePreferences`
    - `antigravityOnboarding` = `"true"`
    - `storage.serviceMachineId`
- **OAuth**: Google consumer OAuth (id_token present, `auth_method="consumer"`). No dedicated client_id constant in-repo; credentials imported/injected.
- **Quota**: no HTTP quota endpoint; state driven by `antigravityUnifiedStateSync.userStatus`.

---

## 2. Codex

- **id / name**: `codex` / "Codex"
- **Real credential**: `~/.codex/auth.json` (or `$CODEX_HOME/auth.json`) + `~/.codex/config.toml`; cockpit projection `~/.codex/.cockpit_codex_auth.json` (`CODEX_AUTH_PROJECTION_FILE_NAME`).
- **macOS Keychain mirror** (`codex_account_core_authority.rs`, `codex_account_core_provider.rs`): service `CODEX_KEYCHAIN_SERVICE = "Codex Auth"`, account `build_codex_keychain_account` = `cli|<sha256(canonical base_dir) hex[..16]>`, via `security add-generic-password -U -s "Codex Auth" -a <acct> -w <json>`.
- **Windows** (`codex_account_windows_auth_store.rs`): Credential Manager service `"Codex Auth"`, account `cli|<sha256 hex[..16]>`; plus age-encrypted `secrets/codex_auth.age`.
- **auth.json shape** (`CodexAuthFile` / `CodexAuthTokens`, `models/codex.rs`):
  `{auth_mode, OPENAI_API_KEY, base_url, tokens:{id_token,access_token,refresh_token,account_id}, agent_identity, last_refresh}`; `mark_codex_auth_type` stamps `auth_type:"codex"`.
  Account model `CodexAccount{id,email,auth_mode,openai_api_key,api_base_url,api_provider_mode,...,tokens:CodexTokens{id_token,access_token,refresh_token,account_id},agent_identity,...}`.
- **Write / switch** (`codex_account_runtime_switch.rs`): `switch_account_with_prepared` → `write_prepared_account_bundle_to_dir(codex_home)` writes `auth.json`, keychain, and `config.toml`; updates index `current_account_id`.
- **OAuth** (`codex_oauth.rs`):
  - PKCE loopback port `1455` (`OAUTH_CALLBACK_PORT`), client_id `app_EMoamEEZ73f0CkXaXp7hrann`, authorize `https://auth.openai.com/oauth/authorize`, token `https://auth.openai.com/oauth/token`, scopes `openid profile email offline_access api.connectors.read api.connectors.invoke`.
  - Device code: `https://auth.openai.com/api/accounts/deviceauth/usercode`, `.../deviceauth/token`, verify `https://auth.openai.com/codex/device`.
- **Quota**: `https://chatgpt.com/backend-api/wham/accounts/check` (`ACCOUNT_CHECK_URL`).

---

## 3. GitHub Copilot

- **id / name**: `github_copilot` / "GitHub Copilot"
- **Cockpit store**: `github_copilot_accounts.json` / `github_copilot_accounts/`.
- **Real target**: VS Code `state.vscdb` (see global paths; win also `.vscode-shared`/`.vscode-shared-insiders`).
- **Write / switch** (`vscode_inject.rs`, `github_copilot_instance.rs`): `write_copilot_items_to_db` → `INSERT OR REPLACE INTO ItemTable`:
  - `secret://{"extensionId":"vscode.github-authentication","key":"github.auth"}` — encrypted Buffer JSON sessions array (session scopes `read:user user:email repo workflow`).
  - `github.copilot-github` — username.
  Model `GitHubCopilotAccount{github_login,github_id,github_access_token,copilot_token,copilot_plan,copilot_quota_snapshots,...}`.
- **OAuth** (`github_copilot_oauth.rs`): GitHub OAuth, client_id `01ab8ac9400c4e429b23`, authorize `https://github.com/login/oauth/authorize`, token `https://github.com/login/oauth/access_token`, redirect `https://vscode.dev/redirect`, scope `read:user repo user:email workflow`; user `https://api.github.com/user`, emails `https://api.github.com/user/emails`.
- **Quota**: `https://api.github.com/copilot_internal/user`, token `https://api.github.com/copilot_internal/v2/token`.

---

## 4. Windsurf

- **id / name**: `windsurf` / "Windsurf"
- **Cockpit store**: `windsurf_accounts.json` / `windsurf_accounts/`.
- **Real target**: `state.vscdb` — mac `~/Library/Application Support/Windsurf/User/globalStorage/state.vscdb` (Devin rename handled), win `%APPDATA%\Windsurf\...`, linux `~/.config/Windsurf/...`.
- **Write / switch** (`windsurf_instance.rs`, `write_windsurf_auth_data`), SQL keys:
  - `windsurfAuthStatus` (JSON, incl. `userStatusProtoBinaryBase64`)
  - `secret://{"extensionId":"codeium.windsurf","key":"windsurf_auth.sessions"}` (encrypted Buffer JSON; sessions payload `[{id,accessToken,account:{label,id},scopes}]`)
  - `secret://{"extensionId":"codeium.windsurf","key":"windsurf_auth.apiServerUrl"}`
  - `codeium.windsurf-windsurf_auth` (selected label)
  - `codeium.windsurf` (ext state JSON incl. `apiServerUrl`,`windsurf.pendingApiKeyMigration`,`codeium.installationId`)
  - `windsurfOnboarding`; `windsurf_auth-<label>`; `windsurf_auth-<label>-usages`; `DELETE ... WHERE key LIKE 'windsurf_auth-%'`
  - Model `WindsurfAccount{github_*, copilot_*, windsurf_api_key,windsurf_api_server_url,windsurf_auth_token,windsurf_auth_status_raw, devin_auth1_token,devin_account_id,devin_org_id,devin_session_token,devin_user_status_proto_b64}`.
- **OAuth** (`windsurf_oauth.rs`, `windsurf_devin_oauth.rs`): implicit/Firebase loopback `https://www.windsurf.com/windsurf/signin?response_type=token&client_id=3GUryQ7ldAeKEuD2obYnppsnmj58eP5u&redirect_uri=http://127.0.0.1:{port}/windsurf-auth-callback`; Firebase `https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword`; Devin Auth `https://windsurf.com/_devin-auth/password/login`, `.../SeatManagementService/WindsurfPostAuth`, `.../GetOneTimeAuthToken`, `https://register.windsurf.com/.../RegisterUser`, `.../GetCurrentUser`. Default api server `https://server.codeium.com`.
- **Quota**: `windsurfAuthStatus` (embedded plan/quota); Devin user status protobuf.

---

## 5. Kiro

- **id / name**: `kiro` / "Kiro"
- **Cockpit store**: `kiro_accounts.json` / `kiro_accounts/`.
- **Real credential paths**:
  - Auth token `~/.aws/sso/cache/kiro-auth-token.json` (+ `~/.aws/sso/cache/<clientIdHash>.json`) — `get_default_kiro_auth_token_path`.
  - Profile `~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/profile.json` (win `%APPDATA%\Kiro`, linux `~/.config/Kiro`).
  - Usage/state db `<dir>/User/globalStorage/state.vscdb` key `kiro.kiroAgent` (`LOCAL_USAGE_DB_KEY`).
- **Write / switch**: writes token file JSON fields `accessToken,refreshToken,tokenType,expiresAt,profileArn,provider,loginProvider,authMethod,idc_region,issuer_url,client_id,clientIdHash,scopes,login_hint,email,userId`; profile.json `{arn,name,email}`; usage snapshot into `state.vscdb`.
  Model `KiroAccount{access_token,refresh_token,token_type,expires_at,idc_region,issuer_url,client_id,scopes,login_hint,kiro_auth_token_raw,kiro_profile_raw,kiro_usage_raw,...}`.
- **OAuth** (`kiro_oauth.rs`): PKCE loopback (`code_challenge_method=S256`) portal `https://app.kiro.dev/signin`, callback ports `[3128,4649,6588,8008,9091,49153,50153,51153,52153,53153]`, token `https://prod.us-east-1.auth.desktop.kiro.dev/oauth/token`, refresh `.../refreshToken`; AWS Builder ID / IdC token `https://oidc.{region}.amazonaws.com/token`.
- **Quota**: runtime `https://q.{region}.amazonaws.com` (default `https://q.us-east-1.amazonaws.com`; regional map incl. `q-fips.*`).

---

## 6. Cursor

- **id / name**: `cursor` / "Cursor"
- **Cockpit store**: `cursor_accounts.json` / `cursor_accounts/`.
- **Real target**: `state.vscdb` — mac `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`, win `%APPDATA%\Cursor\...`, linux `~/.config/Cursor/...`.
- **Write / switch** (`cursor_account.rs`), `INSERT OR REPLACE` keys:
  `cursorAuth/accessToken`, `cursorAuth/refreshToken`, `cursorAuth/cachedEmail`, `cursorAuth/stripeMembershipType`, `cursorAuth/stripeSubscriptionStatus`, `cursorAuth/authId`, `cursorAuth/cachedSignUpType`, `cursor.accessToken`, `cursor.email`.
  Model `CursorAccount{email,auth_id,access_token,refresh_token,membership_type,subscription_status,sign_up_type,cursor_auth_raw,cursor_usage_raw,...}`.
- **OAuth** (`cursor_oauth.rs`): login `https://cursor.com/loginDeepControl`, poll `https://api2.cursor.sh/auth/poll`, token `https://api2.cursor.sh/oauth/token`, client_id `KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB`.
- **Quota**: `https://cursor.com/api/usage-summary`, `https://api2.cursor.sh/aiserver.v1.AuthService/GetUserMeta`, `.../auth/full_stripe_profile`, `.../auth/stripe_profile`.

---

## 7. Qoder

- **id / name**: `qoder` / "Qoder"
- **Cockpit store**: `qoder_accounts.json` / `qoder_accounts/`.
- **Real target**: `state.vscdb` under Qoder user data dir `<root>/User/globalStorage/state.vscdb` (mac `~/Library/Application Support/Qoder`, win `%APPDATA%\Qoder`, linux `~/.config/Qoder`).
- **Write / switch** (`qoder_account.rs`, via `inject_secret_to_state_db_for_qoder`), keys:
  `secret://aicoding.auth.userInfo`, `secret://aicoding.auth.userPlan`, `secret://aicoding.auth.creditUsage`.
  Model `QoderAccount{auth_user_info_raw,auth_user_plan_raw,auth_credit_usage_raw,plan_type,credits_used,credits_total,...}`.
- **OAuth** (`qoder_oauth.rs`): PKCE device login, base `https://qoder.com/device/selectAccounts`, openapi `https://openapi.qoder.sh`, redirect `qoder://aicoding.aicoding-agent/login-success`, S256; poll `/api/v1/deviceToken/poll`.
- **Quota** paths: `/api/v2/quota/usage`, `/api/v2/user/plan`, `/api/v3/user/status`, `/api/v1/userinfo`.

---

## 8. CodeBuddy

- **id / name**: `codebuddy` / "CodeBuddy"
- **Cockpit store**: `codebuddy_accounts.json` / `codebuddy_accounts/`.
- **Real target**: `state.vscdb` — mac `~/Library/Application Support/CodeBuddy/User/globalStorage/state.vscdb`, win `%APPDATA%\CodeBuddy\...`, linux `~/.config/CodeBuddy/...`.
- **Write / switch**: `inject_secret_to_state_db_for_codebuddy` (`vscode_inject.rs`); ext `tencent-cloud.coding-copilot`, key `planning-genie.new.accessToken` (`CODEBUDDY_SECRET_KEY`).
  Session JSON has `id:"Tencent-Cloud.genie-ide"`, `token`, `refreshToken`, `expiresAt`, `domain`, `accessToken:"<uid>+<token>"`, `account{...}`, `auth{accessToken,refreshToken,tokenType,domain,expiresAt,...}`.
  Model `CodebuddyAccount{uid,nickname,enterprise_id,enterprise_name,access_token,refresh_token,domain,plan_type,dosage_notify_*,quota_raw,auth_raw,profile_raw,usage_raw,...}`.
- **OAuth** (`codebuddy_oauth.rs`): base `https://www.codebuddy.ai`, prefix `/v2/plugin`; `/auth/state?platform=`, login `https://www.codebuddy.ai/login?state=`, poll `/auth/token?state=`, `/login/account?state=`, refresh `/auth/token/refresh`.
- **Quota** (`/v2/billing/meter/...`): `get-dosage-notify`, `get-payment-type`, `get-user-resource`, `get-enterprise-user-usage`.

---

## 9. CodeBuddy CN

- **id / name**: `codebuddy_cn` / "CodeBuddy CN"
- **Cockpit store**: `codebuddy_cn_accounts.json` / `codebuddy_cn_accounts/`.
- **Real target**: `state.vscdb` — mac `~/Library/Application Support/CodeBuddy CN/User/globalStorage/state.vscdb`, win `%APPDATA%\CodeBuddy CN\...`, linux `~/.config/CodeBuddy CN/...`.
- **Write / switch**: `inject_secret_to_state_db_for_codebuddy_cn`; ext `tencent-cloud.coding-copilot`, key `planning-genie.new.accessTokencn`.
- **OAuth** (`codebuddy_cn_oauth.rs`): base `https://www.codebuddy.cn`, prefix `/v2/plugin`; same `/auth/state`, `/login`, `/auth/token`, `/login/account`, `/auth/token/refresh`.
- **Quota**: same `/v2/billing/meter/*` paths on `www.codebuddy.cn`.

---

## 10–13. Trae / TRAE SOLO / Trae CN / TRAE SOLO CN

- **ids / names** (`TraePlatformKind`): `trae`→"Trae", `trae_solo`→"TRAE SOLO", `trae_cn`→"Trae CN", `trae_solo_cn`→"TRAE SOLO CN". `app_support_dir_name()` = display name.
- **Cockpit store**: `trae_accounts.json` / `trae_accounts/` (shared; platform param).
- **Real target**: `<data dir>/User/globalStorage/storage.json` — mac `~/Library/Application Support/<name>`, win `%APPDATA%\<name>`, linux `~/.config/<name>` (names = display names).
- **Write / switch** (`trae_account_core_platform_storage.rs`, `..._product_paths.rs`, `..._injection.rs`), storage.json keys:
  - `iCubeAuthInfo://icube.cloudide` (icube-cipher base64 auth)
  - `iCubeEntitlementInfo://icube.cloudide` (JSON string)
  - `iCubeServerData://icube.cloudide` (JSON string)
  - `iCubeAuthInfo://usertag`
  - `iCubeAuthInfo://icube-dc:<device_id>`
  Model `TraeAccount{user_id,nickname,access_token,refresh_token,token_type,expires_at,plan_type,trae_auth_raw,trae_profile_raw,trae_entitlement_raw,trae_usage_raw,trae_server_raw,trae_usertag_raw,...}`.
- **OAuth** (`trae_oauth.rs`): browser login guidance `https://www.trae.ai/cloudide/api/v3/trae/GetLoginGuidance` (and api.marscode.com / api.trae.ai / api.trae.cn / api.trae.com.cn); exchange `/cloudide/api/v3/trae/oauth/ExchangeToken` and `/trae/api/v3/oauth/ExchangeToken`; userinfo `/cloudide/api/v3/trae/GetUserInfo`; client_ids `ono9krqynydwx5` (Trae) / `en1oxy7wnw8j9n` (SOLO); domains `www.trae.ai` / `www.trae.cn`; origins `grow-normal.trae.ai`, `growsg-normal.trae.ai`, `grow-normal.traeapi.us`, `api.trae.cn`, `api.trae.com.cn`.
- **Quota** paths: `/trae/api/v1/pay/ide_user_pay_status`, `/trae/api/v1/pay/ide_user_ent_usage`, `/cloudide/api/v3/trae/CheckLogin`.

---

## 14. Zed

- **id / name**: `zed` / "Zed"
- **Cockpit store**: `zed_accounts.json` / `zed_accounts/`.
- **Real credential**: macOS Keychain **internet-password**, server `ZED_SERVER_URL = "https://zed.dev"`, account = user_id, password = access_token. `security add-internet-password -U -a <user_id> -s https://zed.dev -w <token>`. Switching macOS-only.
- **Models**: `ZedAccount{user_id,github_login,plan_raw,subscription_status,token_spend_*,edit_predictions_*,user_raw,subscription_raw,usage_raw,...}`; `ZedStoredAccount{public_account:ZedAccount, access_token}`.
- **OAuth** (`zed_oauth.rs`): RSA-encrypted callback loopback `127.0.0.1`; verify `https://zed.dev/native_app_signin?native_app_port=<port>&native_app_public_key=<b64>`.
- **Quota**: `https://cloud.zed.dev/client/users/me` (`ZED_CLOUD_BASE_URL`).

---

## 15. Grok CLI

- **id / name**: `grok` / "Grok CLI"
- **Cockpit store**: `grok_accounts.json` / `grok_accounts/` + `grok_profiles/`.
- **Real credential**: `~/.grok/auth.json` (or `$GROK_HOME/auth.json`) + `~/.grok/config.toml` (`DEFAULT_HOME_DIR=".grok"`, `AUTH_FILE="auth.json"`, `CONFIG_FILE="config.toml"`).
- **auth.json shape**: registry keyed `https://auth.x.ai::b1a00492-073a-47ea-816f-4c329264a828`; entry `{key(=access_token), auth_mode:"oidc", email, refresh_token, user_id, principal_id, principal_type, team_id, first_name, last_name, profile_image_asset_id, coding_data_retention_opt_out, oidc_issuer, oidc_client_id, expires_at}`.
  Model `GrokAccount{auth_mode,first_name,last_name,user_id,principal_id,principal_type,team_id,profile_image_asset_id,coding_data_retention_opt_out,access_token,api_key,refresh_token,id_token,expires_at,oidc_issuer,oidc_client_id,...}`.
- **Write / switch** (`grok_account.rs`): OAuth writes official `auth.json`; API Key accounts only recorded in cockpit (official auth.json untouched); managed per-account `GROK_HOME` dir.
- **OAuth** (`grok_oauth.rs`): device-code (RFC 8628), issuer `https://auth.x.ai`, client_id `b1a00492-073a-47ea-816f-4c329264a828`, token `https://auth.x.ai/oauth2/token`, discovery `https://auth.x.ai/.well-known/openid-configuration`, device `https://auth.x.ai/oauth2/device`; scope `openid profile email offline_access grok-cli:access api:access conversations:read conversations:write workspaces:read workspaces:write`.
- **Quota**: `https://cli-chat-proxy.grok.com/v1/billing?format=credits`, `.../v1/user?include=subscription`, `https://grok.com/rest/subscriptions`, `https://grok.com/rest/tasks/usage`.

---

## 16. ZCode

- **id / name**: `zcode` / "ZCode"
- **Cockpit store**: `zcode_accounts.json` / `zcode_accounts/`.
- **Real credential**: `~/.zcode/v2/credentials.json` (OAuth), `~/.zcode/v2/config.json` (API key), `~/.zcode/v2/setting.json` (`dataBaseDir` override); instance root `<root>/data/.zcode/v2/...`; app data mac `~/Library/Application Support/ZCode`.
- **Credential format**: AES-256-GCM, ciphertext prefix `CREDENTIAL_PREFIX = "enc:v1:"`.
  credentials.json keys: `oauth:active_provider`, `oauth:<provider>:access_token`, `oauth:<provider>:refresh_token`, `oauth:<provider>:user_info`, `zcodejwttoken` (providers `zai` / `bigmodel`).
  Model `ZcodeAccount{auth_mode,provider,email,user_id,access_token,refresh_token,zcode_jwt_token,api_key,expires_at,plan_type,quota_*,user_info_raw,subscription_raw,quota_raw,...}`.
- **Write / switch** (`zcode_account.rs`): OAuth → credentials.json encrypted values; API Key → config.json (`write_json_map`).
- **OAuth** (`zcode_oauth.rs`): PKCE loopback, authorize `https://chat.z.ai/api/oauth/authorize` (client_id `client_P8X5CMWmlaRO9gyO-KSqtg`, redirect `zcode://zai-auth/callback`), token `https://zcode.z.ai/api/v1/oauth/token`, userinfo `https://chat.z.ai/api/oauth/userinfo`, business login `https://api.z.ai/api/auth/z/login`; bigmodel login `https://bigmodel.cn/login` (redirect `zcode://oauth/callback`), userinfo `https://open.bigmodel.cn/api/biz/customer/getCustomerInfo`.
- **Quota**: `https://zcode.z.ai/api/v1/zcode-plan/billing/balance` (`BILLING_BALANCE_URL`).

---

## Cross-cutting notes

- **Platforms whose switching edits `state.vscdb`** (SQLite `ItemTable`):
  - GitHub Copilot: `secret://{...vscode.github-authentication...github.auth}`, `github.copilot-github`
  - Windsurf: `windsurfAuthStatus`, `secret://{...codeium.windsurf...}`, `codeium.windsurf-windsurf_auth`, `codeium.windsurf`, `windsurfOnboarding`, `windsurf_auth-*`
  - Cursor: `cursorAuth/*`, `cursor.accessToken`, `cursor.email`
  - Qoder: `secret://aicoding.auth.userInfo|userPlan|creditUsage`
  - CodeBuddy: `secret://{...tencent-cloud.coding-copilot...planning-genie.new.accessToken}`
  - CodeBuddy CN: `secret://{...tencent-cloud.coding-copilot...planning-genie.new.accessTokencn}`
  - Antigravity (<2.0.0 legacy): `antigravityUnifiedStateSync.*`, `antigravityOnboarding`, `storage.serviceMachineId`
  - Kiro (usage snapshot only): `kiro.kiroAgent`
- **Platforms with their own cockpit store whose real target is a file/keychain** (not cockpit store): Codex (`auth.json`+keychain), Kiro (`.aws/sso/cache` + profile.json), Trae×4 (`storage.json`), Grok (`~/.grok/auth.json`), ZCode (`~/.zcode/v2/*`), Zed (Keychain), Antigravity (Keychain/secret-tool).
